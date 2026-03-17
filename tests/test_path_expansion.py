"""Tests for allowed_paths_only expansion (basic.py gate check)."""
import pytest
from unittest.mock import MagicMock

from src.gates.checks.basic import allowed_paths_only, _expand_allowed_dirs


def _make_task(**kwargs):
    """Create a mock task with given attributes."""
    task = MagicMock()
    task.target_files = kwargs.get("target_files", [])
    task.category = kwargs.get("category", "backend")
    task.task_type = kwargs.get("task_type", "service")
    task.title = kwargs.get("title", "Test task")
    task.related_dirs = kwargs.get("related_dirs", [])
    return task


def _make_diff(files: list[str]) -> str:
    """Create a minimal git diff with the given file paths."""
    lines = []
    for f in files:
        lines.append(f"diff --git a/{f} b/{f}")
        lines.append(f"--- a/{f}")
        lines.append(f"+++ b/{f}")
        lines.append("+// changed")
    return "\n".join(lines)


class TestSharedDirExpansion:
    def test_shared_dir_allowed(self):
        """app/sales/ task can touch app/shared/."""
        task = _make_task(
            target_files=["app/sales/models.py"],
            related_dirs=["app/shared"],
        )
        diff = _make_diff(["app/sales/models.py", "app/shared/fsm.py"])
        passed, _ = allowed_paths_only(task, diff=diff)
        assert passed

    def test_common_dir_allowed(self):
        """app/sales/ task can touch app/common/."""
        task = _make_task(target_files=["app/sales/routes.py"])
        # _expand_allowed_dirs adds sibling common
        diff = _make_diff(["app/sales/routes.py", "app/common/types.py"])
        passed, _ = allowed_paths_only(task, diff=diff)
        assert passed

    def test_unrelated_module_still_blocked(self):
        """app/sales/ task cannot touch app/auth/."""
        task = _make_task(
            target_files=["app/sales/models.py"],
            category="backend",
            task_type="general",
        )
        diff = _make_diff(["app/sales/models.py", "app/auth/login.py"])
        passed, msg = allowed_paths_only(task, diff=diff)
        assert not passed
        assert "app/auth/login.py" in msg

    def test_monorepo_shared_at_app_root(self):
        """apps/api task can touch apps/api/shared/."""
        task = _make_task(target_files=["apps/api/src/modules/products/service.ts"])
        diff = _make_diff([
            "apps/api/src/modules/products/service.ts",
            "apps/api/shared/types.ts",
        ])
        passed, _ = allowed_paths_only(task, diff=diff)
        assert passed

    def test_related_dirs_from_task(self):
        """Task-provided related_dirs should be allowed."""
        task = _make_task(
            target_files=["app/sales/models.py"],
            related_dirs=["app/finance"],
        )
        diff = _make_diff(["app/sales/models.py", "app/finance/utils.py"])
        passed, _ = allowed_paths_only(task, diff=diff)
        assert passed


class TestExpandAllowedDirs:
    def test_sibling_shared(self):
        task = _make_task(category="frontend", task_type="frontend")
        result = _expand_allowed_dirs(task, {"app/sales"})
        assert "app/shared" in result
        assert "app/common" in result

    def test_no_unrelated_expansion(self):
        task = _make_task(category="frontend", task_type="frontend")
        result = _expand_allowed_dirs(task, {"app/sales"})
        assert "app/auth" not in result
        assert "app/finance" not in result
