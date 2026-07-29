---
name: clean-git-worktrees
description: Audit and clean up a Git repository with many local feature branches and linked worktrees. Use when Codex is asked to organize, summarize, prune, delete, or safely clean local Git branches/worktrees by checking working tree cleanliness, GitHub PR merge state, and diffs against origin/main.
---

# Clean Git Worktrees

## Overview

Use this skill to inspect every local branch and linked worktree in a repo, delete only entries that are safe by policy, and summarize everything else for the user.

The bundled script is the source of truth for the audit and deletion sequence.

## Quick Start

Run:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/clean-git-worktrees/scripts/clean_git_worktrees.py" /path/to/repo
```

Use `--dry-run` when the user asks for preview-only output:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/clean-git-worktrees/scripts/clean_git_worktrees.py" /path/to/repo --dry-run
```

## Policy

Treat a local branch/worktree as auto-deletable only when all of these common conditions are true:

- The branch is not a protected base branch such as `main`, `master`, `develop`, `dev`, or `trunk`.
- The associated worktree is clean: no staged changes, unstaged tracked changes, or untracked files.
- The branch is not detached and is not checked out in the primary worktree.

Also require one of these history conditions:

- A GitHub PR for the local branch is merged, and the local branch tip matches the merged PR head SHA when GitHub provides it.
- GitHub confirms that no PR exists for the branch, the base comparison succeeds, and the local branch has zero commits ahead of the configured base. This means the complete branch tip is already reachable from the base, even when the branch is behind it.

Keep a no-PR branch when it has any commit not in the base, including an empty commit or a commit whose final tree happens to match the base. Keep it when the fetch failed or the base comparison is unavailable. A PR lookup error is not the same as a confirmed no-PR result.

The script removes local linked worktrees with `git worktree remove` and removes local branches with `git branch -D`. It never deletes remote branches.

If normal removal fails with `working trees containing submodules cannot be moved or removed`, the script may use `git worktree remove --force` only after the submodule safety audit below passes. Do not treat the presence of `.gitmodules` alone as requiring force removal.

If a merged PR exists but the worktree is dirty, do not delete. Report the dirty summary from the script.

If no GitHub PR is found and the branch has commits not in the base, do not delete. Report the script's diff summary against the configured base, defaulting to `origin/main`.

If PR lookup fails because `gh` is unavailable or unauthenticated, do not delete. Report the lookup failure and any diff summary available.

## Submodule Worktree Safety

Git 2.34 can reject removal when a linked worktree retains per-worktree submodule metadata under its absolute Git directory, even when `git submodule status` shows uninitialized entries.

When this happens, let the script perform this exact fallback:

1. Recheck the root worktree and every initialized recursive submodule for staged, unstaged, and untracked content.
2. Recursively inspect submodule Git repositories under `<absolute-git-dir>/modules`.
3. Refuse force removal if any repository has a stash, an unsupported local ref, a branch or detached `HEAD` commit not recoverable from a currently advertised remote ref, a local tag without a matching advertised remote tag, or an unavailable remote.
4. Run `git submodule deinit --all`.
5. Recheck that the root worktree is still clean.
6. Run `git worktree remove --force` and delete the local branch only after removal succeeds.

An empty administrative directory such as `modules/3rdparty` is safe when no submodule Git repositories or dirty checkouts are present. Warnings about an already-missing `core.worktree` are not sufficient success evidence; require a successful deinit exit, a clean recheck, successful worktree removal, and final path/branch absence.

In `--dry-run` mode, run the same read-only safety audit and report whether deinitialization and force removal would be used. Never manually delete `.git/worktrees/.../modules`.

## Workflow

1. Run the script against the repo path the user supplied.
2. If `git fetch --prune origin` fails due to a GitHub SSH/authentication problem, follow the repo or user `AGENTS.md` instructions for repairing SSH agent state, then retry once.
3. Read the report and relay:
   - branches/worktrees deleted
   - submodule cleanup audit, deinitialization, and force-removal actions
   - branches/worktrees kept because they were dirty
   - branches without PRs, their `origin/main` containment decision, and their diff summary
   - PR lookup errors, protected branches, detached worktrees, or primary-worktree branches that were intentionally kept
4. Do not manually run destructive Git commands outside the script unless the user explicitly asks for a narrower follow-up.

## Script Options

- `--dry-run`: print what would be deleted without deleting.
- `--base <ref>`: compare no-PR branches against another base ref instead of `origin/main`.
- `--remote <name>`: fetch another remote instead of `origin`.
- `--no-fetch`: skip the initial `git fetch --prune`.
- `--json`: emit machine-readable JSON.
- `--max-files <n>`: change how many filenames are included in summaries.

## Output Expectations

For each local branch/worktree, the report includes the local path if present, PR status, worktree cleanliness, deletion decision, and one focused summary:

- Deleted entries: deletion action and merged PR URL.
- Submodule entries: whether fallback was required, whether its audit was safe, and how many administrative repositories were checked.
- Dirty merged PR entries: staged/unstaged/untracked shortstat and sample filenames.
- No-PR entries: ahead/behind count and shortstat versus `origin/main`; delete only when `ahead` is zero and the common safety conditions pass.
- Kept entries: explicit reason such as open PR, closed-unmerged PR, protected branch, detached worktree, PR lookup error, primary worktree, or PR head mismatch.
