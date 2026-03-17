"""
Test: feature-slice allowed paths expansion.
- Backend feature tasks get companion dirs (prisma, test, config, etc.)
- Frontend tasks do NOT get expansion
- Expansion scoped to same app root (no cross-app leaking)

Run: python3 -m unittest tests.test_feature_slice_paths -v
"""

import sys
import unittest

sys.path.insert(0, ".")

from src.gates.checks.basic import (
    allowed_paths_only,
    _is_backend_feature_task,
    _find_app_root,
    _expand_allowed_dirs,
)


# ── Minimal task stub ──

class FakeTask:
    def __init__(self, target_files=None, category="", task_type="", title="", related_dirs=None):
        self.target_files = target_files or []
        self.category = category
        self.task_type = task_type
        self.title = title
        self.related_dirs = related_dirs or []


# ── Diff helpers ──

def _make_diff(*files):
    """Build a minimal git diff touching the given file paths."""
    parts = []
    for f in files:
        parts.append(
            f"diff --git a/{f} b/{f}\n"
            f"index 0000000..1111111 100644\n"
            f"--- a/{f}\n"
            f"+++ b/{f}\n"
            f"@@ -0,0 +1 @@\n"
            f"+// changed\n"
        )
    return "\n".join(parts)


# ══════════════════════════════════════════════════════════
# _is_backend_feature_task
# ══════════════════════════════════════════════════════════

class TestIsBackendFeatureTask(unittest.TestCase):

    def test_backend_crud(self):
        t = FakeTask(category="backend", title="Product service — CRUD, filtering")
        self.assertTrue(_is_backend_feature_task(t))

    def test_backend_module(self):
        t = FakeTask(category="backend", task_type="module", title="Order module")
        self.assertTrue(_is_backend_feature_task(t))

    def test_backend_controller(self):
        t = FakeTask(category="backend", title="Users controller")
        self.assertTrue(_is_backend_feature_task(t))

    def test_frontend_no_expand(self):
        t = FakeTask(category="frontend", title="Product listing page")
        self.assertFalse(_is_backend_feature_task(t))

    def test_fullstack_api(self):
        t = FakeTask(category="fullstack", title="API endpoint for dashboard")
        self.assertTrue(_is_backend_feature_task(t))

    def test_empty_category(self):
        t = FakeTask(category="", title="Setup something")
        self.assertFalse(_is_backend_feature_task(t))

    def test_backend_no_keyword(self):
        t = FakeTask(category="backend", title="Add logging")
        self.assertFalse(_is_backend_feature_task(t))


# ══════════════════════════════════════════════════════════
# _find_app_root
# ══════════════════════════════════════════════════════════

class TestFindAppRoot(unittest.TestCase):

    def test_monorepo_apps(self):
        self.assertEqual(_find_app_root("apps/api/src/modules/products"), "apps/api")

    def test_monorepo_packages(self):
        self.assertEqual(_find_app_root("packages/shared/src/types"), "packages/shared")

    def test_flat_src(self):
        self.assertEqual(_find_app_root("src/modules/products"), "")

    def test_no_match(self):
        self.assertEqual(_find_app_root("lib/utils"), "")

    def test_services(self):
        self.assertEqual(_find_app_root("services/auth/src"), "services/auth")


# ══════════════════════════════════════════════════════════
# allowed_paths_only — integration tests
# ══════════════════════════════════════════════════════════

class TestAllowedPathsIntegration(unittest.TestCase):

    def test_backend_prisma_allowed(self):
        """Backend feature task + prisma schema → PASS"""
        task = FakeTask(
            target_files=["apps/api/src/modules/products/products.service.ts"],
            category="backend",
            title="Product service — CRUD",
        )
        diff = _make_diff("apps/api/prisma/schema.prisma")
        passed, msg = allowed_paths_only(task, diff=diff)
        self.assertTrue(passed, msg)

    def test_backend_test_dir_allowed(self):
        """Backend feature task + test dir → PASS"""
        task = FakeTask(
            target_files=["apps/api/src/modules/products/products.service.ts"],
            category="backend",
            title="Product service — CRUD",
        )
        diff = _make_diff("apps/api/test/products.test.ts")
        passed, msg = allowed_paths_only(task, diff=diff)
        self.assertTrue(passed, msg)

    def test_backend_other_app_blocked(self):
        """Backend feature task + file in different app → FAIL"""
        task = FakeTask(
            target_files=["apps/api/src/modules/products/products.service.ts"],
            category="backend",
            title="Product service — CRUD",
        )
        diff = _make_diff("apps/web/src/pages/index.tsx")
        passed, msg = allowed_paths_only(task, diff=diff)
        self.assertFalse(passed, msg)

    def test_backend_other_module_blocked(self):
        """Backend feature task + file in different module → FAIL"""
        task = FakeTask(
            target_files=["apps/api/src/modules/products/products.service.ts"],
            category="backend",
            title="Product service — CRUD",
        )
        diff = _make_diff("apps/api/src/modules/auth/auth.service.ts")
        passed, msg = allowed_paths_only(task, diff=diff)
        self.assertFalse(passed, msg)

    def test_frontend_prisma_blocked(self):
        """Frontend task + prisma → FAIL (no expansion)"""
        task = FakeTask(
            target_files=["apps/web/src/components/ProductList.tsx"],
            category="frontend",
            title="Product listing page",
        )
        diff = _make_diff("apps/web/prisma/schema.prisma")
        passed, msg = allowed_paths_only(task, diff=diff)
        self.assertFalse(passed, msg)

    def test_flat_project_prisma_allowed(self):
        """Flat project (no apps/) + backend task + prisma → PASS"""
        task = FakeTask(
            target_files=["src/modules/products/products.service.ts"],
            category="backend",
            title="Product service — CRUD",
        )
        diff = _make_diff("prisma/schema.prisma")
        passed, msg = allowed_paths_only(task, diff=diff)
        self.assertTrue(passed, msg)

    def test_no_target_files_skip(self):
        """No target_files → skip (existing behavior)"""
        task = FakeTask(target_files=[], category="backend", title="Something")
        diff = _make_diff("anything/file.ts")
        passed, msg = allowed_paths_only(task, diff=diff)
        self.assertTrue(passed, msg)
        self.assertIn("skipping", msg.lower())

    def test_subdirectory_still_works(self):
        """Subdirectory of target file → still allowed (existing behavior)"""
        task = FakeTask(
            target_files=["apps/api/src/modules/products/products.service.ts"],
            category="backend",
            title="Product service — CRUD",
        )
        diff = _make_diff("apps/api/src/modules/products/dto/create-product.dto.ts")
        passed, msg = allowed_paths_only(task, diff=diff)
        self.assertTrue(passed, msg)

    def test_ancestor_still_works(self):
        """Ancestor file (app.module.ts) → still allowed (existing behavior)"""
        task = FakeTask(
            target_files=["apps/api/src/modules/products/products.service.ts"],
            category="backend",
            title="Product service — CRUD",
        )
        diff = _make_diff("apps/api/src/app.module.ts")
        passed, msg = allowed_paths_only(task, diff=diff)
        self.assertTrue(passed, msg)

    def test_backend_migrations_allowed(self):
        """Backend feature task + migrations → PASS"""
        task = FakeTask(
            target_files=["apps/api/src/modules/products/products.service.ts"],
            category="backend",
            title="Product service — CRUD",
        )
        diff = _make_diff("apps/api/migrations/001_add_products.sql")
        passed, msg = allowed_paths_only(task, diff=diff)
        self.assertTrue(passed, msg)


if __name__ == "__main__":
    unittest.main()
