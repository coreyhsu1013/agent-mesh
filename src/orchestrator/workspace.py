"""
Agent Mesh v0.7 — Wave-based Workspace Pool

v0.7 核心改變：
  Wave 執行期間 main 不動，所有 slot 互不影響。
  Wave 結束後統一依序 merge，衝突幾乎歸零。

Flow:
  Wave Start:  create N slots (one per task) from main
  Wave Run:    semaphore 控制並行數，完成的 slot 原地等
  Wave End:    依序 merge slot → main，每個 merge 後可驗證
  Next Wave:   cleanup 舊 slots，從更新後的 main 建新 slots
"""

from __future__ import annotations
import asyncio
import logging
import os
import shutil

logger = logging.getLogger(__name__)

WORKSPACE_DIR = ".agent-mesh/workspaces"


class WorkspacePool:
    """
    Wave-based git worktree pool.
    每個 Wave 的每個 task 拿到自己的 slot，Wave 結束統一 merge。
    """

    def __init__(self, repo_dir: str, config: dict):
        self.repo_dir = os.path.abspath(repo_dir)
        self.config = config
        self.workspace_base = os.path.join(self.repo_dir, WORKSPACE_DIR)
        os.makedirs(self.workspace_base, exist_ok=True)
        self._active_slots: dict[int, str] = {}   # slot_id → ws_dir

    # ── Wave Lifecycle ──

    async def setup_wave(self, task_count: int) -> dict[int, str]:
        """
        Wave 開始：建立 N 個 slot，每個 task 一個。
        Returns: {slot_id: workspace_dir}
        """
        # Clean up previous wave's slots
        await self._cleanup_all_slots()

        await self._ensure_base_branch()

        self._active_slots = {}
        for i in range(task_count):
            ws_dir = await self._create_slot(i)
            self._active_slots[i] = ws_dir

        logger.info(f"[WorkspacePool] Wave setup: {task_count} slots ready")
        return dict(self._active_slots)

    async def merge_wave(self, completed_slots: list[int], task_labels: dict[int, str] | None = None) -> dict[int, bool]:
        """
        Wave 結束：依序 merge 所有完成的 slot → main。
        
        Args:
            completed_slots: list of slot_ids that completed successfully
            task_labels: optional {slot_id: task_title} for commit messages
            
        Returns: {slot_id: merge_success}
        """
        results = {}
        task_labels = task_labels or {}

        logger.info(f"\n{'─'*40}")
        logger.info(f"  🔀 Merge Phase: {len(completed_slots)} slots → main")
        logger.info(f"{'─'*40}")

        for slot_id in completed_slots:
            label = task_labels.get(slot_id, f"slot_{slot_id}")
            commit_msg = f"[agent-mesh] {label}"

            success = await self._merge_slot(slot_id, commit_msg)
            results[slot_id] = success

            status = "✅" if success else "❌"
            logger.info(f"  {status} slot_{slot_id}: {label}")

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

    async def cleanup_wave(self):
        """Wave 結束後清理所有 slots。"""
        await self._cleanup_all_slots()
        self._active_slots = {}
        logger.debug("[WorkspacePool] Wave cleanup done")

    def get_slot_dir(self, slot_id: int) -> str:
        """取得 slot 的工作目錄。"""
        return self._active_slots.get(slot_id, "")

    # ── Merge Logic ──

    async def _merge_slot(self, slot_id: int, commit_msg: str) -> bool:
        """
        把單個 slot merge 回 main。
        Strategy: commit in slot → merge to main (try clean, fallback -X theirs)
        """
        ws_dir = os.path.join(self.workspace_base, f"slot_{slot_id}")
        branch_name = f"agent-mesh/slot_{slot_id}"

        try:
            # 1) Commit in slot
            await self._run_git("add -A", cwd=ws_dir)
            await self._run_git(
                f'commit --allow-empty -m "{commit_msg}"',
                cwd=ws_dir
            )

            # 2) Commit any pending changes on main
            try:
                await self._run_git("add -A", cwd=self.repo_dir)
                await self._run_git(
                    'commit --allow-empty -m "[agent-mesh] pre-merge"',
                    cwd=self.repo_dir
                )
            except Exception:
                pass

            # 3) Try clean merge first
            try:
                await self._run_git(
                    f'merge {branch_name} --no-ff -m "Merge {commit_msg}"',
                    cwd=self.repo_dir
                )
                return True
            except Exception:
                pass

            # 4) Clean merge failed — abort and use -X theirs
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
            logger.error(f"[WorkspacePool] Merge slot_{slot_id} failed: {e}")
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
        ws_dir = os.path.join(self.workspace_base, f"slot_{slot_id}")
        branch_name = f"agent-mesh/slot_{slot_id}"

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
        await self._run_git(f"worktree add {ws_dir} -b {branch_name} main")

        # Clear any index.lock
        lock_file = os.path.join(ws_dir, ".git", "index.lock")
        if os.path.exists(lock_file):
            os.remove(lock_file)

        logger.debug(f"[WorkspacePool] Created slot_{slot_id}")
        return ws_dir

    async def _cleanup_all_slots(self):
        """Remove all worktree slots."""
        if not os.path.exists(self.workspace_base):
            return

        for entry in os.listdir(self.workspace_base):
            if entry.startswith("slot_"):
                ws_dir = os.path.join(self.workspace_base, entry)
                try:
                    await self._run_git(f"worktree remove {ws_dir} --force")
                except Exception:
                    shutil.rmtree(ws_dir, ignore_errors=True)

                # Clean up branch
                slot_id = entry.replace("slot_", "")
                try:
                    await self._run_git(f"branch -D agent-mesh/slot_{slot_id}")
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
            await self._run_git("rev-parse --verify main")
        except Exception:
            try:
                await self._run_git("checkout -b main")
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
