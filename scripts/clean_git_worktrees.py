#!/usr/bin/env python3
"""Audit and prune local Git branches/worktrees by GitHub PR state."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PR_FIELDS = ",".join(
    [
        "number",
        "title",
        "state",
        "mergedAt",
        "url",
        "headRefName",
        "headRefOid",
        "baseRefName",
        "updatedAt",
    ]
)

PROTECTED_BRANCHES = {"main", "master", "develop", "dev", "trunk"}
SUBMODULE_WORKTREE_ERROR = "working trees containing submodules cannot be moved or removed"


@dataclass
class Worktree:
    path: str
    head: str | None = None
    branch: str | None = None
    detached: bool = False
    bare: bool = False
    primary: bool = False


@dataclass
class Branch:
    name: str
    tip: str
    upstream: str | None = None
    upstream_track: str | None = None


@dataclass
class Entry:
    kind: str
    name: str
    branch: Branch | None = None
    worktree: Worktree | None = None
    clean: bool | None = None
    dirty: dict[str, Any] | None = None
    pr: dict[str, Any] | None = None
    base_diff: dict[str, Any] | None = None
    submodule_cleanup: dict[str, Any] | None = None
    decision: str = "kept"
    reason: str = ""
    actions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class GitError(RuntimeError):
    def __init__(self, cmd: list[str], returncode: int, stdout: str, stderr: str):
        super().__init__(stderr.strip() or stdout.strip() or f"command failed: {' '.join(cmd)}")
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def run(
    cmd: list[str],
    *,
    cwd: str | Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        raise GitError(cmd, proc.returncode, proc.stdout, proc.stderr)
    return proc


def git(repo: str | Path, *args: str, check: bool = True) -> str:
    return run(["git", "-C", str(repo), *args], check=check).stdout


def git_ok(repo: str | Path, *args: str) -> bool:
    return run(["git", "-C", str(repo), *args], check=False).returncode == 0


def normalize_repo(repo_arg: str) -> Path:
    repo = Path(repo_arg).expanduser().resolve()
    top = git(repo, "rev-parse", "--show-toplevel").strip()
    return Path(top).resolve()


def parse_worktrees(repo: Path) -> list[Worktree]:
    out = git(repo, "worktree", "list", "--porcelain")
    items: list[Worktree] = []
    current: Worktree | None = None

    def finish() -> None:
        nonlocal current
        if current is not None:
            current.primary = len(items) == 0
            items.append(current)
            current = None

    for raw in out.splitlines():
        if not raw:
            finish()
            continue
        if raw.startswith("worktree "):
            finish()
            current = Worktree(path=raw[len("worktree ") :])
        elif current is None:
            continue
        elif raw.startswith("HEAD "):
            current.head = raw[len("HEAD ") :]
        elif raw.startswith("branch "):
            ref = raw[len("branch ") :]
            current.branch = ref.removeprefix("refs/heads/")
        elif raw == "detached":
            current.detached = True
        elif raw == "bare":
            current.bare = True
    finish()
    return items


def list_branches(repo: Path) -> dict[str, Branch]:
    fmt = "%(refname:short)%00%(objectname)%00%(upstream:short)%00%(upstream:track)"
    out = git(repo, "for-each-ref", f"--format={fmt}", "refs/heads")
    branches: dict[str, Branch] = {}
    for line in out.splitlines():
        if not line:
            continue
        parts = line.split("\0")
        while len(parts) < 4:
            parts.append("")
        name, tip, upstream, upstream_track = parts[:4]
        branches[name] = Branch(
            name=name,
            tip=tip,
            upstream=upstream or None,
            upstream_track=upstream_track or None,
        )
    return branches


def parse_status_counts(lines: list[str]) -> dict[str, int]:
    counts = {
        "staged": 0,
        "unstaged": 0,
        "untracked": 0,
        "deleted": 0,
        "renamed": 0,
    }
    for line in lines:
        if not line:
            continue
        code = line[:2]
        if code == "??":
            counts["untracked"] += 1
            continue
        if code[0] != " ":
            counts["staged"] += 1
        if code[1] != " ":
            counts["unstaged"] += 1
        if "D" in code:
            counts["deleted"] += 1
        if "R" in code:
            counts["renamed"] += 1
    return {key: value for key, value in counts.items() if value}


def clean_shortstat(value: str) -> str:
    return " ".join(value.split()) if value.strip() else ""


def dirty_summary(worktree_path: str, max_files: int) -> tuple[bool, dict[str, Any]]:
    status = git(worktree_path, "status", "--porcelain=v1")
    lines = status.splitlines()
    summary: dict[str, Any] = {
        "counts": parse_status_counts(lines),
        "files": [line[3:] if len(line) > 3 else line for line in lines[:max_files]],
        "file_count": len(lines),
        "staged_shortstat": clean_shortstat(git(worktree_path, "diff", "--cached", "--shortstat")),
        "unstaged_shortstat": clean_shortstat(git(worktree_path, "diff", "--shortstat")),
        "untracked": git(worktree_path, "ls-files", "--others", "--exclude-standard").splitlines()[:max_files],
    }
    return len(lines) == 0, summary


def _git_admin(git_dir: Path, *args: str, check: bool = True) -> str:
    return run(
        ["git", f"--git-dir={git_dir}", "--work-tree=/", *args],
        check=check,
    ).stdout


def _git_admin_ok(git_dir: Path, *args: str) -> bool:
    return (
        run(
            ["git", f"--git-dir={git_dir}", "--work-tree=/", *args],
            check=False,
        ).returncode
        == 0
    )


def _find_submodule_admin_repos(modules_root: Path) -> list[Path]:
    repos: list[Path] = []

    def visit(path: Path) -> None:
        if not path.is_dir():
            return
        if (path / "HEAD").is_file() and (path / "config").is_file() and (path / "objects").is_dir():
            repos.append(path)
            visit(path / "modules")
            return
        for child in sorted(path.iterdir()):
            if child.is_dir():
                visit(child)

    visit(modules_root)
    return repos


def _parse_refs(output: str) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    for line in output.splitlines():
        if not line:
            continue
        oid, _, refname = line.partition("\0")
        if oid and refname:
            refs.append((oid, refname))
    return refs


def _advertised_refs(admin_repo: Path) -> tuple[dict[str, set[str]], list[str]]:
    refs: dict[str, set[str]] = {}
    errors: list[str] = []
    remotes = [line for line in _git_admin(admin_repo, "remote").splitlines() if line]
    if not remotes:
        return refs, ["no configured remote"]

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    for remote in remotes:
        url = _git_admin(admin_repo, "remote", "get-url", remote).strip()
        proc = run(["git", "ls-remote", "--refs", url], check=False, env=env)
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout).strip()
            errors.append(f"unable to query remote {remote}: {message}")
            continue
        for line in proc.stdout.splitlines():
            oid, _, refname = line.partition("\t")
            if oid and refname:
                refs.setdefault(refname, set()).add(oid)
    return refs, errors


def _commit_recoverable(admin_repo: Path, oid: str, remote_oids: set[str]) -> bool:
    if oid in remote_oids:
        return True
    for remote_oid in remote_oids:
        peeled = _git_admin(
            admin_repo,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{remote_oid}^{{commit}}",
            check=False,
        ).strip()
        if not peeled:
            continue
        if _git_admin_ok(admin_repo, "merge-base", "--is-ancestor", oid, peeled):
            return True
    return False


def _audit_submodule_admin_repo(admin_repo: Path, display_path: str) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    head = _git_admin(admin_repo, "rev-parse", "--verify", "HEAD").strip()
    refs = _parse_refs(_git_admin(admin_repo, "for-each-ref", "--format=%(objectname)%00%(refname)", "refs"))
    local_heads = [(oid, refname) for oid, refname in refs if refname.startswith("refs/heads/")]
    local_tags = [(oid, refname) for oid, refname in refs if refname.startswith("refs/tags/")]
    unsupported_refs = [
        refname for _, refname in refs if not refname.startswith(("refs/heads/", "refs/remotes/", "refs/tags/"))
    ]
    if unsupported_refs:
        issues.append(f"{display_path}: unsupported local refs: {', '.join(unsupported_refs)}")

    remote_refs, remote_errors = _advertised_refs(admin_repo)
    issues.extend(f"{display_path}: {error}" for error in remote_errors)
    remote_oids = {oid for oids in remote_refs.values() for oid in oids}

    critical_commits = {head, *(oid for oid, _ in local_heads)}
    for oid in sorted(critical_commits):
        if not _commit_recoverable(admin_repo, oid, remote_oids):
            issues.append(f"{display_path}: commit {oid} is not recoverable from an advertised remote ref")

    for oid, refname in local_tags:
        if oid not in remote_refs.get(refname, set()):
            issues.append(f"{display_path}: local tag {refname} is not preserved by a matching remote tag")

    return (
        {
            "path": display_path,
            "head": head,
            "local_head_count": len(local_heads),
            "local_tag_count": len(local_tags),
            "advertised_remote_ref_count": len(remote_refs),
        },
        issues,
    )


def _audit_submodule_force_removal(worktree_path: str) -> dict[str, Any]:
    git_dir = Path(git(worktree_path, "rev-parse", "--absolute-git-dir").strip())
    modules_root = git_dir / "modules"
    required = modules_root.is_dir() and any(modules_root.iterdir())
    result: dict[str, Any] = {
        "required": required,
        "safe": True,
        "admin_git_dir": str(git_dir),
        "admin_repo_count": 0,
        "repositories": [],
        "issues": [],
    }

    root_status = git(worktree_path, "status", "--porcelain=v1", "--untracked-files=all")
    if root_status:
        result["issues"].append("root worktree became dirty before submodule cleanup")

    submodule_status = git(
        worktree_path,
        "submodule",
        "foreach",
        "--quiet",
        "--recursive",
        "git status --porcelain=v1 --untracked-files=all",
    )
    if submodule_status:
        samples = "; ".join(submodule_status.splitlines()[:12])
        result["issues"].append(f"initialized submodule worktree is dirty: {samples}")

    if not required:
        result["safe"] = not result["issues"]
        return result

    admin_repos = _find_submodule_admin_repos(modules_root)
    result["admin_repo_count"] = len(admin_repos)
    for admin_repo in admin_repos:
        display_path = str(admin_repo.relative_to(git_dir))
        try:
            summary, issues = _audit_submodule_admin_repo(admin_repo, display_path)
        except (GitError, OSError) as exc:
            result["issues"].append(f"{display_path}: unable to audit submodule admin repo: {exc}")
            continue
        result["repositories"].append(summary)
        result["issues"].extend(issues)

    result["safe"] = not result["issues"]
    return result


def parse_count_pair(text: str) -> dict[str, int] | None:
    parts = text.split()
    if len(parts) != 2:
        return None
    try:
        return {"left": int(parts[0]), "right": int(parts[1])}
    except ValueError:
        return None


def ahead_behind(repo: Path, left: str, right: str) -> dict[str, int] | None:
    proc = run(
        ["git", "-C", str(repo), "rev-list", "--left-right", "--count", f"{left}...{right}"],
        check=False,
    )
    if proc.returncode != 0:
        return None
    pair = parse_count_pair(proc.stdout)
    if pair is None:
        return None
    return {"behind": pair["left"], "ahead": pair["right"]}


def resolve_base(repo: Path, preferred: str) -> tuple[str | None, list[str]]:
    notes: list[str] = []
    candidates = [preferred, "origin/main", "origin/master", "main", "master"]
    seen: set[str] = set()
    for ref in candidates:
        if ref in seen:
            continue
        seen.add(ref)
        if git_ok(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
            if ref != preferred:
                notes.append(f"base {preferred!r} not found; using {ref!r}")
            return ref, notes
    notes.append(f"no usable base ref found; tried {', '.join(candidates)}")
    return None, notes


def diff_summary(repo: Path, ref: str, base_ref: str | None, max_files: int) -> dict[str, Any]:
    if base_ref is None:
        return {"base": None, "error": "no base ref available"}
    merge_base_proc = run(
        ["git", "-C", str(repo), "merge-base", base_ref, ref],
        check=False,
    )
    merge_base = merge_base_proc.stdout.strip() if merge_base_proc.returncode == 0 else base_ref
    name_status = git(repo, "diff", "--name-status", "--find-renames", f"{merge_base}..{ref}").splitlines()
    return {
        "base": base_ref,
        "merge_base": merge_base,
        "ahead_behind": ahead_behind(repo, base_ref, ref),
        "shortstat": clean_shortstat(git(repo, "diff", "--shortstat", f"{merge_base}..{ref}")),
        "files": name_status[:max_files],
        "file_count": len(name_status),
    }


def sort_prs(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(pr: dict[str, Any]) -> tuple[int, str]:
        state = pr.get("state") or ""
        merged_at = pr.get("mergedAt") or ""
        updated_at = pr.get("updatedAt") or ""
        if merged_at:
            rank = 3
            date = merged_at
        elif state.upper() == "OPEN":
            rank = 2
            date = updated_at
        else:
            rank = 1
            date = updated_at
        return rank, date

    return sorted(prs, key=key, reverse=True)


def lookup_pr(repo: Path, branch: str) -> dict[str, Any]:
    if shutil.which("gh") is None:
        return {"status": "lookup_error", "error": "gh CLI not found"}

    proc = run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "all",
            "--head",
            branch,
            "--limit",
            "20",
            "--json",
            PR_FIELDS,
        ],
        cwd=repo,
        check=False,
    )
    if proc.returncode != 0:
        return {
            "status": "lookup_error",
            "error": (proc.stderr or proc.stdout).strip(),
        }
    try:
        prs = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"status": "lookup_error", "error": f"failed to parse gh output: {exc}"}
    if not prs:
        return {"status": "no_pr"}

    pr = sort_prs(prs)[0]
    state = (pr.get("state") or "").upper()
    status = "merged" if pr.get("mergedAt") or state == "MERGED" else state.lower() or "unknown"
    pr["status"] = status
    pr["candidates"] = len(prs)
    return pr


def branch_upstream_divergence(repo: Path, branch: Branch) -> dict[str, Any] | None:
    if not branch.upstream:
        return None
    counts = ahead_behind(repo, branch.upstream, branch.name)
    if counts is None:
        return {"upstream": branch.upstream, "error": "unable to compare with upstream"}
    return {"upstream": branch.upstream, **counts}


def deletion_decision(
    entry: Entry,
    *,
    protected: set[str],
    allow_no_pr_deletion: bool,
) -> tuple[bool, str]:
    if entry.kind != "branch" or entry.branch is None:
        return False, "not a local branch"
    branch = entry.branch
    if branch.name in protected:
        return False, "protected branch"
    if entry.worktree and entry.worktree.detached:
        return False, "detached worktree"
    if entry.worktree and entry.worktree.primary:
        return False, "checked out in primary worktree"
    if entry.clean is False:
        return False, "dirty worktree"
    if not entry.pr:
        return False, "no PR lookup result"
    if entry.pr.get("status") == "lookup_error":
        return False, "PR lookup error"
    if entry.pr.get("status") == "no_pr":
        if not allow_no_pr_deletion:
            return False, "no GitHub PR and base freshness is unverified"
        base_diff = entry.base_diff or {}
        counts = base_diff.get("ahead_behind")
        if not counts:
            return False, "no GitHub PR and base comparison is unavailable"
        if counts["ahead"] != 0:
            return False, "no GitHub PR and branch has commits not in base"
        return True, f"no GitHub PR and branch tip is contained in {base_diff.get('base')}"
    if entry.pr.get("status") != "merged":
        return False, f"PR is {entry.pr.get('status')}"
    head_ref_oid = entry.pr.get("headRefOid")
    if head_ref_oid and head_ref_oid != branch.tip:
        return False, "local branch tip differs from merged PR head"
    return True, "merged PR and clean local state"


def delete_entry(repo: Path, entry: Entry, dry_run: bool) -> None:
    branch = entry.branch
    if branch is None:
        return

    current_tip = git(repo, "rev-parse", f"refs/heads/{branch.name}").strip()
    if current_tip != branch.tip:
        entry.errors.append(f"branch tip changed during audit: expected {branch.tip}, found {current_tip}")
        entry.decision = "delete_failed"
        return

    if (entry.pr or {}).get("status") == "no_pr":
        base_ref = (entry.base_diff or {}).get("base")
        if not base_ref or not git_ok(repo, "merge-base", "--is-ancestor", branch.tip, base_ref):
            entry.errors.append(f"base {base_ref or '(unavailable)'} no longer contains branch tip {branch.tip}")
            entry.decision = "delete_failed"
            return

    if dry_run:
        if entry.worktree:
            try:
                entry.submodule_cleanup = _audit_submodule_force_removal(entry.worktree.path)
            except (GitError, OSError) as exc:
                entry.errors.append(f"unable to audit submodule state: {exc}")
                entry.decision = "delete_failed"
                return
            if entry.submodule_cleanup["required"]:
                if not entry.submodule_cleanup["safe"]:
                    entry.reason = "submodule safety check failed"
                    entry.errors.extend(entry.submodule_cleanup["issues"])
                    entry.decision = "kept"
                    return
                entry.actions.append(f"would deinitialize submodules in {entry.worktree.path}")
                entry.actions.append(f"would force-remove worktree {entry.worktree.path}")
            else:
                entry.actions.append(f"would remove worktree {entry.worktree.path}")
        entry.actions.append(f"would delete local branch {branch.name}")
        entry.decision = "would_delete"
        return

    if entry.worktree:
        try:
            git(repo, "worktree", "remove", entry.worktree.path)
            entry.actions.append(f"removed worktree {entry.worktree.path}")
        except GitError as exc:
            if SUBMODULE_WORKTREE_ERROR not in str(exc):
                entry.errors.append(str(exc))
                entry.decision = "delete_failed"
                return
            try:
                entry.submodule_cleanup = _audit_submodule_force_removal(entry.worktree.path)
            except (GitError, OSError) as audit_exc:
                entry.errors.append(f"unable to audit submodule state: {audit_exc}")
                entry.decision = "delete_failed"
                return
            if not entry.submodule_cleanup["safe"]:
                entry.reason = "submodule safety check failed"
                entry.errors.extend(entry.submodule_cleanup["issues"])
                entry.decision = "kept"
                return
            try:
                git(entry.worktree.path, "submodule", "deinit", "--all")
                entry.actions.append(f"deinitialized submodules in {entry.worktree.path}")
                clean_after_deinit, _ = dirty_summary(entry.worktree.path, 0)
                if not clean_after_deinit:
                    entry.errors.append("worktree became dirty after submodule deinitialization")
                    entry.decision = "delete_failed"
                    return
                git(repo, "worktree", "remove", "--force", entry.worktree.path)
                entry.actions.append(f"force-removed worktree {entry.worktree.path} after safe submodule audit")
            except GitError as cleanup_exc:
                entry.errors.append(str(cleanup_exc))
                entry.decision = "delete_failed"
                return

    try:
        current_tip = git(repo, "rev-parse", f"refs/heads/{branch.name}").strip()
        if current_tip != branch.tip:
            entry.errors.append(f"branch tip changed before deletion: expected {branch.tip}, found {current_tip}")
            entry.decision = "delete_failed"
            return
        git(repo, "branch", "-D", branch.name)
        entry.actions.append(f"deleted local branch {branch.name}")
        entry.decision = "deleted"
    except GitError as exc:
        entry.errors.append(str(exc))
        entry.decision = "delete_failed"


def build_entries(
    repo: Path,
    *,
    base_ref: str | None,
    max_files: int,
    protected: set[str],
    allow_no_pr_deletion: bool,
    dry_run: bool,
) -> list[Entry]:
    worktrees = parse_worktrees(repo)
    branches = list_branches(repo)
    worktree_by_branch = {wt.branch: wt for wt in worktrees if wt.branch}
    entries: list[Entry] = []

    for branch_name in sorted(branches):
        branch = branches[branch_name]
        wt = worktree_by_branch.get(branch_name)
        entry = Entry(kind="branch", name=branch_name, branch=branch, worktree=wt)
        if wt is not None:
            entry.clean, entry.dirty = dirty_summary(wt.path, max_files)
        else:
            entry.clean = True
            entry.dirty = {"note": "branch is not checked out in a worktree"}

        entry.pr = lookup_pr(repo, branch.name)
        upstream = branch_upstream_divergence(repo, branch)
        if upstream:
            entry.pr["upstream_divergence"] = upstream

        if entry.pr.get("status") in {"no_pr", "lookup_error"}:
            entry.base_diff = diff_summary(repo, branch.name, base_ref, max_files)

        can_delete, reason = deletion_decision(
            entry,
            protected=protected,
            allow_no_pr_deletion=allow_no_pr_deletion,
        )
        entry.reason = reason
        if can_delete:
            delete_entry(repo, entry, dry_run)
            if entry.decision not in {"deleted", "would_delete", "delete_failed"}:
                entry.decision = "kept"
        else:
            entry.decision = "kept"
        entries.append(entry)

    checked_branches = set(branches)
    for wt in worktrees:
        if wt.branch in checked_branches:
            continue
        name = f"detached:{(wt.head or 'unknown')[:12]}" if wt.detached else f"worktree:{wt.path}"
        entry = Entry(kind="worktree", name=name, worktree=wt)
        if not wt.bare:
            entry.clean, entry.dirty = dirty_summary(wt.path, max_files)
        entry.base_diff = diff_summary(repo, wt.head or "HEAD", base_ref, max_files) if wt.head else None
        entry.reason = "detached or non-branch worktree"
        entry.decision = "kept"
        entries.append(entry)

    return entries


def entry_to_dict(entry: Entry) -> dict[str, Any]:
    return {
        "kind": entry.kind,
        "name": entry.name,
        "branch": entry.branch.__dict__ if entry.branch else None,
        "worktree": entry.worktree.__dict__ if entry.worktree else None,
        "clean": entry.clean,
        "dirty": entry.dirty,
        "pr": entry.pr,
        "base_diff": entry.base_diff,
        "submodule_cleanup": entry.submodule_cleanup,
        "decision": entry.decision,
        "reason": entry.reason,
        "actions": entry.actions,
        "errors": entry.errors,
    }


def format_pr(pr: dict[str, Any] | None) -> str:
    if not pr:
        return "PR: not checked"
    status = pr.get("status")
    if status == "lookup_error":
        return f"PR: lookup error ({pr.get('error')})"
    if status == "no_pr":
        return "PR: none"
    number = pr.get("number")
    title = pr.get("title") or ""
    url = pr.get("url") or ""
    merged_at = pr.get("mergedAt")
    extra = f", merged {merged_at}" if merged_at else ""
    return f"PR: #{number} {status}{extra} - {title} {url}".strip()


def format_dirty(dirty: dict[str, Any] | None) -> list[str]:
    if not dirty:
        return []
    lines: list[str] = []
    if dirty.get("note"):
        lines.append(f"dirty: {dirty['note']}")
        return lines
    counts = dirty.get("counts") or {}
    if counts:
        lines.append(f"dirty: {counts}, files={dirty.get('file_count', 0)}")
    staged = dirty.get("staged_shortstat")
    unstaged = dirty.get("unstaged_shortstat")
    if staged:
        lines.append(f"staged: {staged}")
    if unstaged:
        lines.append(f"unstaged: {unstaged}")
    files = dirty.get("files") or []
    if files:
        lines.append("files: " + "; ".join(files))
    untracked = dirty.get("untracked") or []
    if untracked:
        lines.append("untracked: " + "; ".join(untracked))
    return lines


def format_diff(diff: dict[str, Any] | None) -> list[str]:
    if not diff:
        return []
    if diff.get("error"):
        return [f"diff: {diff['error']}"]
    parts = [f"diff vs {diff.get('base')}"]
    counts = diff.get("ahead_behind")
    if counts:
        parts.append(f"ahead {counts['ahead']}, behind {counts['behind']}")
    if diff.get("shortstat"):
        parts.append(diff["shortstat"])
    parts.append(f"files {diff.get('file_count', 0)}")
    lines = [": ".join([parts[0], ", ".join(parts[1:])])]
    files = diff.get("files") or []
    if files:
        lines.append("diff files: " + "; ".join(files))
    return lines


def print_human_report(report: dict[str, Any]) -> None:
    print(f"Repository: {report['repo']}")
    print(f"Base: {report.get('base') or 'unavailable'}")
    print(f"Mode: {'dry-run' if report['dry_run'] else 'delete-safe-entries'}")
    for note in report.get("notes", []):
        print(f"Note: {note}")
    if report.get("fetch_error"):
        print(f"Fetch error: {report['fetch_error']}")
    print()

    entries = report["entries"]
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["decision"]] = counts.get(entry["decision"], 0) + 1
    print("Summary: " + ", ".join(f"{key}={counts[key]}" for key in sorted(counts)))
    print()

    for entry in entries:
        wt = entry.get("worktree")
        branch = entry.get("branch")
        location = wt.get("path") if wt else "(no worktree)"
        tip = (branch or {}).get("tip") or (wt or {}).get("head") or ""
        print(f"- {entry['name']} [{entry['decision']}]")
        print(f"  location: {location}")
        if tip:
            print(f"  tip: {tip[:12]}")
        print(f"  reason: {entry['reason']}")
        print(f"  clean: {entry.get('clean')}")
        print(f"  {format_pr(entry.get('pr'))}")
        for action in entry.get("actions", []):
            print(f"  action: {action}")
        for error in entry.get("errors", []):
            print(f"  error: {error}")
        submodule_cleanup = entry.get("submodule_cleanup")
        if submodule_cleanup:
            print(
                "  submodule cleanup: "
                f"required={submodule_cleanup['required']}, "
                f"safe={submodule_cleanup['safe']}, "
                f"admin repos={submodule_cleanup['admin_repo_count']}"
            )
        if entry["decision"] == "kept" and entry.get("clean") is False:
            for line in format_dirty(entry.get("dirty")):
                print(f"  {line}")
        if entry.get("base_diff") and (entry.get("pr") or {}).get("status") in {"no_pr", "lookup_error"}:
            for line in format_diff(entry.get("base_diff")):
                print(f"  {line}")
        print()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit local Git branches/worktrees and delete clean branches whose GitHub PRs are merged "
            "or whose no-PR tips are already contained in the base."
        )
    )
    parser.add_argument("repo", help="Path inside the Git repository to audit.")
    parser.add_argument("--base", default="origin/main", help="Base ref for no-PR diff summaries.")
    parser.add_argument("--remote", default="origin", help="Remote to fetch before auditing.")
    parser.add_argument("--no-fetch", action="store_true", help="Skip git fetch --prune.")
    parser.add_argument("--dry-run", action="store_true", help="Report deletions without performing them.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable text.")
    parser.add_argument("--max-files", type=int, default=12, help="Maximum filenames to show per summary.")
    parser.add_argument(
        "--protect",
        action="append",
        default=[],
        help="Additional branch name to protect from deletion. May be repeated.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    repo = normalize_repo(args.repo)
    notes: list[str] = []
    fetch_error = ""

    if not args.no_fetch:
        proc = run(["git", "-C", str(repo), "fetch", "--prune", args.remote], check=False)
        if proc.returncode != 0:
            fetch_error = (proc.stderr or proc.stdout).strip()

    base_ref, base_notes = resolve_base(repo, args.base)
    notes.extend(base_notes)
    protected = set(PROTECTED_BRANCHES)
    protected.update(args.protect)
    if base_ref and "/" not in base_ref:
        protected.add(base_ref)

    entries = build_entries(
        repo,
        base_ref=base_ref,
        max_files=max(args.max_files, 0),
        protected=protected,
        allow_no_pr_deletion=not bool(fetch_error),
        dry_run=args.dry_run,
    )

    report = {
        "repo": str(repo),
        "base": base_ref,
        "remote": args.remote,
        "dry_run": args.dry_run,
        "fetch_error": fetch_error,
        "notes": notes,
        "entries": [entry_to_dict(entry) for entry in entries],
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_human_report(report)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
