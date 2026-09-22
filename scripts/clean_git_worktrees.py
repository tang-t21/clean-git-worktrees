#!/usr/bin/env python3
"""List local worktrees, delete obvious safe cases, and report the rest."""

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

BASE_CANDIDATES = ("origin/main", "main")
PROTECTED_BRANCHES = {"main", "master", "develop", "dev", "trunk"}
PR_FIELDS = (
    "number,title,state,mergedAt,url,headRefName,headRefOid,baseRefName,updatedAt"
)
REPORT_LIMIT = 20
COMMAND_TIMEOUT_SECONDS = 60
SUBMODULE_WORKTREE_ERROR = (
    "working trees containing submodules cannot be moved or removed"
)


@dataclass
class Worktree:
    path: str
    head: str | None = None
    branch: str | None = None
    detached: bool = False
    bare: bool = False
    primary: bool = False
    locked: str | None = None
    prunable: str | None = None


@dataclass
class Entry:
    worktree: Worktree
    clean: bool | None = None
    dirty: dict[str, Any] | None = None
    pr: dict[str, Any] | None = None
    base_diff: dict[str, Any] | None = None
    submodule_cleanup: dict[str, Any] | None = None
    decision: str = "review"
    reason: str = ""
    actions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class CommandError(RuntimeError):
    def __init__(self, cmd: list[str], proc: subprocess.CompletedProcess[str]):
        message = (
            proc.stderr.strip()
            or proc.stdout.strip()
            or f"command failed: {' '.join(cmd)}"
        )
        super().__init__(message)
        self.cmd = cmd
        self.returncode = proc.returncode


def run(
    cmd: list[str],
    *,
    cwd: str | Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
    timeout: float | None = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        detail = f"command timed out after {timeout:g} seconds: {' '.join(cmd)}"
        proc = subprocess.CompletedProcess(
            cmd,
            124,
            exc.stdout if isinstance(exc.stdout, str) else "",
            "\n".join(part for part in (stderr.strip(), detail) if part),
        )
    if check and proc.returncode != 0:
        raise CommandError(cmd, proc)
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
    output = git(repo, "worktree", "list", "--porcelain")
    worktrees: list[Worktree] = []
    current: Worktree | None = None

    def finish() -> None:
        nonlocal current
        if current is not None:
            current.primary = not worktrees
            worktrees.append(current)
            current = None

    for raw in output.splitlines():
        if not raw:
            finish()
            continue
        if raw.startswith("worktree "):
            finish()
            current = Worktree(path=raw.removeprefix("worktree "))
        elif current is None:
            continue
        elif raw.startswith("HEAD "):
            current.head = raw.removeprefix("HEAD ")
        elif raw.startswith("branch "):
            current.branch = raw.removeprefix("branch ").removeprefix("refs/heads/")
        elif raw == "detached":
            current.detached = True
        elif raw == "bare":
            current.bare = True
        elif raw == "locked" or raw.startswith("locked "):
            current.locked = raw.removeprefix("locked ") or "locked"
        elif raw == "prunable" or raw.startswith("prunable "):
            current.prunable = raw.removeprefix("prunable ") or "prunable"
    finish()
    return worktrees


def parse_status(lines: list[str]) -> dict[str, Any]:
    counts = {"staged": 0, "unstaged": 0, "untracked": 0}
    files: list[str] = []
    for line in lines:
        if not line:
            continue
        code = line[:2]
        files.append(line)
        if code == "??":
            counts["untracked"] += 1
            continue
        if code[0] != " ":
            counts["staged"] += 1
        if code[1] != " ":
            counts["unstaged"] += 1
    return {
        "counts": {key: value for key, value in counts.items() if value},
        "file_count": len(files),
        "files": files[:REPORT_LIMIT],
        "truncated": len(files) > REPORT_LIMIT,
    }


def worktree_status(worktree: Worktree) -> tuple[bool | None, dict[str, Any]]:
    if worktree.prunable or not Path(worktree.path).is_dir():
        return None, {"error": worktree.prunable or "worktree path is missing"}
    proc = run(
        [
            "git",
            "-C",
            worktree.path,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        check=False,
    )
    if proc.returncode != 0:
        return None, {"error": (proc.stderr or proc.stdout).strip()}
    lines = proc.stdout.splitlines()
    return not lines, parse_status(lines)


def _git_admin(git_dir: Path, *args: str, check: bool = True) -> str:
    return run(
        ["git", f"--git-dir={git_dir}", "--work-tree=/", *args],
        check=check,
    ).stdout


def _find_submodule_admin_repos(modules_root: Path) -> list[Path]:
    repositories: list[Path] = []

    def visit(path: Path) -> None:
        if not path.is_dir():
            return
        if (
            (path / "HEAD").is_file()
            and (path / "config").is_file()
            and (path / "objects").is_dir()
        ):
            repositories.append(path)
            visit(path / "modules")
            return
        for child in sorted(path.iterdir()):
            if child.is_dir():
                visit(child)

    visit(modules_root)
    return repositories


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
    containing_refs = _git_admin(
        admin_repo,
        "for-each-ref",
        "--contains",
        oid,
        "--format=%(objectname)",
        "refs/remotes",
        "refs/tags",
    ).splitlines()
    return any(ref_oid in remote_oids for ref_oid in containing_refs)


def _audit_submodule_admin_repo(
    admin_repo: Path, display_path: str
) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    head = _git_admin(admin_repo, "rev-parse", "--verify", "HEAD").strip()
    refs = _parse_refs(
        _git_admin(
            admin_repo,
            "for-each-ref",
            "--format=%(objectname)%00%(refname)",
            "refs",
        )
    )
    local_heads = [
        (oid, refname) for oid, refname in refs if refname.startswith("refs/heads/")
    ]
    local_tags = [
        (oid, refname) for oid, refname in refs if refname.startswith("refs/tags/")
    ]
    unsupported_refs = [
        refname
        for _, refname in refs
        if not refname.startswith(("refs/heads/", "refs/remotes/", "refs/tags/"))
    ]
    if unsupported_refs:
        issues.append(
            f"{display_path}: unsupported local refs: {', '.join(unsupported_refs)}"
        )

    remote_refs, remote_errors = _advertised_refs(admin_repo)
    issues.extend(f"{display_path}: {error}" for error in remote_errors)
    remote_oids = {oid for oids in remote_refs.values() for oid in oids}

    critical_commits = {head, *(oid for oid, _ in local_heads)}
    for oid in sorted(critical_commits):
        if not _commit_recoverable(admin_repo, oid, remote_oids):
            issues.append(
                f"{display_path}: commit {oid} is not recoverable from an "
                "advertised remote ref"
            )

    for oid, refname in local_tags:
        if oid not in remote_refs.get(refname, set()):
            issues.append(
                f"{display_path}: local tag {refname} is not preserved by a "
                "matching remote tag"
            )

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


def audit_submodule_force_removal(worktree_path: str) -> dict[str, Any]:
    git_dir = Path(git(worktree_path, "rev-parse", "--absolute-git-dir").strip())
    modules_root = git_dir / "modules"
    required = modules_root.is_dir() and any(modules_root.iterdir())
    result: dict[str, Any] = {
        "required": required,
        "safe": True,
        "admin_git_dir": str(git_dir),
        "admin_repo_count": 0,
        "initialized_submodule_count": 0,
        "repositories": [],
        "issues": [],
    }

    root_status = git(
        worktree_path, "status", "--porcelain=v1", "--untracked-files=all"
    )
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

    initialized_submodules = git(
        worktree_path,
        "submodule",
        "foreach",
        "--quiet",
        "--recursive",
        'printf "%s\\n" "$displaypath"',
    ).splitlines()
    result["initialized_submodule_count"] = len(initialized_submodules)

    if required:
        admin_repos = _find_submodule_admin_repos(modules_root)
        result["admin_repo_count"] = len(admin_repos)
        if not admin_repos:
            result["issues"].append(
                "submodule metadata exists but no admin repository was auditable"
            )
        if len(initialized_submodules) > len(admin_repos):
            result["issues"].append(
                "not every initialized submodule has an auditable admin repository"
            )
        for admin_repo in admin_repos:
            display_path = str(admin_repo.relative_to(git_dir))
            try:
                summary, issues = _audit_submodule_admin_repo(admin_repo, display_path)
            except (CommandError, OSError) as exc:
                result["issues"].append(
                    f"{display_path}: unable to audit submodule admin repo: {exc}"
                )
                continue
            result["repositories"].append(summary)
            result["issues"].extend(issues)

    result["safe"] = not result["issues"]
    return result


def _is_only_submodule_worktree_error(message: str) -> bool:
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    if len(lines) != 1:
        return False
    normalized = lines[0].removeprefix("fatal:").strip()
    return normalized == SUBMODULE_WORKTREE_ERROR


def resolve_base(repo: Path) -> str | None:
    for ref in BASE_CANDIDATES:
        if git_ok(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
            return ref
    return None


def ahead_behind(repo: Path, base: str, head: str) -> dict[str, int] | None:
    proc = run(
        [
            "git",
            "-C",
            str(repo),
            "rev-list",
            "--left-right",
            "--count",
            f"{base}...{head}",
        ],
        check=False,
    )
    parts = proc.stdout.split()
    if proc.returncode != 0 or len(parts) != 2:
        return None
    try:
        return {"behind": int(parts[0]), "ahead": int(parts[1])}
    except ValueError:
        return None


def base_diff(repo: Path, base: str | None, head: str | None) -> dict[str, Any]:
    if base is None:
        return {"base": None, "error": "main is unavailable"}
    if head is None:
        return {"base": base, "error": "worktree HEAD is unavailable"}

    merge_base = run(["git", "-C", str(repo), "merge-base", base, head], check=False)
    if merge_base.returncode != 0:
        return {"base": base, "error": (merge_base.stderr or merge_base.stdout).strip()}
    start = merge_base.stdout.strip()
    name_status = git(
        repo, "diff", "--name-status", "--find-renames", f"{start}..{head}"
    ).splitlines()
    shortstat = " ".join(git(repo, "diff", "--shortstat", f"{start}..{head}").split())
    commits = git(
        repo, "log", "--format=%h %s", "--no-merges", f"{base}..{head}"
    ).splitlines()
    return {
        "base": base,
        "merge_base": start,
        "ahead_behind": ahead_behind(repo, base, head),
        "shortstat": shortstat,
        "files": name_status[:REPORT_LIMIT],
        "file_count": len(name_status),
        "commits": commits[:REPORT_LIMIT],
        "commit_count": len(commits),
        "truncated": len(name_status) > REPORT_LIMIT or len(commits) > REPORT_LIMIT,
    }


def discover_github_repositories(
    repo: Path,
) -> tuple[list[tuple[str, str]], str | None]:
    if shutil.which("gh") is None:
        return [], "gh CLI not found"
    proc = run(
        ["gh", "repo", "view", "--json", "nameWithOwner,parent"], cwd=repo, check=False
    )
    if proc.returncode != 0:
        return [], (proc.stderr or proc.stdout).strip()
    try:
        data = json.loads(proc.stdout)
        origin = data["nameWithOwner"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return [], f"unable to identify GitHub repository: {exc}"

    owner = origin.split("/", 1)[0]
    targets = [(origin, "local")]
    parent = data.get("parent") or {}
    parent_name = parent.get("nameWithOwner")
    if parent_name and parent_name != origin:
        targets.append((parent_name, owner))
    return targets, None


def classify_prs(prs: list[dict[str, Any]]) -> dict[str, Any]:
    if not prs:
        return {"status": "no_pr"}

    def newest(items: list[dict[str, Any]]) -> dict[str, Any]:
        return max(
            items, key=lambda item: item.get("updatedAt") or item.get("mergedAt") or ""
        )

    open_prs = [
        pr
        for pr in prs
        if (pr.get("state") or "").upper() == "OPEN" and not pr.get("mergedAt")
    ]
    merged_prs = [
        pr
        for pr in prs
        if pr.get("mergedAt") or (pr.get("state") or "").upper() == "MERGED"
    ]
    if open_prs:
        primary = newest(open_prs)
        status = "open"
    elif merged_prs:
        primary = newest(merged_prs)
        status = "merged"
    else:
        primary = newest(prs)
        status = "closed"
    return {**primary, "status": status, "match_count": len(prs)}


def lookup_pr(
    repo: Path,
    branch: str | None,
    repositories: list[tuple[str, str]],
    discovery_error: str | None,
) -> dict[str, Any]:
    if branch is None:
        return {"status": "not_applicable"}
    if discovery_error:
        return {"status": "lookup_error", "error": discovery_error}

    matches: list[dict[str, Any]] = []
    errors: list[str] = []
    for repository, owner_mode in repositories:
        head = branch if owner_mode == "local" else f"{owner_mode}:{branch}"
        proc = run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repository,
                "--state",
                "all",
                "--head",
                head,
                "--limit",
                "50",
                "--json",
                PR_FIELDS,
            ],
            check=False,
        )
        if proc.returncode != 0:
            errors.append(f"{repository}: {(proc.stderr or proc.stdout).strip()}")
            continue
        try:
            found = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            errors.append(f"{repository}: invalid gh output: {exc}")
            continue
        for pr in found:
            pr["repository"] = repository
            matches.append(pr)

    if errors:
        return {"status": "lookup_error", "error": "; ".join(errors)}
    unique = {
        pr.get("url") or f"{pr.get('repository')}#{pr.get('number')}": pr
        for pr in matches
    }
    return classify_prs(list(unique.values()))


def deletion_decision(entry: Entry, *, fetch_ok: bool) -> tuple[bool, str]:
    worktree = entry.worktree
    blockers: list[str] = []
    if worktree.primary:
        blockers.append("primary worktree")
    if worktree.branch in PROTECTED_BRANCHES:
        blockers.append("protected branch")
    if worktree.bare:
        blockers.append("bare worktree")
    if worktree.locked:
        blockers.append(f"locked: {worktree.locked}")
    if worktree.prunable:
        blockers.append(f"stale metadata: {worktree.prunable}")
    if entry.clean is None:
        blockers.append("worktree state is unavailable")

    pr = entry.pr or {"status": "lookup_error", "error": "PR was not checked"}
    status = pr.get("status")
    if status == "lookup_error":
        blockers.append("PR lookup failed")
    elif status == "open":
        blockers.append("active PR is not merged")
    if entry.clean is False:
        blockers.append("dirty worktree")
    if blockers:
        return False, "; ".join(dict.fromkeys(blockers))

    if status == "merged" and pr.get("headRefOid") == worktree.head:
        return True, "merged PR, matching HEAD, and clean worktree"

    if not fetch_ok:
        return False, "origin fetch failed; main comparison is not fresh"
    diff = entry.base_diff or {}
    counts = diff.get("ahead_behind")
    if not counts:
        return False, "main comparison is unavailable"
    if counts["ahead"] == 0:
        return True, f"no unique commits versus {diff.get('base')} and clean worktree"
    if status == "merged":
        return False, "worktree has commits beyond the merged PR"
    if status == "closed":
        return False, "closed-unmerged PR and worktree has unique commits"
    return False, "no active PR and worktree has unique commits"


def build_entries(
    repo: Path,
    *,
    base: str | None,
    fetch_ok: bool,
    repositories: list[tuple[str, str]],
    discovery_error: str | None,
) -> list[Entry]:
    entries: list[Entry] = []
    for worktree in parse_worktrees(repo):
        clean, dirty = worktree_status(worktree)
        entry = Entry(
            worktree=worktree,
            clean=clean,
            dirty=dirty,
            pr=lookup_pr(repo, worktree.branch, repositories, discovery_error),
            base_diff=base_diff(repo, base, worktree.head),
        )
        can_delete, reason = deletion_decision(entry, fetch_ok=fetch_ok)
        entry.decision = "delete" if can_delete else "review"
        entry.reason = reason
        entries.append(entry)
    return entries


def revalidate_history(
    repo: Path, entry: Entry, base: str | None, fetch_ok: bool
) -> bool:
    worktree = entry.worktree
    pr = entry.pr or {}
    if pr.get("status") == "merged" and pr.get("headRefOid") == worktree.head:
        return True
    return bool(
        fetch_ok
        and base
        and worktree.head
        and git_ok(repo, "merge-base", "--is-ancestor", worktree.head, base)
    )


def delete_entry(
    repo: Path, entry: Entry, *, base: str | None, fetch_ok: bool, dry_run: bool
) -> None:
    worktree = entry.worktree
    if entry.decision != "delete":
        return
    if dry_run:
        try:
            entry.submodule_cleanup = audit_submodule_force_removal(worktree.path)
        except (CommandError, OSError) as exc:
            entry.decision = "review"
            entry.reason = "unable to audit submodule state"
            entry.errors.append(str(exc))
            return
        if entry.submodule_cleanup["required"]:
            if not (
                entry.submodule_cleanup["safe"]
                and entry.submodule_cleanup["admin_repo_count"] > 0
            ):
                entry.decision = "review"
                entry.reason = "submodule safety check failed"
                entry.errors.extend(entry.submodule_cleanup["issues"])
                return
            entry.actions.append(f"would deinitialize submodules in {worktree.path}")
            entry.actions.append(
                f"would force-remove worktree {worktree.path} after safe submodule audit"
            )
        else:
            entry.actions.append(f"would remove worktree {worktree.path}")
        entry.decision = "would_delete"
        if worktree.branch:
            entry.actions.append(f"would delete local branch {worktree.branch}")
        return

    clean_now, dirty_now = worktree_status(worktree)
    if clean_now is not True:
        entry.decision = "review"
        entry.reason = "worktree changed or became unavailable before deletion"
        entry.dirty = dirty_now
        return
    current_head = git(worktree.path, "rev-parse", "HEAD").strip()
    if current_head != worktree.head:
        entry.decision = "review"
        entry.reason = f"HEAD changed during audit: {worktree.head} -> {current_head}"
        return
    if not revalidate_history(repo, entry, base, fetch_ok):
        entry.decision = "review"
        entry.reason = "history safety check changed during audit"
        return

    proc = run(
        ["git", "-C", str(repo), "worktree", "remove", worktree.path],
        check=False,
        timeout=None,
    )
    if proc.returncode != 0:
        remove_error = "\n".join(
            output.strip() for output in (proc.stderr, proc.stdout) if output.strip()
        )
        if not _is_only_submodule_worktree_error(remove_error):
            entry.decision = "review"
            entry.reason = "automatic worktree removal failed"
            entry.errors.append(remove_error)
            return
        try:
            entry.submodule_cleanup = audit_submodule_force_removal(worktree.path)
        except (CommandError, OSError) as exc:
            entry.decision = "review"
            entry.reason = "unable to audit submodule state"
            entry.errors.append(str(exc))
            return
        if not (
            entry.submodule_cleanup["safe"]
            and entry.submodule_cleanup["required"]
            and entry.submodule_cleanup["admin_repo_count"] > 0
        ):
            entry.decision = "review"
            entry.reason = "submodule safety check failed"
            issues = entry.submodule_cleanup["issues"] or [
                "submodule removal was requested without auditable admin metadata"
            ]
            entry.errors.extend(issues)
            return
        deinit = run(
            ["git", "-C", worktree.path, "submodule", "deinit", "--all"],
            check=False,
            timeout=None,
        )
        if deinit.returncode != 0:
            entry.decision = "review"
            entry.reason = "submodule deinitialization failed"
            entry.errors.append((deinit.stderr or deinit.stdout).strip())
            return
        entry.actions.append(f"deinitialized submodules in {worktree.path}")

        clean_after_deinit, dirty_after_deinit = worktree_status(worktree)
        if clean_after_deinit is not True:
            entry.decision = "review"
            entry.reason = "worktree changed after submodule deinitialization"
            entry.dirty = dirty_after_deinit
            return
        head_after_deinit = git(worktree.path, "rev-parse", "HEAD").strip()
        if head_after_deinit != worktree.head:
            entry.decision = "review"
            entry.reason = (
                f"HEAD changed during submodule cleanup: {worktree.head} -> "
                f"{head_after_deinit}"
            )
            return
        forced = run(
            [
                "git",
                "-C",
                str(repo),
                "worktree",
                "remove",
                "--force",
                worktree.path,
            ],
            check=False,
            timeout=None,
        )
        if forced.returncode != 0:
            entry.decision = "review"
            entry.reason = "worktree removal failed after safe submodule cleanup"
            entry.errors.append((forced.stderr or forced.stdout).strip())
            return
        entry.actions.append(
            f"force-removed worktree {worktree.path} after safe submodule audit"
        )
    else:
        entry.actions.append(f"removed worktree {worktree.path}")
    entry.decision = "deleted"

    if not worktree.branch:
        return
    ref = f"refs/heads/{worktree.branch}"
    current_tip = run(
        ["git", "-C", str(repo), "rev-parse", "--verify", ref], check=False
    )
    if current_tip.returncode != 0 or current_tip.stdout.strip() != worktree.head:
        entry.errors.append(
            f"kept local branch {worktree.branch}: branch tip changed or is unavailable"
        )
        return
    branch_delete = run(
        ["git", "-C", str(repo), "branch", "-D", worktree.branch], check=False
    )
    if branch_delete.returncode != 0:
        entry.errors.append(
            f"kept local branch {worktree.branch}: {(branch_delete.stderr or branch_delete.stdout).strip()}"
        )
        return
    entry.actions.append(f"deleted local branch {worktree.branch}")


def entry_dict(entry: Entry) -> dict[str, Any]:
    return {
        "worktree": entry.worktree.__dict__,
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
        return "PR: lookup unavailable"
    status = pr.get("status")
    if status == "not_applicable":
        return "PR: n/a (detached)"
    if status == "lookup_error":
        return f"PR: lookup error ({pr.get('error')})"
    if status == "no_pr":
        return "PR: none"
    return (
        f"PR: {status} {pr.get('repository', '')}#{pr.get('number')} "
        f"{pr.get('title', '')} {pr.get('url', '')}"
    ).strip()


def print_report(report: dict[str, Any]) -> None:
    print(f"Repository: {report['repo']}")
    print(f"Base: {report.get('base') or 'unavailable'}")
    print(f"Mode: {'dry-run' if report['dry_run'] else 'delete-safe-worktrees'}")
    if report.get("fetch_error"):
        print(f"Fetch error: {report['fetch_error']}")
    print()

    counts: dict[str, int] = {}
    for entry in report["entries"]:
        counts[entry["decision"]] = counts.get(entry["decision"], 0) + 1
    print(
        "Summary: "
        + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    )
    print()

    for entry in report["entries"]:
        worktree = entry["worktree"]
        branch = worktree.get("branch") or "(detached)"
        print(f"- {worktree['path']} [{entry['decision']}]")
        print(f"  branch: {branch}")
        print(f"  HEAD: {(worktree.get('head') or 'unknown')[:12]}")
        print(f"  reason: {entry['reason']}")
        print(f"  clean: {entry.get('clean')}")
        if worktree.get("locked"):
            print(f"  locked: {worktree['locked']}")
        if worktree.get("prunable"):
            print(f"  stale: {worktree['prunable']}")
        print(f"  {format_pr(entry.get('pr'))}")

        diff = entry.get("base_diff") or {}
        if diff.get("error"):
            print(f"  main diff: {diff['error']}")
        else:
            counts_pair = diff.get("ahead_behind") or {}
            print(
                f"  vs {diff.get('base')}: ahead {counts_pair.get('ahead', '?')}, "
                f"behind {counts_pair.get('behind', '?')}, {diff.get('shortstat') or 'no file changes'}"
            )
            for commit in diff.get("commits") or []:
                print(f"  commit: {commit}")
            for changed in diff.get("files") or []:
                print(f"  changed: {changed}")

        dirty = entry.get("dirty") or {}
        if dirty.get("error"):
            print(f"  worktree state: {dirty['error']}")
        for changed in dirty.get("files") or []:
            print(f"  workspace: {changed}")
        for action in entry.get("actions") or []:
            print(f"  action: {action}")
        for error in entry.get("errors") or []:
            print(f"  error: {error}")
        submodule_cleanup = entry.get("submodule_cleanup")
        if submodule_cleanup:
            print(
                "  submodule cleanup: "
                f"required={submodule_cleanup['required']}, "
                f"safe={submodule_cleanup['safe']}, "
                f"admin repos={submodule_cleanup['admin_repo_count']}, "
                "initialized="
                f"{submodule_cleanup['initialized_submodule_count']}"
            )
        print()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="Path inside the Git repository to clean.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List decisions without deleting anything.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON for Codex to summarize."
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    repo = normalize_repo(args.repo)
    fetch = run(["git", "-C", str(repo), "fetch", "--prune", "origin"], check=False)
    fetch_error = (
        "" if fetch.returncode == 0 else (fetch.stderr or fetch.stdout).strip()
    )
    base = resolve_base(repo)
    repositories, discovery_error = discover_github_repositories(repo)
    entries = build_entries(
        repo,
        base=base,
        fetch_ok=not fetch_error,
        repositories=repositories,
        discovery_error=discovery_error,
    )
    for entry in entries:
        delete_entry(
            repo, entry, base=base, fetch_ok=not fetch_error, dry_run=args.dry_run
        )

    report = {
        "repo": str(repo),
        "base": base,
        "dry_run": args.dry_run,
        "fetch_error": fetch_error,
        "entries": [entry_dict(entry) for entry in entries],
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except CommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
