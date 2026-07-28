from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "clean_git_worktrees.py"
SPEC = importlib.util.spec_from_file_location("clean_git_worktrees", SCRIPT)
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


class SubmoduleWorktreeCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="clean-worktree-test-")
        self.root = Path(self.tempdir.name)
        self.subrepo = self.root / "subrepo"
        self.superrepo = self.root / "superrepo"
        self.worktree = self.root / "linked"

        run_git(self.root, "init", "-q", str(self.subrepo))
        run_git(self.subrepo, "config", "user.name", "Test")
        run_git(self.subrepo, "config", "user.email", "test@example.invalid")
        run_git(self.subrepo, "commit", "-q", "--allow-empty", "-m", "initial")

        run_git(self.root, "init", "-q", str(self.superrepo))
        run_git(self.superrepo, "config", "user.name", "Test")
        run_git(self.superrepo, "config", "user.email", "test@example.invalid")
        run_git(self.superrepo, "commit", "-q", "--allow-empty", "-m", "initial")
        run_git(
            self.superrepo,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(self.subrepo),
            "deps/sub",
        )
        run_git(self.superrepo, "commit", "-q", "-am", "add submodule")
        run_git(self.superrepo, "worktree", "add", "-q", "-b", "merged-test", str(self.worktree))
        self.tip = run_git(self.worktree, "rev-parse", "HEAD").strip()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def entry(self) -> CLEANER.Entry:
        return CLEANER.Entry(
            kind="branch",
            name="merged-test",
            branch=CLEANER.Branch(name="merged-test", tip=self.tip),
            worktree=CLEANER.Worktree(
                path=str(self.worktree),
                head=self.tip,
                branch="merged-test",
            ),
            clean=True,
        )

    def initialize_submodule(self) -> Path:
        run_git(
            self.worktree,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "update",
            "--init",
        )
        return self.worktree / "deps" / "sub"

    def test_clean_recoverable_submodule_uses_safe_force_removal(self) -> None:
        self.initialize_submodule()
        entry = self.entry()

        CLEANER.delete_entry(self.superrepo, entry, dry_run=False)

        self.assertEqual(entry.decision, "deleted")
        self.assertFalse(self.worktree.exists())
        self.assertFalse(CLEANER.git_ok(self.superrepo, "show-ref", "--verify", "refs/heads/merged-test"))
        self.assertIsNotNone(entry.submodule_cleanup)
        self.assertTrue(entry.submodule_cleanup["safe"])
        self.assertTrue(any("force-removed worktree" in action for action in entry.actions))

    def test_dry_run_reports_force_removal_without_mutation(self) -> None:
        self.initialize_submodule()
        entry = self.entry()

        CLEANER.delete_entry(self.superrepo, entry, dry_run=True)

        self.assertEqual(entry.decision, "would_delete")
        self.assertTrue(self.worktree.exists())
        self.assertTrue(CLEANER.git_ok(self.superrepo, "show-ref", "--verify", "refs/heads/merged-test"))
        self.assertTrue(any("would force-remove worktree" in action for action in entry.actions))

    def test_dirty_initialized_submodule_blocks_force_removal(self) -> None:
        submodule = self.initialize_submodule()
        (submodule / "untracked.txt").write_text("preserve me\n")
        entry = self.entry()

        CLEANER.delete_entry(self.superrepo, entry, dry_run=False)

        self.assertEqual(entry.decision, "kept")
        self.assertEqual(entry.reason, "submodule safety check failed")
        self.assertTrue(self.worktree.exists())
        self.assertTrue(any("initialized submodule worktree is dirty" in error for error in entry.errors))

    def test_local_only_submodule_commit_blocks_force_removal(self) -> None:
        submodule = self.initialize_submodule()
        expected = run_git(submodule, "rev-parse", "HEAD").strip()
        run_git(submodule, "config", "user.name", "Test")
        run_git(submodule, "config", "user.email", "test@example.invalid")
        run_git(submodule, "checkout", "-q", "-b", "local-only")
        run_git(submodule, "commit", "-q", "--allow-empty", "-m", "local only")
        run_git(submodule, "checkout", "-q", "--detach", expected)
        entry = self.entry()

        CLEANER.delete_entry(self.superrepo, entry, dry_run=False)

        self.assertEqual(entry.decision, "kept")
        self.assertEqual(entry.reason, "submodule safety check failed")
        self.assertTrue(self.worktree.exists())
        self.assertTrue(CLEANER.git_ok(self.superrepo, "show-ref", "--verify", "refs/heads/merged-test"))
        self.assertTrue(any("not recoverable" in error for error in entry.errors))

    def test_empty_submodule_admin_metadata_is_safe(self) -> None:
        git_dir = Path(run_git(self.worktree, "rev-parse", "--absolute-git-dir").strip())
        (git_dir / "modules" / "deps").mkdir(parents=True)
        entry = self.entry()

        CLEANER.delete_entry(self.superrepo, entry, dry_run=False)

        self.assertEqual(entry.decision, "deleted")
        self.assertFalse(self.worktree.exists())
        self.assertIsNotNone(entry.submodule_cleanup)
        self.assertEqual(entry.submodule_cleanup["admin_repo_count"], 0)


if __name__ == "__main__":
    unittest.main()
