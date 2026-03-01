"""
Agent Mesh v0.6.5 — Workspace Pool
每個並行 task 都拿到自己專屬的 git worktree slot。
解決多個 task 共用同一個 worktree 互相踩踏的問題。

Before (v0.6.0-v0.6.2):
  deepseek_aider/  ← Task A + Task B 同時改 → 互踩 → 失敗

After (v0.6.5):
  slot_0/  ← Task A 獨佔
  slot_1/  ← Task B 獨佔
  slot_2/  ← Task C 獨佔
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
    Git worktree pool：每個並行 task 拿到自己的 slot。
    Slot 數量 = max_parallel（config.dispatcher.max_parallel）。
    """

    def __init__(self, repo_dir: str, config: dict):
        self.repo_dir = os.path.abspath(repo_dir)
        self.config = config
        self.workspace_base = os.path.join(self.repo_dir, WORKSPACE_DIR)
        os.makedirs(self.workspace_base, exist_ok=True)

        max_parallel = config.get("dispatcher", {}).get("max_parallel", 4)
        self._slot_count = max_parallel
        self._locks: list[asyncio.Lock] = []

    async def setup(self):
        """初始化所有 worktree slots。"""
        self._locks = [asyncio.Lock() for _ in range(self._slot_count)]

        await self._ensure_base_branch()
        for i in range(self._slot_count):
            await self._create_slot(i)
        logger.info(f"[WorkspacePool] {self._slot_count} slots ready")

    async def acquire(self) -> tuple[int, str]:
        """
        取得一個可用的 worktree slot（會 block 直到有空位）。
        Returns: (slot_id, workspace_dir)
        """
        while True:
            for i, lock in enumerate(self._locks):
                if not lock.locked():
                    await lock.acquire()
                    ws_dir = os.path.join(self.workspace_base, f"slot_{i}")

                    # Reset to latest main
                    try:
                        await self._run_git("reset --hard main", cwd=ws_dir)
                        await self._run_git("clean -fd", cwd=ws_dir)
                    except Exception as e:
                        logger.warning(f"[WorkspacePool] Reset slot_{i} failed: {e}")

                    logger.info(f"[WorkspacePool] Acquired slot_{i}")
                    return i, ws_dir

            await asyncio.sleep(0.5)

    def release(self, slot_id: int):
        """釋放 worktree slot。"""
        if 0 <= slot_id < len(self._locks) and self._locks[slot_id].locked():
            self._locks[slot_id].release()
            logger.debug(f"[WorkspacePool] Released slot_{slot_id}")

    async def merge_to_main(self, slot_id: int, commit_msg: str = "") -> bool:
        """把 slot 的改動 merge 回 main。"""
        ws_dir = os.path.join(self.workspace_base, f"slot_{slot_id}")
        branch_name = f"agent-mesh/slot_{slot_id}"

        if not commit_msg:
            commit_msg = f"[agent-mesh] slot_{slot_id}"

        try:
            # 1) Commit in slot
            await self._run_git("add -A", cwd=ws_dir)
            await self._run_git(
                f'commit --allow-empty -m "{commit_msg}"',
                cwd=ws_dir
            )

            # 2) Commit main（防 uncommitted changes 擋 merge）
            try:
                await self._run_git("add -A", cwd=self.repo_dir)
                await self._run_git(
                    'commit --allow-empty -m "[agent-mesh] pre-merge"',
                    cwd=self.repo_dir
                )
            except Exception:
                pass

            # 3) Rebase slot on main (handle conflicts gracefully)
            try:
                await self._run_git("rebase main", cwd=ws_dir)
            except Exception:
                try:
                    await self._run_git("rebase --abort", cwd=ws_dir)
                except Exception:
                    pass
                try:
                    await self._run_git("merge main --no-edit", cwd=ws_dir)
                except Exception:
                    # ★ Both failed: abort merge and continue with slot as-is
                    try:
                        await self._run_git("merge --abort", cwd=ws_dir)
                    except Exception:
                        await self._run_git("reset --hard HEAD", cwd=ws_dir)

            # 4) Merge to main
            await self._run_git(
                f'merge {branch_name} --no-ff -m "Merge {commit_msg}"',
                cwd=self.repo_dir
            )

            logger.info(f"[WorkspacePool] Merged slot_{slot_id} → main")
            return True

        except Exception as e:
            logger.warning(f"[WorkspacePool] Normal merge failed: {e}")
            try:
                # ★ Step 1: Abort the failed merge (clean working tree)
                try:
                    await self._run_git("merge --abort", cwd=self.repo_dir)
                except Exception:
                    await self._run_git("reset --hard HEAD", cwd=self.repo_dir)

                # ★ Step 2: Force merge with -X theirs (auto-resolve conflicts)
                await self._run_git(
                    f'merge {branch_name} -X theirs --no-ff -m "Force merge {commit_msg}"',
                    cwd=self.repo_dir
                )

                # ★ Step 3: Post-merge conflict marker scan (safety net)
                conflicts = await self._scan_conflict_markers()
                if conflicts:
                    logger.warning(
                        f"[WorkspacePool] ⚠️ {len(conflicts)} files with conflict markers after merge: "
                        f"{', '.join(conflicts[:5])}"
                    )

                logger.info(f"[WorkspacePool] Force-merged slot_{slot_id} → main")
                return True
            except Exception as e2:
                logger.error(f"[WorkspacePool] Force merge also failed: {e2}")
                return False

    async def cleanup(self):
        """清除所有 worktree slots。"""
        for i in range(self._slot_count):
            ws_dir = os.path.join(self.workspace_base, f"slot_{i}")
            if os.path.exists(ws_dir):
                try:
                    await self._run_git(f"worktree remove {ws_dir} --force")
                except Exception:
                    shutil.rmtree(ws_dir, ignore_errors=True)

    # ── Internal ──

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

    async def _ensure_base_branch(self):
        try:
            await self._run_git("rev-parse HEAD")
        except Exception:
            await self._run_git("add -A")
            await self._run_git('commit --allow-empty -m "Initial commit"')
        try:
            await self._run_git("worktree prune")
        except Exception:
            pass

        # ★ 清除 stale index.lock files（git crash 殘留）
        import glob
        for lock in glob.glob(os.path.join(self.repo_dir, ".git/worktrees/*/index.lock")):
            try:
                os.remove(lock)
                logger.info(f"[WorkspacePool] Removed stale lock: {lock}")
            except Exception:
                pass
        # Main repo lock too
        main_lock = os.path.join(self.repo_dir, ".git/index.lock")
        if os.path.exists(main_lock):
            try:
                os.remove(main_lock)
                logger.info(f"[WorkspacePool] Removed stale main lock")
            except Exception:
                pass

    async def _create_slot(self, slot_id: int):
        ws_dir = os.path.join(self.workspace_base, f"slot_{slot_id}")
        branch_name = f"agent-mesh/slot_{slot_id}"

        if os.path.exists(ws_dir):
            return

        try:
            try:
                await self._run_git(f"branch {branch_name}")
            except Exception:
                pass
            await self._run_git(f"worktree add {ws_dir} {branch_name}")
            logger.debug(f"[WorkspacePool] Created slot_{slot_id}")
        except Exception as e:
            logger.warning(f"[WorkspacePool] Slot creation failed: {e}")
            os.makedirs(ws_dir, exist_ok=True)
            await self._run_cmd(
                f"git archive HEAD | tar -x -C {ws_dir}",
                cwd=self.repo_dir
            )

    async def _run_git(self, args: str, cwd: str | None = None) -> str:
        return await self._run_cmd(f"git {args}", cwd=cwd or self.repo_dir)

    @staticmethod
    async def _run_cmd(cmd: str, cwd: str | None = None) -> str:
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(
                f"Command failed ({proc.returncode}): {cmd}\n{stderr.decode()[:500]}"
            )
        return stdout.decode().strip()
