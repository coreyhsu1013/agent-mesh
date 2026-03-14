"""
Regression test: scaffold monorepo policy.
- Build artifacts (.next/, dist/, node_modules/) → blocked
- Monorepo config (pnpm-workspace.yaml, turbo.json, lockfiles) → blocked unless in target_files
- Normal app files → allowed

Run: python3 -m unittest tests.test_scaffold_policy -v
"""

import sys
import unittest

sys.path.insert(0, ".")

from src.gates.checks.basic import (
    no_build_artifacts,
    no_monorepo_config,
    _extract_changed_files,
)


# ── Sample diffs ──

DIFF_NORMAL = """\
diff --git a/apps/admin/src/app.tsx b/apps/admin/src/app.tsx
index abc1234..def5678 100644
--- a/apps/admin/src/app.tsx
+++ b/apps/admin/src/app.tsx
@@ -1,3 +1,4 @@
+import { Layout } from './layout';
"""

DIFF_NEXT_BUILD = """\
diff --git a/apps/admin/.next/static/chunks/main.js b/apps/admin/.next/static/chunks/main.js
new file mode 100644
--- /dev/null
+++ b/apps/admin/.next/static/chunks/main.js
@@ -0,0 +1 @@
+// build output
"""

DIFF_DIST = """\
diff --git a/packages/ui/dist/index.js b/packages/ui/dist/index.js
new file mode 100644
--- /dev/null
+++ b/packages/ui/dist/index.js
@@ -0,0 +1 @@
+export default {};
"""

DIFF_NODE_MODULES = """\
diff --git a/node_modules/react/index.js b/node_modules/react/index.js
index abc1234..def5678 100644
--- a/node_modules/react/index.js
+++ b/node_modules/react/index.js
@@ -1 +1 @@
-old
+new
"""

DIFF_PYCACHE = """\
diff --git a/src/__pycache__/main.cpython-311.pyc b/src/__pycache__/main.cpython-311.pyc
new file mode 100644
Binary files differ
"""

DIFF_PNPM_WORKSPACE = """\
diff --git a/pnpm-workspace.yaml b/pnpm-workspace.yaml
index abc1234..def5678 100644
--- a/pnpm-workspace.yaml
+++ b/pnpm-workspace.yaml
@@ -1,3 +1,4 @@
 packages:
   - 'apps/*'
+  - 'apps/admin'
"""

DIFF_TURBO_JSON = """\
diff --git a/turbo.json b/turbo.json
index abc1234..def5678 100644
--- a/turbo.json
+++ b/turbo.json
@@ -1 +1 @@
-{}
+{"pipeline":{}}
"""

DIFF_ROOT_PKG_JSON = """\
diff --git a/package.json b/package.json
index abc1234..def5678 100644
--- a/package.json
+++ b/package.json
@@ -1 +1 @@
-{"name":"monorepo"}
+{"name":"monorepo","dependencies":{"new-pkg":"1.0"}}
"""

DIFF_LOCKFILE = """\
diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml
index abc1234..def5678 100644
--- a/pnpm-lock.yaml
+++ b/pnpm-lock.yaml
@@ -1 +1 @@
-lockfileVersion: 5.4
+lockfileVersion: 6.0
"""

DIFF_MIXED_ARTIFACT = """\
diff --git a/apps/admin/src/page.tsx b/apps/admin/src/page.tsx
index abc1234..def5678 100644
--- a/apps/admin/src/page.tsx
+++ b/apps/admin/src/page.tsx
@@ -1 +1 @@
-old
+new
diff --git a/apps/admin/.next/cache/data.json b/apps/admin/.next/cache/data.json
new file mode 100644
--- /dev/null
+++ b/apps/admin/.next/cache/data.json
@@ -0,0 +1 @@
+{}
"""

DIFF_NESTED_PKG_JSON = """\
diff --git a/apps/admin/package.json b/apps/admin/package.json
index abc1234..def5678 100644
--- a/apps/admin/package.json
+++ b/apps/admin/package.json
@@ -1 +1 @@
-{"name":"admin"}
+{"name":"admin","version":"2.0"}
"""


class FakeTask:
    target_files = None


class TestNoBuildArtifacts(unittest.TestCase):
    """Gate check: no_build_artifacts."""

    def test_normal_files_allowed(self):
        passed, msg = no_build_artifacts(FakeTask(), diff=DIFF_NORMAL)
        self.assertTrue(passed)

    def test_next_build_blocked(self):
        passed, msg = no_build_artifacts(FakeTask(), diff=DIFF_NEXT_BUILD)
        self.assertFalse(passed)
        self.assertIn(".next", msg)

    def test_dist_blocked(self):
        passed, msg = no_build_artifacts(FakeTask(), diff=DIFF_DIST)
        self.assertFalse(passed)
        self.assertIn("dist", msg)

    def test_node_modules_blocked(self):
        passed, msg = no_build_artifacts(FakeTask(), diff=DIFF_NODE_MODULES)
        self.assertFalse(passed)
        self.assertIn("node_modules", msg)

    def test_pycache_blocked(self):
        passed, msg = no_build_artifacts(FakeTask(), diff=DIFF_PYCACHE)
        self.assertFalse(passed)
        self.assertIn("__pycache__", msg)

    def test_mixed_with_artifact_blocked(self):
        passed, msg = no_build_artifacts(FakeTask(), diff=DIFF_MIXED_ARTIFACT)
        self.assertFalse(passed)
        self.assertIn(".next", msg)

    def test_empty_diff_passes(self):
        passed, _ = no_build_artifacts(FakeTask(), diff="")
        self.assertTrue(passed)

    def test_no_diff_passes(self):
        passed, _ = no_build_artifacts(FakeTask())
        self.assertTrue(passed)


class TestNoMonorepoConfig(unittest.TestCase):
    """Gate check: no_monorepo_config."""

    def test_normal_files_allowed(self):
        passed, msg = no_monorepo_config(FakeTask(), diff=DIFF_NORMAL)
        self.assertTrue(passed)

    def test_pnpm_workspace_blocked(self):
        passed, msg = no_monorepo_config(FakeTask(), diff=DIFF_PNPM_WORKSPACE)
        self.assertFalse(passed)
        self.assertIn("pnpm-workspace.yaml", msg)

    def test_turbo_json_blocked(self):
        passed, msg = no_monorepo_config(FakeTask(), diff=DIFF_TURBO_JSON)
        self.assertFalse(passed)
        self.assertIn("turbo.json", msg)

    def test_root_package_json_blocked(self):
        passed, msg = no_monorepo_config(FakeTask(), diff=DIFF_ROOT_PKG_JSON)
        self.assertFalse(passed)
        self.assertIn("package.json", msg)

    def test_lockfile_blocked(self):
        passed, msg = no_monorepo_config(FakeTask(), diff=DIFF_LOCKFILE)
        self.assertFalse(passed)
        self.assertIn("pnpm-lock.yaml", msg)

    def test_nested_package_json_allowed(self):
        """apps/admin/package.json is NOT a root config — should pass."""
        passed, msg = no_monorepo_config(FakeTask(), diff=DIFF_NESTED_PKG_JSON)
        self.assertTrue(passed)

    def test_allowed_via_target_files(self):
        """Root config in target_files → allowed."""
        task = FakeTask()
        task.target_files = ["pnpm-workspace.yaml"]
        passed, msg = no_monorepo_config(task, diff=DIFF_PNPM_WORKSPACE)
        self.assertTrue(passed)

    def test_partial_target_files(self):
        """Only some configs in target_files → others still blocked."""
        task = FakeTask()
        task.target_files = ["turbo.json"]
        diff = DIFF_TURBO_JSON + DIFF_PNPM_WORKSPACE
        passed, msg = no_monorepo_config(task, diff=diff)
        self.assertFalse(passed)
        self.assertIn("pnpm-workspace.yaml", msg)
        self.assertNotIn("turbo.json", msg)

    def test_empty_diff_passes(self):
        passed, _ = no_monorepo_config(FakeTask(), diff="")
        self.assertTrue(passed)


if __name__ == "__main__":
    unittest.main()
