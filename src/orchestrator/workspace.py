"""
Agent Mesh v0.7 — Wave-based Workspace Pool

v0.7 核心改變：
  Wave 執行期間 main 不動，所有 slot 互不影響。
  Wave 結束後統一依序 merge，衝突幾乎歸零。

v0.7.2 改進：
  slot 數量上限 = max_parallel，超額 task 由 worker 依序執行並回收 slot。
  記憶體用量永遠 <= max_parallel × 300MB。

Flow:
  Wave Start:  create min(task_count, max_parallel) worker slots from main
  Wave Run:    worker 完成一個 task 後 commit + 回收 slot，填入下一個 task
  Wave End:    依序 merge task branches → main
  Next Wave:   cleanup slots + task branches，從更新後的 main 建新 slots
"""

from __future__ import annotations
import asyncio
import logging
import os
import shutil

logger = logging.getLogger(__name__)

WORKSPACE_DIR = ".agent-mesh/workspaces"

# Paths excluded from git staging across all execution paths.
# Prevents runtime files and build artifacts from entering commits.
# Root-only paths (only appear at repo root):
_ROOT_EXCLUDES = [".agent-mesh"]
# Nestable paths (can appear at any depth in monorepos, e.g. apps/admin/node_modules):
_NESTED_EXCLUDES = [
    ".next", "dist", "build", "out", "node_modules",
    "__pycache__", ".turbo", ".cache",
]

# Combined flat list for .gitignore injection (everything except .agent-mesh)
_STAGING_EXCLUDES = _ROOT_EXCLUDES + _NESTED_EXCLUDES

# Pre-built pathspec string for git add: -- . ':(exclude).agent-mesh' ':(exclude)**/...' ...
# Uses ':(exclude)' long form — ':!' and ':^' break on some git/shell/locale combos.
# Nested paths use **/ glob to match at any depth (e.g. apps/admin/node_modules).
_EXCLUDES_PARTS = [f"':(exclude){p}'" for p in _ROOT_EXCLUDES]
_EXCLUDES_PARTS += [f"':(exclude)**/{p}'" for p in _NESTED_EXCLUDES]
_EXCLUDES = " ".join(_EXCLUDES_PARTS)
GIT_ADD_PATHSPEC = f"add -- . {_EXCLUDES}"

# Entries to ensure in .gitignore (build artifacts only, not .agent-mesh)
_GITIGNORE_ENTRIES = list(_NESTED_EXCLUDES)


def _ensure_gitignore(workspace_dir: str) -> None:
    """
    Ensure .gitignore in workspace covers build artifacts.
    Appends missing entries — does NOT overwrite existing content.
    Handles repos that have no .gitignore or have node_modules tracked.
    """
    gitignore_path = os.path.join(workspace_dir, ".gitignore")
    existing = set()
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r") as f:
            for line in f:
                existing.add(line.strip().rstrip("/"))

    missing = [e for e in _GITIGNORE_ENTRIES if e not in existing]
    if not missing:
        return

    with open(gitignore_path, "a") as f:
        f.write("\n# agent-mesh: auto-added build artifact exclusions\n")
        for entry in missing:
            f.write(f"{entry}/\n")

    logger.debug(f"[Workspace] .gitignore: added {len(missing)} entries")


async def _untrack_artifacts(pool, workspace_dir: str) -> None:
    """
    Remove build artifact dirs from git tracking if they were previously committed.
    .gitignore only affects untracked files — already-tracked dirs need 'git rm --cached'.
    Searches both root-level and nested (monorepo) artifact dirs.
    Commits the cleanup so the task's diff stays clean.
    """
    import glob as globmod

    untracked = []
    for entry in _NESTED_EXCLUDES:
        # Root-level
        root_path = os.path.join(workspace_dir, entry)
        if os.path.isdir(root_path):
            try:
                await pool._run_git(
                    f"rm -r --cached {entry}", cwd=workspace_dir
                )
                untracked.append(entry)
            except Exception:
                pass

        # Nested (e.g. apps/admin/.next, apps/api/node_modules)
        for nested in globmod.glob(
            os.path.join(workspace_dir, "**", entry), recursive=True
        ):
            if os.path.isdir(nested):
                rel = os.path.relpath(nested, workspace_dir)
                if rel == entry:
                    continue  # Already handled above
                try:
                    await pool._run_git(
                        f"rm -r --cached {rel}", cwd=workspace_dir
                    )
                    untracked.append(rel)
                except Exception:
                    pass

    if untracked:
        try:
            await pool._run_git("add .gitignore", cwd=workspace_dir)
            dirs = ", ".join(untracked[:5])
            if len(untracked) > 5:
                dirs += f" (+{len(untracked) - 5} more)"
            await pool._run_git(
                f'commit -m "[agent-mesh] untrack build artifacts: {dirs}"',
                cwd=workspace_dir,
            )
            logger.info(
                f"[Workspace] Untracked {len(untracked)} artifact dirs: {dirs}"
            )
        except Exception as e:
            logger.debug(f"[Workspace] Untrack commit failed (ok): {e}")


class WorkspacePool:
    """
    Wave-based git worktree pool.
    每個 Wave 建立固定數量 worker slots，task 依序填入；Wave 結束統一 merge。
    """

    def __init__(self, repo_dir: str, config: dict,
                 target_branch: str = "main",
                 slot_prefix: str = "slot"):
        self.repo_dir = os.path.abspath(repo_dir)
        self.config = config
        self.target_branch = target_branch  # v1.2: configurable merge target
        self.slot_prefix = slot_prefix      # v1.2: namespace for parallel chunks
        self.workspace_base = os.path.join(self.repo_dir, WORKSPACE_DIR)
        os.makedirs(self.workspace_base, exist_ok=True)
        self._active_slots: dict[int, str] = {}   # slot_id → ws_dir
        self._task_branches: list[str] = []        # task branch names for cleanup

    # ── Wave Lifecycle ──

    async def setup_wave(self, slot_count: int) -> dict[int, str]:
        """
        Wave 開始：建立 slot_count 個 worker slots。
        slot_count 應為 min(task_count, max_parallel)。
        Returns: {slot_id: workspace_dir}
        """
        # Clean up previous wave's slots
        await self._cleanup_all_slots()

        await self._ensure_base_branch()

        self._active_slots = {}
        self._task_branches = []
        for i in range(slot_count):
            ws_dir = await self._create_slot(i)
            self._active_slots[i] = ws_dir

        logger.info(f"[WorkspacePool] Wave setup: {slot_count} slots ready")
        return dict(self._active_slots)

    async def prepare_slot_for_task(self, slot_id: int, task_idx: int) -> str:
        """
        Switch a worker slot to a fresh branch for a new task.
        Returns the workspace directory.
        """
        ws_dir = self._active_slots[slot_id]
        branch_name = f"agent-mesh/{self.slot_prefix}_task_{task_idx}"

        # Ensure clean state (discard any leftover uncommitted changes)
        try:
            await self._run_git("reset --hard", cwd=ws_dir)
        except Exception:
            pass

        # Delete branch if left over from previous run
        try:
            await self._run_git(f"branch -D {branch_name}")
        except Exception:
            pass

        # Create fresh branch from main and checkout
        await self._run_git(
            f"checkout -b {branch_name} {self.target_branch}", cwd=ws_dir
        )
        self._task_branches.append(branch_name)

        # Remove untracked files from previous task
        await self._run_git("clean -fd", cwd=ws_dir)

        # Clear any index.lock
        lock_file = os.path.join(ws_dir, ".git", "index.lock")
        if os.path.exists(lock_file):
            os.remove(lock_file)

        # Ensure .gitignore covers build artifacts (even if repo lacks one)
        _ensure_gitignore(ws_dir)

        # Untrack build artifacts that were previously committed (e.g. node_modules)
        # .gitignore only affects untracked files; already-tracked dirs need explicit removal
        await _untrack_artifacts(self, ws_dir)

        return ws_dir

    async def commit_slot_task(self, slot_id: int, commit_msg: str):
        """Commit all changes in a worker slot (call before recycling)."""
        ws_dir = self._active_slots[slot_id]
        try:
            await self._run_git(GIT_ADD_PATHSPEC, cwd=ws_dir)
        except RuntimeError:
            # git add exits 1 when .gitignore blocks already-tracked files — safe to ignore
            pass
        try:
            await self._run_git(
                f'commit --allow-empty -m "{commit_msg}"',
                cwd=ws_dir,
            )
        except Exception:
            pass

    async def merge_wave(
        self,
        completed_task_indices: list[int],
        task_labels: dict[int, str] | None = None,
    ) -> dict[int, bool]:
        """
        Wave 結束：依序 merge 所有完成的 task branches → main。

        Args:
            completed_task_indices: task indices that completed successfully
            task_labels: optional {task_idx: label} for commit messages

        Returns: {task_idx: merge_success}
        """
        results = {}
        task_labels = task_labels or {}

        logger.info(f"\n{'─'*40}")
        logger.info(f"  🔀 Merge Phase: {len(completed_task_indices)} tasks → main")
        logger.info(f"{'─'*40}")

        for idx in completed_task_indices:
            branch_name = f"agent-mesh/{self.slot_prefix}_task_{idx}"
            label = task_labels.get(idx, f"task_{idx}")
            commit_msg = f"[agent-mesh] {label}"

            success = await self._merge_branch(branch_name, commit_msg)
            results[idx] = success

            status = "✅" if success else "❌"
            logger.info(f"  {status} task_{idx}: {label}")

        # Post-merge: scan for conflict markers
        conflicts = await self._scan_conflict_markers()
        if conflicts:
            logger.warning(
                f"[WorkspacePool] ⚠️ {len(conflicts)} files with conflict markers after wave merge: "
                f"{', '.join(conflicts[:5])}"
            )

        merged = sum(1 for v in results.values() if v)
        failed = sum(1 for v in results.values() if not v)
        logger.info(f"\n  Merge result: {merged} ✅ / {failed} ❌")

        return results

    async def merge_single(self, task_idx: int, commit_msg: str) -> bool:
        """Merge a single task branch → main. Returns success."""
        branch_name = f"agent-mesh/{self.slot_prefix}_task_{task_idx}"
        return await self._merge_branch(branch_name, commit_msg)

    async def run_build_check(self, build_cmd: str = "") -> tuple[bool, str]:
        """Run build on main repo. Returns (success, output)."""
        if not build_cmd:
            build_cmd = (
                "pnpm run build 2>&1 || npm run build 2>&1 || "
                "npx tsc --noEmit 2>&1 || echo 'NO_BUILD_SCRIPT'"
            )
        try:
            proc = await asyncio.create_subprocess_shell(
                build_cmd,
                cwd=self.repo_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
            output = stdout.decode(errors="replace") if stdout else ""
            if "NO_BUILD_SCRIPT" in output:
                return True, output[:5000]
            return proc.returncode == 0, output[:5000]
        except asyncio.TimeoutError:
            return False, "BUILD TIMEOUT (180s)"
        except Exception as e:
            return False, f"BUILD CMD ERROR: {e}"

    async def cleanup_wave(self):
        """Wave 結束後清理所有 worker slots 和 task branches。"""
        await self._cleanup_all_slots()

        # Clean up task branches
        for branch_name in self._task_branches:
            try:
                await self._run_git(f"branch -D {branch_name}")
            except Exception:
                pass

        self._active_slots = {}
        self._task_branches = []

        try:
            await self._run_git("worktree prune")
        except Exception:
            pass

        logger.debug("[WorkspacePool] Wave cleanup done")

    def get_slot_dir(self, slot_id: int) -> str:
        """取得 slot 的工作目錄。"""
        return self._active_slots.get(slot_id, "")

    # ── Merge Logic ──

    async def _merge_branch(self, branch_name: str, commit_msg: str) -> bool:
        """
        把 task branch merge 回 main。
        Changes are already committed on the branch (by commit_slot_task).
        Strategy: try clean merge, fallback -X theirs.
        """
        try:
            # 1) Commit any pending changes on main
            try:
                await self._run_git(
                    GIT_ADD_PATHSPEC, cwd=self.repo_dir
                )
                await self._run_git(
                    'commit --allow-empty -m "[agent-mesh] pre-merge"',
                    cwd=self.repo_dir
                )
            except Exception:
                pass

            # 2) Try clean merge first
            try:
                await self._run_git(
                    f'merge {branch_name} --no-ff -m "Merge {commit_msg}"',
                    cwd=self.repo_dir
                )
                return True
            except Exception:
                pass

            # 3) Clean merge failed — abort and use -X theirs
            try:
                await self._run_git("merge --abort", cwd=self.repo_dir)
            except Exception:
                try:
                    await self._run_git("reset --hard HEAD", cwd=self.repo_dir)
                except Exception:
                    pass

            await self._run_git(
                f'merge {branch_name} -X theirs --no-ff -m "Merge (auto-resolve) {commit_msg}"',
                cwd=self.repo_dir
            )
            return True

        except Exception as e:
            logger.error(f"[WorkspacePool] Merge {branch_name} failed: {e}")
            # Make sure main is clean for next merge
            try:
                await self._run_git("merge --abort", cwd=self.repo_dir)
            except Exception:
                try:
                    await self._run_git("reset --hard HEAD", cwd=self.repo_dir)
                except Exception:
                    pass
            return False

    # ── Internal: Slot Management ──

    async def _create_slot(self, slot_id: int) -> str:
        """Create a worktree slot from current main."""
        ws_dir = os.path.join(self.workspace_base, f"{self.slot_prefix}_{slot_id}")
        branch_name = f"agent-mesh/{self.slot_prefix}_{slot_id}"

        # Remove existing
        if os.path.exists(ws_dir):
            try:
                await self._run_git(f"worktree remove {ws_dir} --force")
            except Exception:
                shutil.rmtree(ws_dir, ignore_errors=True)

        # Delete old branch
        try:
            await self._run_git(f"branch -D {branch_name}")
        except Exception:
            pass

        # Clean any stale worktree references
        try:
            await self._run_git("worktree prune")
        except Exception:
            pass

        # Create fresh worktree from main
        await self._run_git(
            f"worktree add {ws_dir} -b {branch_name} {self.target_branch}"
        )

        # Clear any index.lock
        lock_file = os.path.join(ws_dir, ".git", "index.lock")
        if os.path.exists(lock_file):
            os.remove(lock_file)

        # Copy CLAUDE.md into worktree (untracked, not in git checkout)
        claude_md = os.path.join(self.repo_dir, "CLAUDE.md")
        if os.path.isfile(claude_md):
            shutil.copy2(claude_md, os.path.join(ws_dir, "CLAUDE.md"))

        logger.debug(f"[WorkspacePool] Created slot_{slot_id}")
        return ws_dir

    async def _cleanup_all_slots(self):
        """Remove all worktree slots."""
        if not os.path.exists(self.workspace_base):
            return

        prefix = f"{self.slot_prefix}_"
        for entry in os.listdir(self.workspace_base):
            if entry.startswith(prefix):
                ws_dir = os.path.join(self.workspace_base, entry)
                try:
                    await self._run_git(f"worktree remove {ws_dir} --force")
                except Exception:
                    shutil.rmtree(ws_dir, ignore_errors=True)

                # Clean up branch
                slot_id = entry[len(prefix):]
                try:
                    await self._run_git(f"branch -D agent-mesh/{self.slot_prefix}_{slot_id}")
                except Exception:
                    pass

        try:
            await self._run_git("worktree prune")
        except Exception:
            pass

    async def _scan_conflict_markers(self) -> list[str]:
        """Scan repo for files containing git conflict markers."""
        try:
            result = await asyncio.create_subprocess_shell(
                'grep -rl "<<<<<<< " --include="*.ts" --include="*.tsx" '
                '--include="*.js" --include="*.json" --include="*.prisma" '
                '--include="*.sol" --include="*.yaml" --include="*.yml" '
                '| grep -v node_modules | grep -v .agent-mesh',
                cwd=self.repo_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await result.communicate()
            if stdout:
                return [f.strip() for f in stdout.decode().strip().split('\n') if f.strip()]
            return []
        except Exception:
            return []

    # ── Internal: Git Helpers ──

    async def _ensure_base_branch(self):
        try:
            await self._run_git(
                f"rev-parse --verify {self.target_branch}"
            )
        except Exception:
            try:
                await self._run_git(
                    f"checkout -b {self.target_branch}"
                )
            except Exception:
                pass

    async def _run_git(self, cmd: str, cwd: str | None = None):
        cwd = cwd or self.repo_dir
        full_cmd = f"git {cmd}"
        proc = await asyncio.create_subprocess_shell(
            full_cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err_msg = stderr.decode().strip() or stdout.decode().strip()
            raise RuntimeError(f"Command failed ({proc.returncode}): git {cmd}\n{err_msg}")
        return stdout.decode().strip()
