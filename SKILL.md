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

Treat a local branch/worktree as auto-deletable only when all of these are true:

- The branch is not a protected base branch such as `main`, `master`, `develop`, `dev`, or `trunk`.
- A GitHub PR for the local branch is found and the PR is merged.
- The local branch tip matches the merged PR head SHA when GitHub provides it.
- The associated worktree is clean: no staged changes, unstaged tracked changes, or untracked files.
- The branch is not detached and is not checked out in the primary worktree.

The script removes local linked worktrees with `git worktree remove` and removes local branches with `git branch -D`. It never deletes remote branches.

If a merged PR exists but the worktree is dirty, do not delete. Report the dirty summary from the script.

If no GitHub PR is found, do not delete. Report the script's diff summary against the configured base, defaulting to `origin/main`.

If PR lookup fails because `gh` is unavailable or unauthenticated, do not delete. Report the lookup failure and any diff summary available.

## Workflow

1. Run the script against the repo path the user supplied.
2. If `git fetch --prune origin` fails due to a GitHub SSH/authentication problem, follow the repo or user `AGENTS.md` instructions for repairing SSH agent state, then retry once.
3. Read the report and relay:
   - branches/worktrees deleted
   - branches/worktrees kept because they were dirty
   - branches without PRs and their `origin/main` diff summary
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
- Dirty merged PR entries: staged/unstaged/untracked shortstat and sample filenames.
- No-PR entries: ahead/behind count and shortstat versus `origin/main`.
- Kept entries: explicit reason such as open PR, closed-unmerged PR, protected branch, detached worktree, PR lookup error, primary worktree, or PR head mismatch.
