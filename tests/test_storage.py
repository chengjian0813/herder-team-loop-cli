import tempfile
import unittest
from pathlib import Path

from herdr_team_loop.errors import LoopError
from herdr_team_loop.storage import Store


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.ws = self.store.create("workspaces", {"path": self.temp.name})
        self.req = self.store.create("requirements", {"workspace_id": self.ws["id"], "title": "first"})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_cross_workspace_relationship_rejected(self):
        other = self.store.create("workspaces", {"path": self.temp.name + "/other"})
        with self.assertRaises(LoopError):
            self.store.create("runs", {"workspace_id": other["id"], "requirement_id": self.req["id"], "status": "READY"})
        self.assertEqual(self.store.list("runs"), [])

    def test_one_live_run_and_atomic_rollback(self):
        run = {"workspace_id": self.ws["id"], "requirement_id": self.req["id"], "status": "READY"}
        with self.assertRaises(LoopError):
            with self.store.transaction():
                self.store.create("runs", run)
                self.store.create("runs", run)
        self.assertEqual(self.store.list("runs"), [])

    def test_stale_revision_and_immutable_scope(self):
        self.store.update("requirements", self.req["id"], {"title": "updated"}, 1)
        with self.assertRaises(LoopError):
            self.store.update("requirements", self.req["id"], {"title": "old"}, 1)
        with self.assertRaises(LoopError):
            self.store.update("requirements", self.req["id"], {"workspace_id": "other"})

    def test_artifact_integrity_and_consistent_backup(self):
        artifact = self.store.save_artifact(self.ws["id"], requirement_id=self.req["id"], kind="scope", content="original")
        backup = Path(self.temp.name) / "backup"
        self.store.backup(backup)
        restored = Store(backup)
        self.assertEqual(restored.read_artifact(artifact["id"]), b"original")
        restored.close()
        (self.store.root / artifact["path"]).write_text("changed")
        with self.assertRaises(LoopError):
            self.store.read_artifact(artifact["id"])


if __name__ == "__main__":
    unittest.main()
