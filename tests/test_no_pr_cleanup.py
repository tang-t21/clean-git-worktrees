from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "clean_git_worktrees.py"
SPEC = importlib.util.spec_from_file_location("clean_git_worktrees_no_pr", SCRIPT)
assert SPEC is not None
assert SPEC.loader is not None
CLEANER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CLEANER
SPEC.loader.exec_module(CLEANER)


def run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout


class NoPullRequestCleanupTest(unittest.TestCase):
    def entry(self, *, ahead: int | None) -> CLEANER.Entry:
        base_diff = {
            "base": "origin/main",
            "ahead_behind": None if ahead is None else {"ahead": ahead, "behind": 2},
        }
        return CLEANER.Entry(
            kind="branch",
            name="topic",
            branch=CLEANER.Branch(name="topic", tip="a" * 40),
            clean=True,
            pr={"status": "no_pr"},
            base_diff=base_diff,
        )

    def test_no_pr_tip_contained_in_base_is_deletable(self) -> None:
        can_delete, reason = CLEANER.deletion_decision(
            self.entry(ahead=0),
            protected=set(),
            allow_no_pr_deletion=True,
        )

        self.assertTrue(can_delete)
        self.assertIn("contained in origin/main", reason)

    def test_no_pr_branch_with_unique_commit_is_kept(self) -> None:
        can_delete, reason = CLEANER.deletion_decision(
            self.entry(ahead=1),
            protected=set(),
            allow_no_pr_deletion=True,
        )

        self.assertFalse(can_delete)
        self.assertEqual(reason, "no GitHub PR and branch has commits not in base")

    def test_no_pr_branch_with_unavailable_comparison_is_kept(self) -> None:
        can_delete, reason = CLEANER.deletion_decision(
            self.entry(ahead=None),
            protected=set(),
            allow_no_pr_deletion=True,
        )

        self.assertFalse(can_delete)
        self.assertEqual(reason, "no GitHub PR and base comparison is unavailable")

    def test_no_pr_branch_is_kept_after_fetch_failure(self) -> None:
        can_delete, reason = CLEANER.deletion_decision(
            self.entry(ahead=0),
            protected=set(),
            allow_no_pr_deletion=False,
        )

        self.assertFalse(can_delete)
        self.assertEqual(reason, "no GitHub PR and base freshness is unverified")

    def test_pr_lookup_error_does_not_use_no_pr_policy(self) -> None:
        entry = self.entry(ahead=0)
        entry.pr = {"status": "lookup_error", "error": "offline"}

        can_delete, reason = CLEANER.deletion_decision(
            entry,
            protected=set(),
            allow_no_pr_deletion=True,
        )

        self.assertFalse(can_delete)
        self.assertEqual(reason, "PR lookup error")

    def test_dry_run_deletes_no_pr_branch_already_contained_in_base(self) -> None:
        with tempfile.TemporaryDirectory(prefix="clean-no-pr-test-") as tempdir:
            repo = Path(tempdir) / "repo"
            run_git(Path(tempdir), "init", "-q", "-b", "main", str(repo))
            run_git(repo, "config", "user.name", "Test")
            run_git(repo, "config", "user.email", "test@example.invalid")
            run_git(repo, "commit", "-q", "--allow-empty", "-m", "initial")
            run_git(repo, "branch", "absorbed")
            run_git(repo, "commit", "-q", "--allow-empty", "-m", "main advances")

            with mock.patch.object(CLEANER, "lookup_pr", return_value={"status": "no_pr"}):
                entries = CLEANER.build_entries(
                    repo,
                    base_ref="main",
                    max_files=3,
                    protected={"main"},
                    allow_no_pr_deletion=True,
                    dry_run=True,
                )

            absorbed = next(entry for entry in entries if entry.name == "absorbed")
            self.assertEqual(absorbed.decision, "would_delete")
            self.assertEqual(absorbed.base_diff["ahead_behind"]["ahead"], 0)
            self.assertTrue(CLEANER.git_ok(repo, "show-ref", "--verify", "refs/heads/absorbed"))


if __name__ == "__main__":
    unittest.main()
