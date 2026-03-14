"""
Regression test: runtime path protection.
- Agent can modify workspace files → allowed
- Agent modifying .agent-mesh/ runtime files → blocked by gate check
- git add excludes .agent-mesh/ paths

Run: python3 -m unittest tests.test_runtime_protection -v
"""

import sys
import unittest

sys.path.insert(0, ".")

from src.gates.checks.basic import no_runtime_modification, _extract_changed_files


# ── Sample diffs ──

DIFF_NORMAL = """\
diff --git a/src/app.ts b/src/app.ts
index abc1234..def5678 100644
--- a/src/app.ts
+++ b/src/app.ts
@@ -1,3 +1,4 @@
+import { auth } from './auth';
 const app = express();
"""

DIFF_RUNTIME_LOG = """\
diff --git a/.agent-mesh/cycles-run-1.log b/.agent-mesh/cycles-run-1.log
deleted file mode 100644
index abc1234..0000000
--- a/.agent-mesh/cycles-run-1.log
+++ /dev/null
@@ -1,100 +0,0 @@
-[2026-03-14] Log content...
"""

DIFF_RUNTIME_DB = """\
diff --git a/.agent-mesh/context.db b/.agent-mesh/context.db
index abc1234..def5678 100644
Binary files a/.agent-mesh/context.db and b/.agent-mesh/context.db differ
"""

DIFF_RUNTIME_STATE = """\
diff --git a/.agent-mesh/events-store.json b/.agent-mesh/events-store.json
index abc1234..def5678 100644
--- a/.agent-mesh/events-store.json
+++ b/.agent-mesh/events-store.json
@@ -1 +1 @@
-{}
+{"corrupted": true}
"""

DIFF_MIXED = """\
diff --git a/src/app.ts b/src/app.ts
index abc1234..def5678 100644
--- a/src/app.ts
+++ b/src/app.ts
@@ -1,3 +1,4 @@
+import { auth } from './auth';
diff --git a/.agent-mesh/context.db b/.agent-mesh/context.db
index abc1234..def5678 100644
Binary files differ
"""

DIFF_WORKSPACE_LOG = """\
diff --git a/.agent-mesh/workspaces/logs/task-1.log b/.agent-mesh/workspaces/logs/task-1.log
new file mode 100644
--- /dev/null
+++ b/.agent-mesh/workspaces/logs/task-1.log
@@ -0,0 +1 @@
+some log output
"""


class FakeTask:
    pass


class TestNoRuntimeModification(unittest.TestCase):
    """Gate check: no_runtime_modification."""

    def test_normal_files_allowed(self):
        passed, msg = no_runtime_modification(FakeTask(), diff=DIFF_NORMAL)
        self.assertTrue(passed)
        self.assertIn("No runtime files touched", msg)

    def test_runtime_log_blocked(self):
        passed, msg = no_runtime_modification(FakeTask(), diff=DIFF_RUNTIME_LOG)
        self.assertFalse(passed)
        self.assertIn(".agent-mesh/cycles-run-1.log", msg)

    def test_runtime_db_blocked(self):
        passed, msg = no_runtime_modification(FakeTask(), diff=DIFF_RUNTIME_DB)
        self.assertFalse(passed)
        self.assertIn(".agent-mesh/context.db", msg)

    def test_runtime_state_blocked(self):
        passed, msg = no_runtime_modification(FakeTask(), diff=DIFF_RUNTIME_STATE)
        self.assertFalse(passed)
        self.assertIn(".agent-mesh/events-store.json", msg)

    def test_mixed_diff_blocked(self):
        """Even if some files are legit, runtime violation still blocks."""
        passed, msg = no_runtime_modification(FakeTask(), diff=DIFF_MIXED)
        self.assertFalse(passed)
        self.assertIn(".agent-mesh/context.db", msg)

    def test_workspace_log_blocked(self):
        """Workspace logs under .agent-mesh/ are also protected."""
        passed, msg = no_runtime_modification(FakeTask(), diff=DIFF_WORKSPACE_LOG)
        self.assertFalse(passed)
        self.assertIn(".agent-mesh/workspaces/logs/task-1.log", msg)

    def test_empty_diff_passes(self):
        passed, _ = no_runtime_modification(FakeTask(), diff="")
        self.assertTrue(passed)

    def test_no_diff_passes(self):
        passed, _ = no_runtime_modification(FakeTask())
        self.assertTrue(passed)


class TestExtractChangedFiles(unittest.TestCase):
    """Verify _extract_changed_files picks up .agent-mesh/ paths."""

    def test_extracts_runtime_paths(self):
        files = _extract_changed_files(DIFF_RUNTIME_LOG)
        self.assertIn(".agent-mesh/cycles-run-1.log", files)

    def test_extracts_normal_paths(self):
        files = _extract_changed_files(DIFF_NORMAL)
        self.assertIn("src/app.ts", files)

    def test_extracts_mixed(self):
        files = _extract_changed_files(DIFF_MIXED)
        self.assertIn("src/app.ts", files)
        self.assertIn(".agent-mesh/context.db", files)


class TestGitAddExclusion(unittest.TestCase):
    """Verify staging exclusion covers runtime files AND build artifacts."""

    def test_pathspec_excludes_runtime(self):
        """GIT_ADD_PATHSPEC must exclude .agent-mesh."""
        from src.orchestrator.workspace import GIT_ADD_PATHSPEC
        self.assertIn("':!.agent-mesh'", GIT_ADD_PATHSPEC)

    def test_pathspec_excludes_build_artifacts(self):
        """GIT_ADD_PATHSPEC must exclude common build artifact dirs."""
        from src.orchestrator.workspace import GIT_ADD_PATHSPEC
        for artifact in [".next", "dist", "build", "out", "node_modules",
                         "__pycache__", ".turbo", ".cache"]:
            self.assertIn(f"':!{artifact}'", GIT_ADD_PATHSPEC, f"Missing: {artifact}")

    def test_react_loop_uses_pathspec(self):
        """react_loop._observe uses shared GIT_ADD_PATHSPEC."""
        import src.orchestrator.react_loop as rl
        import inspect
        source = inspect.getsource(rl.ReactLoop._observe)
        self.assertIn("GIT_ADD_PATHSPEC", source)
        self.assertNotIn("git add -A", source)

    def test_workspace_commit_uses_pathspec(self):
        """workspace.commit_slot_task uses shared GIT_ADD_PATHSPEC."""
        import src.orchestrator.workspace as ws
        import inspect
        source = inspect.getsource(ws.WorkspacePool.commit_slot_task)
        self.assertIn("GIT_ADD_PATHSPEC", source)

    def test_workspace_merge_uses_pathspec(self):
        """workspace._merge_branch uses shared GIT_ADD_PATHSPEC."""
        import src.orchestrator.workspace as ws
        import inspect
        source = inspect.getsource(ws.WorkspacePool._merge_branch)
        self.assertIn("GIT_ADD_PATHSPEC", source)

    def test_dispatcher_buildfix_uses_pathspec(self):
        """dispatcher._fix_build_on_main uses shared GIT_ADD_PATHSPEC."""
        import src.orchestrator.dispatcher as dp
        import inspect
        source = inspect.getsource(dp.Dispatcher._fix_build_on_main)
        self.assertIn("GIT_ADD_PATHSPEC", source)

    def test_no_raw_git_add_a_in_src(self):
        """No 'add -A' should remain anywhere in src/."""
        import glob
        for py_file in glob.glob("src/**/*.py", recursive=True):
            with open(py_file) as f:
                content = f.read()
            self.assertNotIn(
                '"add -A"', content,
                f"Found raw 'add -A' in {py_file}"
            )


class TestBuildArtifactHint(unittest.TestCase):
    """Verify no_build_artifacts gate hint is actionable."""

    def test_hint_contains_cleanup_commands(self):
        """Hint must include actual rm/git rm commands."""
        from src.gates.runner import _CHECK_HINTS
        hint = _CHECK_HINTS["no_build_artifacts"]
        self.assertIn("rm -rf", hint)
        self.assertIn(".next", hint)
        self.assertIn("git rm", hint)
        self.assertIn(".gitignore", hint)


if __name__ == "__main__":
    unittest.main()
