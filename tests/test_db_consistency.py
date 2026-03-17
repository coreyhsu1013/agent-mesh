"""
Regression test: DB task status ↔ main branch consistency.
- merge_commit recorded on completed tasks
- resume preflight verifies merge_commit is on HEAD
- reset_task_status correctly resets stale tasks
- backward compat: empty merge_commit → trusted

Run: python3 -m unittest tests.test_db_consistency -v
"""

import asyncio
import subprocess
import sys
import tempfile
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")

from src.models.task import Task, TaskStatus
from src.context.store import ContextStore


class TestMergeCommitField(unittest.TestCase):
    """Test merge_commit field on Task model."""

    def test_task_from_dict_with_merge_commit(self):
        t = Task.from_dict({"title": "test", "merge_commit": "abc123"})
        self.assertEqual(t.merge_commit, "abc123")

    def test_task_from_dict_without_merge_commit(self):
        t = Task.from_dict({"title": "test"})
        self.assertEqual(t.merge_commit, "")

    def test_task_to_dict_includes_merge_commit(self):
        t = Task(title="test", merge_commit="abc123")
        d = t.to_dict()
        self.assertEqual(d["merge_commit"], "abc123")


class TestStoreSchema(unittest.TestCase):
    """Test store schema migration and merge_commit persistence."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = ContextStore(self.tmpdir)

    def tearDown(self):
        self.store.close()

    def test_merge_commit_column_exists(self):
        cursor = self.store.conn.cursor()
        cursor.execute("PRAGMA table_info(tasks)")
        columns = {row[1] for row in cursor.fetchall()}
        self.assertIn("merge_commit", columns)

    def test_update_task_saves_merge_commit(self):
        task = Task(id="t1", title="test task", status="pending")
        cursor = self.store.conn.cursor()
        self.store._upsert_task(cursor, task)
        self.store.conn.commit()

        task.status = TaskStatus.COMPLETED.value
        task.merge_commit = "deadbeef1234"
        self.store.update_task(task)

        loaded = self.store.get_task("t1")
        self.assertEqual(loaded.merge_commit, "deadbeef1234")
        self.assertEqual(loaded.status, "completed")

    def test_upsert_insert_writes_merge_commit(self):
        """_upsert_task INSERT path persists merge_commit directly."""
        task = Task(id="t_ins", title="insert test", status="completed", merge_commit="cafe1234")
        cursor = self.store.conn.cursor()
        self.store._upsert_task(cursor, task)
        self.store.conn.commit()

        loaded = self.store.get_task("t_ins")
        self.assertEqual(loaded.merge_commit, "cafe1234")
        self.assertEqual(loaded.status, "completed")

    def test_update_task_empty_merge_commit_on_failure(self):
        task = Task(id="t2", title="failed task", status="pending")
        cursor = self.store.conn.cursor()
        self.store._upsert_task(cursor, task)
        self.store.conn.commit()

        task.status = TaskStatus.FAILED.value
        task.error = "build failed"
        self.store.update_task(task)

        loaded = self.store.get_task("t2")
        self.assertEqual(loaded.merge_commit, "")
        self.assertEqual(loaded.status, "failed")


class TestResetTaskStatus(unittest.TestCase):
    """Test reset_task_status helper."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = ContextStore(self.tmpdir)

    def tearDown(self):
        self.store.close()

    def test_reset_clears_status_and_merge_commit(self):
        task = Task(
            id="t3", title="stale task",
            status=TaskStatus.COMPLETED.value,
            merge_commit="abc123",
            error="old error",
        )
        cursor = self.store.conn.cursor()
        self.store._upsert_task(cursor, task)
        self.store.conn.commit()

        self.store.reset_task_status("t3")

        loaded = self.store.get_task("t3")
        self.assertEqual(loaded.status, "pending")
        self.assertEqual(loaded.merge_commit, "")
        self.assertEqual(loaded.error, "")


class TestVerifyCompletedOnMain(unittest.TestCase):
    """Test _verify_completed_on_main preflight logic."""

    def _make_dispatcher(self):
        """Create a minimal Dispatcher-like object for testing."""

        class FakePool:
            async def _run_git(self, cmd, cwd=None):
                # Will be mocked per test
                raise NotImplementedError

        class FakeDispatcher:
            def __init__(self):
                self.pool = FakePool()
                self.repo_dir = "/fake"

            async def _verify_completed_on_main(self, completed_tasks):
                stale = set()
                for task in completed_tasks:
                    if not task.merge_commit:
                        continue
                    try:
                        await self.pool._run_git(
                            f"merge-base --is-ancestor {task.merge_commit} HEAD",
                            cwd=self.repo_dir,
                        )
                    except Exception:
                        stale.add(task.id)
                return stale

        return FakeDispatcher()

    def test_ancestor_commit_stays_completed(self):
        """merge_commit is ancestor of HEAD → not stale."""
        dispatcher = self._make_dispatcher()
        dispatcher.pool._run_git = AsyncMock(return_value="")

        tasks = [Task(id="t1", title="ok", merge_commit="abc123")]
        stale = asyncio.run(
            dispatcher._verify_completed_on_main(tasks)
        )
        self.assertEqual(stale, set())

    def test_non_ancestor_commit_is_stale(self):
        """merge_commit not ancestor of HEAD → stale."""
        dispatcher = self._make_dispatcher()
        dispatcher.pool._run_git = AsyncMock(
            side_effect=RuntimeError("exit code 1")
        )

        tasks = [Task(id="t1", title="stale", merge_commit="deadbeef")]
        stale = asyncio.run(
            dispatcher._verify_completed_on_main(tasks)
        )
        self.assertEqual(stale, {"t1"})

    def test_empty_merge_commit_trusted(self):
        """Old data without merge_commit → skip verification (backward compat)."""
        dispatcher = self._make_dispatcher()
        dispatcher.pool._run_git = AsyncMock(
            side_effect=RuntimeError("should not be called")
        )

        tasks = [Task(id="t1", title="old", merge_commit="")]
        stale = asyncio.run(
            dispatcher._verify_completed_on_main(tasks)
        )
        self.assertEqual(stale, set())
        dispatcher.pool._run_git.assert_not_called()

    def test_mixed_tasks(self):
        """Mix of ancestor, non-ancestor, and empty merge_commit."""
        dispatcher = self._make_dispatcher()

        call_count = 0
        async def mock_run_git(cmd, cwd=None):
            nonlocal call_count
            call_count += 1
            if "good_sha" in cmd:
                return ""  # ancestor → ok
            raise RuntimeError("not ancestor")

        dispatcher.pool._run_git = mock_run_git

        tasks = [
            Task(id="t1", title="good", merge_commit="good_sha"),
            Task(id="t2", title="bad", merge_commit="bad_sha"),
            Task(id="t3", title="old", merge_commit=""),
        ]
        stale = asyncio.run(
            dispatcher._verify_completed_on_main(tasks)
        )
        self.assertEqual(stale, {"t2"})
        self.assertEqual(call_count, 2)  # only t1 and t2 checked


class TestConsistencyReport(unittest.TestCase):
    """Test consistency_report and repair_unverifiable."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = ContextStore(self.tmpdir)
        cursor = self.store.conn.cursor()
        for t in [
            Task(id="c1", title="with sha 1", status="completed", merge_commit="aaa"),
            Task(id="c2", title="with sha 2", status="completed", merge_commit="bbb"),
            Task(id="c3", title="no sha", status="completed", merge_commit=""),
            Task(id="p1", title="pending"),
        ]:
            self.store._upsert_task(cursor, t)
        self.store.conn.commit()

    def tearDown(self):
        self.store.close()

    def test_report_counts(self):
        r = self.store.consistency_report()
        self.assertEqual(r["completed"], 3)
        self.assertEqual(r["with_sha"], 2)
        self.assertEqual(r["without_sha"], 1)

    def test_repair_resets_unverifiable(self):
        ids = self.store.repair_unverifiable()
        self.assertEqual(ids, ["c3"])
        # c3 should now be pending
        t = self.store.get_task("c3")
        self.assertEqual(t.status, "pending")
        self.assertEqual(t.merge_commit, "")
        # c1, c2 untouched
        self.assertEqual(self.store.get_task("c1").status, "completed")
        self.assertEqual(self.store.get_task("c2").status, "completed")

    def test_repair_noop_when_all_have_sha(self):
        # Give c3 a SHA
        t = self.store.get_task("c3")
        t.merge_commit = "ccc"
        self.store.update_task(t)
        ids = self.store.repair_unverifiable()
        self.assertEqual(ids, [])

    def test_report_after_repair(self):
        self.store.repair_unverifiable()
        r = self.store.consistency_report()
        self.assertEqual(r["completed"], 2)
        self.assertEqual(r["without_sha"], 0)


class TestBackfillMergeCommits(unittest.TestCase):
    """Test backfill_merge_commits — safe SHA fill for old completed tasks."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = ContextStore(self.tmpdir)
        cursor = self.store.conn.cursor()
        for t in [
            Task(id="c1", title="has sha", status="completed", merge_commit="aaa"),
            Task(id="c2", title="no sha", status="completed", merge_commit=""),
            Task(id="c3", title="also no sha", status="completed", merge_commit=""),
            Task(id="p1", title="pending", status="pending"),
        ]:
            self.store._upsert_task(cursor, t)
        self.store.conn.commit()

    def tearDown(self):
        self.store.close()

    def test_backfill_only_empty_sha(self):
        ids = self.store.backfill_merge_commits("HEAD123")
        self.assertEqual(sorted(ids), ["c2", "c3"])
        self.assertEqual(self.store.get_task("c2").merge_commit, "HEAD123")
        self.assertEqual(self.store.get_task("c3").merge_commit, "HEAD123")
        # c1 untouched
        self.assertEqual(self.store.get_task("c1").merge_commit, "aaa")
        # p1 untouched
        self.assertEqual(self.store.get_task("p1").merge_commit, "")

    def test_backfill_preserves_completed_status(self):
        self.store.backfill_merge_commits("HEAD123")
        self.assertEqual(self.store.get_task("c2").status, "completed")
        self.assertEqual(self.store.get_task("c3").status, "completed")

    def test_backfill_noop_when_all_have_sha(self):
        self.store.backfill_merge_commits("first")
        ids = self.store.backfill_merge_commits("second")
        self.assertEqual(ids, [])
        # Still has first SHA, not overwritten
        self.assertEqual(self.store.get_task("c2").merge_commit, "first")

    def test_report_after_backfill(self):
        self.store.backfill_merge_commits("HEAD123")
        r = self.store.consistency_report()
        self.assertEqual(r["completed"], 3)
        self.assertEqual(r["without_sha"], 0)


class TestSchemaIdempotent(unittest.TestCase):
    """Verify ALTER TABLE migration is idempotent (re-open same DB)."""

    def test_double_init(self):
        tmpdir = tempfile.mkdtemp()
        store1 = ContextStore(tmpdir)
        store1.close()
        # Re-open — ALTER TABLE should not error
        store2 = ContextStore(tmpdir)
        cursor = store2.conn.cursor()
        cursor.execute("PRAGMA table_info(tasks)")
        columns = {row[1] for row in cursor.fetchall()}
        self.assertIn("merge_commit", columns)
        store2.close()


class TestMainCLI(unittest.TestCase):
    """Test __main__.py CLI entry point."""

    def _setup_db(self, tmpdir):
        store = ContextStore(tmpdir)
        cursor = store.conn.cursor()
        for t in [
            Task(id="a1", title="verified", status="completed", merge_commit="aaa"),
            Task(id="a2", title="unverified", status="completed", merge_commit=""),
        ]:
            store._upsert_task(cursor, t)
        store.conn.commit()
        store.close()

    def _run_cli(self, *argv):
        from src.context.__main__ import main
        from io import StringIO
        sys.argv = ["prog", *argv]
        captured = StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            main()
        finally:
            sys.stdout = old_stdout
        return captured.getvalue()

    def test_report_only(self):
        tmpdir = tempfile.mkdtemp()
        self._setup_db(tmpdir)
        out = self._run_cli("--repo", tmpdir)
        self.assertIn("completed:   2", out)
        self.assertIn("no SHA:    1", out)
        self.assertIn("--backfill", out)
        # DB unchanged
        store = ContextStore(tmpdir)
        self.assertEqual(store.consistency_report()["without_sha"], 1)
        store.close()

    def test_backfill_mode(self):
        tmpdir = tempfile.mkdtemp()
        self._setup_db(tmpdir)
        # Init a git repo so _get_head_sha works
        subprocess.run(["git", "init"], cwd=tmpdir, capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmpdir, capture_output=True)
        out = self._run_cli("--repo", tmpdir, "--backfill")
        self.assertIn("Backfilled 1 tasks", out)
        store = ContextStore(tmpdir)
        self.assertEqual(store.consistency_report()["without_sha"], 0)
        self.assertEqual(store.get_task("a2").status, "completed")  # still completed!
        store.close()

    def test_repair_mode(self):
        tmpdir = tempfile.mkdtemp()
        self._setup_db(tmpdir)
        out = self._run_cli("--repo", tmpdir, "--repair")
        self.assertIn("Reset 1 tasks", out)
        store = ContextStore(tmpdir)
        self.assertEqual(store.consistency_report()["without_sha"], 0)
        self.assertEqual(store.get_task("a2").status, "pending")
        store.close()


if __name__ == "__main__":
    unittest.main()
