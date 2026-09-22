---
name: clean-git-worktrees
description: List every registered local Git worktree, delete only obvious safe cases, and explain what each retained worktree is doing from its PR, commits, main diff, and dirty files. Use when Codex is asked to audit, review, organize, or clean local Git worktrees.
---

# Clean Git Worktrees

Run the bundled script against the requested repository:

```bash
python3 /home/tangtian/.codex/skills/clean-git-worktrees/scripts/clean_git_worktrees.py /path/to/repo --json
```

The default mode performs safe deletions. Add `--dry-run` only when the user asks for a preview.

## Policy

Audit only entries from `git worktree list`; do not include standalone local branches.

For every worktree:

1. Fetch `origin` and compare with `origin/main`.
2. If its branch has an open PR, keep it.
3. If its PR is merged, delete only when the worktree is clean and its HEAD still matches the merged PR head.
4. Otherwise, including no PR, closed-unmerged PR, a changed post-merge HEAD, or detached HEAD, inspect commits and files unique to the worktree versus `origin/main`.
5. Delete only when the worktree is clean and has zero commits ahead of `origin/main`.
6. Keep dirty worktrees and every worktree with unique commits.

Always keep the primary worktree, protected base branches, locked worktrees, stale or inaccessible records, and entries whose PR or main comparison failed. A failure for one entry must not stop the remaining audit.

Use normal `git worktree remove` first. If Git refuses only because the worktree
contains initialized submodules, continue only after the script verifies that
the root and every initialized submodule are clean and that every submodule
HEAD, local branch, and tag is recoverable from an advertised remote ref. The
script may then deinitialize the submodules, recheck the root HEAD and clean
state, and use `git worktree remove --force` for that audited worktree. Keep the
worktree for review if any audit, remote lookup, deinitialization, or final
removal check fails. Never use force for another removal error and never prune
worktree metadata.

After successfully removing a branch-backed worktree, delete only its matching
local branch. Never delete a remote branch.

## Report

Return one list containing every worktree seen before deletion. For each item include:

- path, branch or detached state, and HEAD
- PR state, title, and URL when present
- clean, dirty, locked, stale, or inaccessible state
- deletion action or exact reason it needs review

For every retained item, describe in one concise sentence what the worktree is doing. Use its PR title when a PR is present, then inspect unique commit subjects, changed files versus `origin/main`, and dirty files for any work not covered by that PR. Do not return only counts or filenames without the purpose summary.

Do not perform unrelated branch cleanup, remote deletion, force removal, metadata pruning, process inspection, or artifact cleanup unless the user explicitly asks for it afterward.
