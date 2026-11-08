import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from herdr_team_loop.errors import LoopError
from herdr_team_loop.fingerprint import fingerprint


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "master")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.source = self.repo / "source.txt"
        self.source.write_text("initial\n")
        (self.repo / ".gitignore").write_text("ignored.txt\n")
        self.git("add", ".")
        self.git("commit", "-m", "fixture")

    def git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.repo), *arguments], check=True, capture_output=True).stdout

    def snapshot(self, **kwargs):
        return fingerprint([str(self.repo)], **kwargs)

    def test_stable_inputs_and_scope_without_repository_writes(self):
        before = self.git("status", "--porcelain=v1")
        result = self.snapshot(scope_digest="scope1")
        self.assertEqual(result, self.snapshot(scope_digest="scope1"))
        self.assertNotEqual(result["digest"], self.snapshot(scope_digest="scope2")["digest"])
        self.assertEqual(before, self.git("status", "--porcelain=v1"))
        self.assertEqual(result["repos"][0]["head"], self.git("rev-parse", "HEAD").decode().strip())

    def test_tracked_staged_untracked_modes_and_missing_files(self):
        original = self.snapshot()
        self.source.write_text("modified\n")
        dirty = self.snapshot()
        self.assertNotEqual(original, dirty)
        self.git("add", "source.txt")
        staged = self.snapshot()
        self.assertNotEqual(dirty, staged)
        self.source.chmod(0o755)
        mode = self.snapshot()
        self.assertNotEqual(staged, mode)
        self.source.unlink()
        self.assertNotEqual(mode, self.snapshot())
        extra = self.repo / "untracked.bin"
        extra.write_bytes(b"one\0two")
        first = self.snapshot()
        extra.write_bytes(b"three\0four")
        self.assertNotEqual(first, self.snapshot())

    def test_git_optimization_flags_cannot_hide_actual_code_changes(self):
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag):
                self.source.write_text("initial\n")
                self.git("update-index", flag, "source.txt")
                before = self.snapshot()
                self.source.write_text("hidden code change\n")
                self.assertNotEqual(before, self.snapshot())
                self.git("update-index", "--no-assume-unchanged", "--no-skip-worktree", "source.txt")

    def test_ignored_files_are_excluded_and_untracked_symlinks_use_link_content(self):
        before = self.snapshot()
        (self.repo / "ignored.txt").write_text("ignored runtime output")
        self.assertEqual(before, self.snapshot())
        link = self.repo / "link"
        link.symlink_to("missing-one")
        first = self.snapshot()
        link.unlink()
        link.symlink_to("missing-two")
        self.assertNotEqual(first, self.snapshot())

    def test_no_task_directory_is_silently_excluded(self):
        before = self.snapshot()
        task = self.repo / ".tasks"
        task.mkdir()
        (task / "source.py").write_text("raise RuntimeError()\n")
        self.git("add", ".tasks/source.py")
        self.assertNotEqual(before, self.snapshot())

    def test_empty_head_and_absent_baseline_are_rejected(self):
        with self.assertRaises(LoopError):
            self.snapshot(baseline="missing-baseline")
        empty = self.root / "empty"
        empty.mkdir()
        subprocess.run(["git", "-C", str(empty), "init", "-b", "master"], check=True, capture_output=True)
        with self.assertRaises(LoopError) as error:
            fingerprint([str(empty)])
        self.assertEqual(error.exception.code, "invalid_repository")

    def test_already_dirty_submodule_content_is_fingerprinted_recursively(self):
        origin = self.root / "origin"
        origin.mkdir()
        def git(*arguments):
            return subprocess.run(["git", "-C", str(origin), *arguments], check=True, capture_output=True)
        git("init", "-b", "master")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        (origin / "child.txt").write_text("initial child\n")
        git("add", ".")
        git("commit", "-m", "fixture")
        self.git("-c", "protocol.file.allow=always", "submodule", "add", str(origin), "child")
        self.git("commit", "-m", "submodule fixture")
        child = self.repo / "child" / "child.txt"
        child.write_text("dirty one\n")
        before = self.snapshot()
        child.write_text("dirty two\n")
        self.assertNotEqual(before, self.snapshot())
        self.git("submodule", "deinit", "-f", "child")
        (self.repo / "child" / "unknown.txt").write_text("uninitialized local data")
        with self.assertRaises(LoopError):
            self.snapshot()

    def test_replaced_parent_symlink_cannot_read_outside_repository(self):
        folder = self.repo / "nested"
        folder.mkdir()
        (folder / "data.txt").write_text("tracked")
        self.git("add", "nested/data.txt")
        (folder / "data.txt").unlink()
        folder.rmdir()
        folder.symlink_to(self.root)
        with self.assertRaises(LoopError) as error:
            self.snapshot()
        self.assertEqual(error.exception.code, "invalid_repository")


if __name__ == "__main__":
    unittest.main()
