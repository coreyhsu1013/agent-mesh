"""
Regression test: scaffold/foundation tasks with 0 file changes must fail.
Covers _is_must_change_task, _has_meaningful_changes, and the guard in execute_task.

Run: python3 -m unittest tests.test_no_file_changes -v
"""

import sys
import unittest
from dataclasses import dataclass

sys.path.insert(0, ".")

from src.orchestrator.react_loop import (
    _is_must_change_task,
    _has_meaningful_changes,
    Observation,
)


# ── Minimal task stub ──

@dataclass
class FakeTask:
    title: str = ""
    task_type: str = ""
    category: str = ""
    module: str = ""


# ── _is_must_change_task ──

class TestIsMustChangeTask(unittest.TestCase):
    def test_scaffold_title(self):
        t = FakeTask(title="Backend Scaffold — Project Init")
        self.assertTrue(_is_must_change_task(t))

    def test_foundation_title(self):
        t = FakeTask(title="Foundation: database schema")
        self.assertTrue(_is_must_change_task(t))

    def test_layout_title(self):
        t = FakeTask(title="Admin Layout — Sidebar + Header")
        self.assertTrue(_is_must_change_task(t))

    def test_bootstrap_title(self):
        t = FakeTask(title="Bootstrap Next.js App")
        self.assertTrue(_is_must_change_task(t))

    def test_initial_setup_title(self):
        t = FakeTask(title="Initial Setup for Auth Module")
        self.assertTrue(_is_must_change_task(t))

    def test_normal_task_not_matched(self):
        t = FakeTask(title="Add user CRUD endpoint")
        self.assertFalse(_is_must_change_task(t))

    def test_task_type_match(self):
        t = FakeTask(title="Create base files", task_type="scaffold")
        self.assertTrue(_is_must_change_task(t))

    def test_category_match(self):
        t = FakeTask(title="Set up project", category="foundation")
        self.assertTrue(_is_must_change_task(t))

    def test_module_match(self):
        t = FakeTask(title="Some task", module="bootstrap-core")
        self.assertTrue(_is_must_change_task(t))

    def test_case_insensitive(self):
        t = FakeTask(title="SCAFFOLD Admin App")
        self.assertTrue(_is_must_change_task(t))


# ── _has_meaningful_changes ──

class TestHasMeaningfulChanges(unittest.TestCase):
    def test_empty_observation(self):
        obs = Observation(
            diff="", build_output="", test_output="", lint_output="",
            files_changed=[], success=True,
        )
        self.assertFalse(_has_meaningful_changes(obs))

    def test_has_files_changed(self):
        obs = Observation(
            diff="", build_output="", test_output="", lint_output="",
            files_changed=["src/app.ts"], success=True,
        )
        self.assertTrue(_has_meaningful_changes(obs))

    def test_has_diff_only(self):
        diff = "diff --git a/src/app.ts b/src/app.ts\n+console.log('hello')"
        obs = Observation(
            diff=diff, build_output="", test_output="", lint_output="",
            files_changed=[], success=True,
        )
        self.assertTrue(_has_meaningful_changes(obs))

    def test_trivial_diff_ignored(self):
        obs = Observation(
            diff="   \n  ", build_output="", test_output="", lint_output="",
            files_changed=[], success=True,
        )
        self.assertFalse(_has_meaningful_changes(obs))

    def test_short_diff_ignored(self):
        obs = Observation(
            diff="abc", build_output="", test_output="", lint_output="",
            files_changed=[], success=True,
        )
        self.assertFalse(_has_meaningful_changes(obs))


# ── Integration: scaffold + no changes → must fail ──

class TestNoFileChangesGuard(unittest.TestCase):
    """Verify the guard logic that would run in execute_task."""

    def _simulate_guard(self, task, observation) -> bool:
        """Simulate the guard check from execute_task. Returns True if guard triggers."""
        return (observation.success
                and not _has_meaningful_changes(observation)
                and _is_must_change_task(task))

    def test_scaffold_no_changes_triggers(self):
        task = FakeTask(title="Backend Scaffold — Init")
        obs = Observation(
            diff="", build_output="", test_output="", lint_output="",
            files_changed=[], success=True,
        )
        self.assertTrue(self._simulate_guard(task, obs))

    def test_scaffold_with_changes_ok(self):
        task = FakeTask(title="Backend Scaffold — Init")
        obs = Observation(
            diff="diff --git a/x b/x\n+new", build_output="", test_output="",
            lint_output="", files_changed=["x"], success=True,
        )
        self.assertFalse(self._simulate_guard(task, obs))

    def test_normal_task_no_changes_ok(self):
        """Non-scaffold tasks with 0 files should NOT trigger the guard."""
        task = FakeTask(title="Fix login validation bug")
        obs = Observation(
            diff="", build_output="", test_output="", lint_output="",
            files_changed=[], success=True,
        )
        self.assertFalse(self._simulate_guard(task, obs))

    def test_failed_observation_not_triggered(self):
        """Already-failed observations should not trigger (guard checks success first)."""
        task = FakeTask(title="Scaffold base project")
        obs = Observation(
            diff="", build_output="", test_output="", lint_output="",
            files_changed=[], success=False, error="build error",
        )
        self.assertFalse(self._simulate_guard(task, obs))


if __name__ == "__main__":
    unittest.main()
