from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        capture_output=True,
    )
    return proc.stdout


class WorktreeParsingTest(unittest.TestCase):
    def test_parse_includes_stale_and_detached_worktrees(self) -> None:
        output = """worktree /repo
HEAD aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
branch refs/heads/main

worktree /linked
HEAD bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
branch refs/heads/topic

worktree /missing
HEAD cccccccccccccccccccccccccccccccccccccccc
detached
prunable gitdir file points to non-existent location

"""
        with mock.patch.object(CLEANER, "git", return_value=output):
            worktrees = CLEANER.parse_worktrees(Path("/repo"))

        self.assertEqual(len(worktrees), 3)
        self.assertTrue(worktrees[0].primary)
        self.assertEqual(worktrees[1].branch, "topic")
        self.assertTrue(worktrees[2].detached)
        self.assertIn("non-existent", worktrees[2].prunable)

    def test_stale_worktree_status_is_reported_without_git_call(self) -> None:
        worktree = CLEANER.Worktree(path="/missing", prunable="missing gitdir")
        with mock.patch.object(CLEANER, "run") as run_mock:
            clean, summary = CLEANER.worktree_status(worktree)

        self.assertIsNone(clean)
        self.assertEqual(summary["error"], "missing gitdir")
        run_mock.assert_not_called()


class CommandTimeoutTest(unittest.TestCase):
    def test_timeout_becomes_a_failed_command_result(self) -> None:
        expired = subprocess.TimeoutExpired(["git", "status"], timeout=0.01)
        with mock.patch.object(CLEANER.subprocess, "run", side_effect=expired):
            proc = CLEANER.run(["git", "status"], check=False, timeout=0.01)

        self.assertEqual(proc.returncode, 124)
        self.assertIn("command timed out after 0.01 seconds", proc.stderr)


class DecisionTest(unittest.TestCase):
    def entry(
        self,
        *,
        pr_status: str,
        ahead: int,
        clean: bool | None = True,
        primary: bool = False,
        detached: bool = False,
        prunable: str | None = None,
        head_ref_oid: str | None = None,
    ) -> CLEANER.Entry:
        head = "a" * 40
        worktree = CLEANER.Worktree(
            path="/linked",
            head=head,
            branch=None if detached else "topic",
            detached=detached,
            primary=primary,
            prunable=prunable,
        )
        pr = {"status": pr_status}
        if head_ref_oid is not None:
            pr["headRefOid"] = head_ref_oid
        return CLEANER.Entry(
            worktree=worktree,
            clean=clean,
            pr=pr,
            base_diff={
                "base": "origin/main",
                "ahead_behind": {"ahead": ahead, "behind": 2},
            },
        )

    def test_open_pr_is_kept(self) -> None:
        can_delete, reason = CLEANER.deletion_decision(
            self.entry(pr_status="open", ahead=0), fetch_ok=True
        )
        self.assertFalse(can_delete)
        self.assertIn("active PR", reason)

    def test_merged_matching_clean_worktree_is_deleted(self) -> None:
        entry = self.entry(pr_status="merged", ahead=4, head_ref_oid="a" * 40)
        can_delete, reason = CLEANER.deletion_decision(entry, fetch_ok=True)
        self.assertTrue(can_delete)
        self.assertIn("merged PR", reason)

    def test_merged_dirty_worktree_is_kept(self) -> None:
        entry = self.entry(
            pr_status="merged", ahead=0, clean=False, head_ref_oid="a" * 40
        )
        can_delete, reason = CLEANER.deletion_decision(entry, fetch_ok=True)
        self.assertFalse(can_delete)
        self.assertIn("dirty worktree", reason)

    def test_no_pr_unique_commits_are_kept(self) -> None:
        can_delete, reason = CLEANER.deletion_decision(
            self.entry(pr_status="no_pr", ahead=1), fetch_ok=True
        )
        self.assertFalse(can_delete)
        self.assertIn("unique commits", reason)

    def test_no_pr_contained_clean_worktree_is_deleted(self) -> None:
        can_delete, reason = CLEANER.deletion_decision(
            self.entry(pr_status="no_pr", ahead=0), fetch_ok=True
        )
        self.assertTrue(can_delete)
        self.assertIn("no unique commits", reason)

    def test_closed_pr_uses_main_containment(self) -> None:
        can_delete, _ = CLEANER.deletion_decision(
            self.entry(pr_status="closed", ahead=0), fetch_ok=True
        )
        self.assertTrue(can_delete)

    def test_detached_contained_clean_worktree_is_deleted(self) -> None:
        entry = self.entry(pr_status="not_applicable", ahead=0, detached=True)
        can_delete, _ = CLEANER.deletion_decision(entry, fetch_ok=True)
        self.assertTrue(can_delete)

    def test_primary_and_stale_worktrees_are_kept(self) -> None:
        primary, primary_reason = CLEANER.deletion_decision(
            self.entry(pr_status="no_pr", ahead=0, primary=True), fetch_ok=True
        )
        stale, stale_reason = CLEANER.deletion_decision(
            self.entry(pr_status="no_pr", ahead=0, clean=None, prunable="missing"),
            fetch_ok=True,
        )
        self.assertFalse(primary)
        self.assertIn("primary", primary_reason)
        self.assertFalse(stale)
        self.assertIn("stale metadata", stale_reason)


class EntryAndDeletionTest(unittest.TestCase):
    def test_build_entries_only_uses_registered_worktrees(self) -> None:
        worktrees = [
            CLEANER.Worktree(
                path="/primary", head="a" * 40, branch="main", primary=True
            ),
            CLEANER.Worktree(path="/linked", head="b" * 40, branch="topic"),
        ]
        with (
            mock.patch.object(CLEANER, "parse_worktrees", return_value=worktrees),
            mock.patch.object(
                CLEANER, "worktree_status", return_value=(True, {"files": []})
            ),
            mock.patch.object(CLEANER, "lookup_pr", return_value={"status": "no_pr"}),
            mock.patch.object(
                CLEANER,
                "base_diff",
                return_value={
                    "base": "origin/main",
                    "ahead_behind": {"ahead": 1, "behind": 0},
                },
            ),
        ):
            entries = CLEANER.build_entries(
                Path("/repo"),
                base="origin/main",
                fetch_ok=True,
                repositories=[],
                discovery_error=None,
            )

        self.assertEqual(
            [entry.worktree.path for entry in entries], ["/primary", "/linked"]
        )

    def test_normal_removal_deletes_worktree_and_matching_local_branch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="clean-worktree-") as tempdir:
            root = Path(tempdir)
            repo = root / "repo"
            linked = root / "linked"
            run_git(root, "init", "-q", "-b", "main", str(repo))
            run_git(repo, "config", "user.name", "Test")
            run_git(repo, "config", "user.email", "test@example.invalid")
            run_git(repo, "commit", "-q", "--allow-empty", "-m", "initial")
            run_git(repo, "branch", "absorbed")
            run_git(repo, "worktree", "add", "-q", str(linked), "absorbed")
            head = run_git(repo, "rev-parse", "absorbed").strip()
            entry = CLEANER.Entry(
                worktree=CLEANER.Worktree(
                    path=str(linked), head=head, branch="absorbed"
                ),
                clean=True,
                pr={"status": "no_pr"},
                base_diff={"base": "main", "ahead_behind": {"ahead": 0, "behind": 0}},
                decision="delete",
            )

            CLEANER.delete_entry(repo, entry, base="main", fetch_ok=True, dry_run=False)

            self.assertEqual(entry.decision, "deleted")
            self.assertFalse(linked.exists())
            self.assertFalse(
                CLEANER.git_ok(repo, "show-ref", "--verify", "refs/heads/absorbed")
            )

    def test_non_submodule_removal_failure_is_reported_without_force(self) -> None:
        entry = CLEANER.Entry(
            worktree=CLEANER.Worktree(path="/linked", head="a" * 40, branch="topic"),
            clean=True,
            pr={"status": "merged", "headRefOid": "a" * 40},
            decision="delete",
        )
        failed = subprocess.CompletedProcess(
            ["git", "worktree", "remove"],
            returncode=1,
            stdout="",
            stderr="permission denied",
        )
        with (
            mock.patch.object(
                CLEANER, "worktree_status", return_value=(True, {"files": []})
            ),
            mock.patch.object(CLEANER, "git", return_value="a" * 40),
            mock.patch.object(CLEANER, "revalidate_history", return_value=True),
            mock.patch.object(CLEANER, "run", return_value=failed) as run_mock,
        ):
            CLEANER.delete_entry(
                Path("/repo"), entry, base="origin/main", fetch_ok=True, dry_run=False
            )

        self.assertEqual(entry.decision, "review")
        self.assertIn("removal failed", entry.reason)
        commands = [call.args[0] for call in run_mock.call_args_list]
        self.assertFalse(any("--force" in command for command in commands))

    def test_mixed_removal_error_does_not_trigger_force(self) -> None:
        entry = CLEANER.Entry(
            worktree=CLEANER.Worktree(path="/linked", head="a" * 40, branch="topic"),
            clean=True,
            pr={"status": "merged", "headRefOid": "a" * 40},
            decision="delete",
        )
        failed = subprocess.CompletedProcess(
            ["git", "worktree", "remove"],
            returncode=1,
            stdout="permission denied",
            stderr=(
                "fatal: working trees containing submodules cannot be moved or removed"
            ),
        )
        with (
            mock.patch.object(
                CLEANER, "worktree_status", return_value=(True, {"files": []})
            ),
            mock.patch.object(CLEANER, "git", return_value="a" * 40),
            mock.patch.object(CLEANER, "revalidate_history", return_value=True),
            mock.patch.object(CLEANER, "run", return_value=failed) as run_mock,
            mock.patch.object(CLEANER, "audit_submodule_force_removal") as audit_mock,
        ):
            CLEANER.delete_entry(
                Path("/repo"), entry, base="origin/main", fetch_ok=True, dry_run=False
            )

        self.assertEqual(entry.decision, "review")
        self.assertTrue(any("permission denied" in error for error in entry.errors))
        audit_mock.assert_not_called()
        commands = [call.args[0] for call in run_mock.call_args_list]
        self.assertFalse(any("--force" in command for command in commands))

    def test_force_requires_auditable_submodule_metadata(self) -> None:
        entry = CLEANER.Entry(
            worktree=CLEANER.Worktree(path="/linked", head="a" * 40, branch="topic"),
            clean=True,
            pr={"status": "merged", "headRefOid": "a" * 40},
            decision="delete",
        )
        failed = subprocess.CompletedProcess(
            ["git", "worktree", "remove"],
            returncode=1,
            stdout="",
            stderr=(
                "fatal: working trees containing submodules cannot be moved or removed"
            ),
        )
        incomplete_audit = {
            "required": False,
            "safe": True,
            "admin_repo_count": 0,
            "initialized_submodule_count": 0,
            "issues": [],
        }
        with (
            mock.patch.object(
                CLEANER, "worktree_status", return_value=(True, {"files": []})
            ),
            mock.patch.object(CLEANER, "git", return_value="a" * 40),
            mock.patch.object(CLEANER, "revalidate_history", return_value=True),
            mock.patch.object(CLEANER, "run", return_value=failed) as run_mock,
            mock.patch.object(
                CLEANER,
                "audit_submodule_force_removal",
                return_value=incomplete_audit,
            ),
        ):
            CLEANER.delete_entry(
                Path("/repo"), entry, base="origin/main", fetch_ok=True, dry_run=False
            )

        self.assertEqual(entry.decision, "review")
        self.assertTrue(
            any("without auditable admin metadata" in error for error in entry.errors)
        )
        commands = [call.args[0] for call in run_mock.call_args_list]
        self.assertFalse(any("--force" in command for command in commands))


class SubmoduleCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="clean-worktree-test-")
        self.root = Path(self.tempdir.name)
        self.subrepo = self.root / "subrepo"
        self.superrepo = self.root / "superrepo"
        self.linked = self.root / "linked"

        run_git(self.root, "init", "-q", "-b", "main", str(self.subrepo))
        run_git(self.subrepo, "config", "user.name", "Test")
        run_git(self.subrepo, "config", "user.email", "test@example.invalid")
        run_git(self.subrepo, "commit", "-q", "--allow-empty", "-m", "initial")

        run_git(self.root, "init", "-q", "-b", "main", str(self.superrepo))
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
        run_git(
            self.superrepo,
            "worktree",
            "add",
            "-q",
            "-b",
            "absorbed",
            str(self.linked),
        )
        self.head = run_git(self.linked, "rev-parse", "HEAD").strip()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def initialize_submodule(self) -> Path:
        run_git(
            self.linked,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "update",
            "--init",
            "-q",
        )
        return self.linked / "deps" / "sub"

    def entry(self) -> CLEANER.Entry:
        return CLEANER.Entry(
            worktree=CLEANER.Worktree(
                path=str(self.linked), head=self.head, branch="absorbed"
            ),
            clean=True,
            pr={"status": "no_pr"},
            base_diff={
                "base": "main",
                "ahead_behind": {"ahead": 0, "behind": 0},
            },
            decision="delete",
        )

    def test_clean_recoverable_submodule_is_safely_force_removed(self) -> None:
        self.initialize_submodule()
        entry = self.entry()

        CLEANER.delete_entry(
            self.superrepo, entry, base="main", fetch_ok=True, dry_run=False
        )

        self.assertEqual(entry.decision, "deleted")
        self.assertFalse(self.linked.exists())
        self.assertIsNotNone(entry.submodule_cleanup)
        self.assertTrue(entry.submodule_cleanup["safe"])
        self.assertTrue(
            any("force-removed worktree" in action for action in entry.actions)
        )
        self.assertFalse(
            CLEANER.git_ok(
                self.superrepo,
                "show-ref",
                "--verify",
                "refs/heads/absorbed",
            )
        )

    def test_dry_run_reports_submodule_cleanup_without_mutation(self) -> None:
        self.initialize_submodule()
        entry = self.entry()

        CLEANER.delete_entry(
            self.superrepo, entry, base="main", fetch_ok=True, dry_run=True
        )

        self.assertEqual(entry.decision, "would_delete")
        self.assertTrue(self.linked.exists())
        self.assertTrue(
            any("would force-remove worktree" in action for action in entry.actions)
        )
        self.assertTrue(
            CLEANER.git_ok(
                self.superrepo,
                "show-ref",
                "--verify",
                "refs/heads/absorbed",
            )
        )

    def test_dirty_initialized_submodule_fails_safety_audit(self) -> None:
        submodule = self.initialize_submodule()
        (submodule / "untracked.txt").write_text("preserve me\n")

        result = CLEANER.audit_submodule_force_removal(str(self.linked))

        self.assertFalse(result["safe"])
        self.assertTrue(
            any(
                "initialized submodule worktree is dirty" in issue
                for issue in result["issues"]
            )
        )

    def test_local_only_submodule_commit_blocks_worktree_removal(self) -> None:
        submodule = self.initialize_submodule()
        expected = run_git(submodule, "rev-parse", "HEAD").strip()
        run_git(submodule, "config", "user.name", "Test")
        run_git(submodule, "config", "user.email", "test@example.invalid")
        run_git(submodule, "checkout", "-q", "-b", "local-only")
        run_git(submodule, "commit", "-q", "--allow-empty", "-m", "local only")
        run_git(submodule, "checkout", "-q", "--detach", expected)

        entry = self.entry()
        CLEANER.delete_entry(
            self.superrepo, entry, base="main", fetch_ok=True, dry_run=False
        )

        self.assertEqual(entry.decision, "review")
        self.assertEqual(entry.reason, "submodule safety check failed")
        self.assertTrue(self.linked.exists())
        self.assertTrue(any("not recoverable" in error for error in entry.errors))
        self.assertTrue(
            CLEANER.git_ok(
                self.superrepo,
                "show-ref",
                "--verify",
                "refs/heads/absorbed",
            )
        )

    def test_unreachable_submodule_remote_blocks_worktree_removal(self) -> None:
        submodule = self.initialize_submodule()
        run_git(
            submodule,
            "remote",
            "set-url",
            "origin",
            str(self.root / "missing-remote"),
        )

        entry = self.entry()
        CLEANER.delete_entry(
            self.superrepo, entry, base="main", fetch_ok=True, dry_run=False
        )

        self.assertEqual(entry.decision, "review")
        self.assertEqual(entry.reason, "submodule safety check failed")
        self.assertTrue(self.linked.exists())
        self.assertTrue(
            any("unable to query remote origin" in error for error in entry.errors)
        )


class PullRequestClassificationTest(unittest.TestCase):
    def test_open_pr_takes_priority_over_merged_history(self) -> None:
        result = CLEANER.classify_prs(
            [
                {
                    "number": 1,
                    "state": "MERGED",
                    "mergedAt": "2026-01-01",
                    "updatedAt": "2026-01-01",
                },
                {
                    "number": 2,
                    "state": "OPEN",
                    "mergedAt": None,
                    "updatedAt": "2026-02-01",
                },
            ]
        )
        self.assertEqual(result["status"], "open")
        self.assertEqual(result["number"], 2)


if __name__ == "__main__":
    unittest.main()
