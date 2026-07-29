# Clean Git Worktrees

A Codex skill for auditing local Git branches and linked worktrees, then
deleting clean entries whose GitHub pull requests are merged or whose no-PR
branch tips are already fully contained in the configured base.

The bundled Python script checks protected branches, pull request state, PR
head SHA, no-PR branch containment, worktree cleanliness, and whether a branch
is checked out in the primary worktree. When Git refuses to remove a linked
worktree because of submodule metadata, the script audits recursive submodule
dirtiness and remote recoverability before deinitializing submodules and using
force removal. It never deletes remote branches.

## Install

Clone the repository into your Codex skills directory:

```bash
git clone https://github.com/tang-t21/clean-git-worktrees.git \
  "${CODEX_HOME:-$HOME/.codex}/skills/clean-git-worktrees"
```

The skill requires Python 3, Git, and an authenticated GitHub CLI:

```bash
gh auth login
```

## Use

Ask Codex to use `$clean-git-worktrees` against a local repository, or invoke
the script directly from this repository:

```bash
python3 scripts/clean_git_worktrees.py /path/to/repo --dry-run
```

Remove `--dry-run` only when you want the script to delete entries that meet
all safety conditions.

See [SKILL.md](SKILL.md) for the complete policy, workflow, and options.
