"""
Agent Mesh v1.0 — Deployer

Deploys project to target host via rsync + SSH.
Runs migration, build, pm2 restart, and health check.

Usage:
  - Standalone: --deploy --repo ~/afatech-erp
  - After Design Pipeline: --evolve ... --deploy
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("agent-mesh")


@dataclass
class DeployResult:
    """Result of a deployment attempt."""
    success: bool = False
    steps: list[dict] = field(default_factory=list)
    duration_s: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "steps": self.steps,
            "duration_s": self.duration_s,
            "error": self.error,
        }

    def summary(self) -> str:
        status = "✅ DEPLOYED" if self.success else f"❌ FAILED: {self.error}"
        parts = [f"[Deploy] {status} ({self.duration_s:.0f}s)"]
        for step in self.steps:
            icon = "✅" if step.get("ok") else "❌"
            parts.append(f"  {icon} {step['name']}: {step.get('detail', '')}")
        return "\n".join(parts)


class Deployer:
    """Deploys project to target host."""

    def __init__(self, config: dict, repo_dir: str):
        self.config = config
        self.repo_dir = repo_dir
        deploy_cfg = config.get("deploy", {})
        self.host = deploy_cfg.get("host", "")
        self.user = deploy_cfg.get("user", "")
        self.password = deploy_cfg.get("password", "")
        self.remote_dir = deploy_cfg.get("remote_dir", "")
        self.port = deploy_cfg.get("port", 22)
        self.exclude = deploy_cfg.get("exclude", [
            ".git", ".agent-mesh", "node_modules", "__pycache__",
            ".env", "*.pyc", "uploads", ".next",
        ])
        self.pre_deploy_cmd = deploy_cfg.get("pre_deploy_cmd", "")
        self.post_deploy_cmd = deploy_cfg.get("post_deploy_cmd", "")
        self.health_check_url = deploy_cfg.get("health_check_url", "")
        self.backup_before_deploy = deploy_cfg.get("backup", True)

    async def deploy(self) -> DeployResult:
        """
        Full deployment flow:
        1. Pre-check (host, remote_dir configured)
        2. Backup remote (optional)
        3. Rsync files to remote
        4. Run deploy script on remote (pip install, migration, npm build, pm2)
        5. Health check
        """
        t0 = time.time()
        result = DeployResult()

        if not self.host or not self.user or not self.remote_dir:
            result.error = (
                "Deploy not configured. Set deploy.host, deploy.user, "
                "deploy.remote_dir in config.yaml"
            )
            logger.error(f"[Deployer] {result.error}")
            return result

        logger.info(f"\n{'='*60}")
        logger.info(f"  🚀 Deploying to {self.user}@{self.host}:{self.remote_dir}")
        logger.info(f"{'='*60}")

        ssh_prefix = self._ssh_prefix()

        # Step 1: Backup
        if self.backup_before_deploy:
            step = await self._run_step(
                "backup",
                f'{ssh_prefix} "cd {self.remote_dir} && '
                f'tar czf ../afatech-backup-$(date +%Y%m%d-%H%M%S).tar.gz '
                f'--exclude=node_modules --exclude=.next --exclude=uploads '
                f'--exclude=__pycache__ . 2>/dev/null || true"',
            )
            result.steps.append(step)
            if not step["ok"]:
                logger.warning("[Deployer] Backup failed, continuing anyway")

        # Step 2: Pre-deploy command (e.g., build frontend locally)
        if self.pre_deploy_cmd:
            step = await self._run_step(
                "pre-deploy",
                self.pre_deploy_cmd,
                cwd=self.repo_dir,
            )
            result.steps.append(step)
            if not step["ok"]:
                result.error = f"Pre-deploy failed: {step.get('detail', '')}"
                result.duration_s = time.time() - t0
                return result

        # Step 3: Rsync
        exclude_args = " ".join(f'--exclude="{e}"' for e in self.exclude)
        rsync_cmd = (
            f'rsync -avz --delete {exclude_args} '
            f'-e "sshpass -p \'{self.password}\' ssh -p {self.port} -o StrictHostKeyChecking=no" '
            f'{self.repo_dir}/ {self.user}@{self.host}:{self.remote_dir}/'
        )
        step = await self._run_step("rsync", rsync_cmd, timeout=300)
        result.steps.append(step)
        if not step["ok"]:
            result.error = f"Rsync failed: {step.get('detail', '')}"
            result.duration_s = time.time() - t0
            return result

        # Step 4: Remote deploy script
        deploy_script = (
            f'cd {self.remote_dir} && '
            f'bash scripts/deploy.sh'
        )
        step = await self._run_step(
            "remote-deploy",
            f'{ssh_prefix} "{deploy_script}"',
            timeout=600,
        )
        result.steps.append(step)
        if not step["ok"]:
            result.error = f"Remote deploy failed: {step.get('detail', '')}"
            result.duration_s = time.time() - t0
            return result

        # Step 5: Post-deploy command
        if self.post_deploy_cmd:
            step = await self._run_step(
                "post-deploy",
                f'{ssh_prefix} "{self.post_deploy_cmd}"',
            )
            result.steps.append(step)

        # Step 6: Health check
        if self.health_check_url:
            step = await self._health_check()
            result.steps.append(step)
            if not step["ok"]:
                result.error = f"Health check failed: {step.get('detail', '')}"
                result.duration_s = time.time() - t0
                return result

        result.success = True
        result.duration_s = time.time() - t0
        logger.info(f"\n{result.summary()}")

        # Save deploy result
        mesh_dir = os.path.join(self.repo_dir, ".agent-mesh")
        os.makedirs(mesh_dir, exist_ok=True)
        result_path = os.path.join(mesh_dir, "deploy-result.json")
        with open(result_path, 'w') as f:
            json.dump(result.to_dict(), f, indent=2, ensure_ascii=False)

        return result

    async def _run_step(
        self, name: str, cmd: str, timeout: int = 120, cwd: str | None = None
    ) -> dict:
        """Run a deploy step and return result dict."""
        logger.info(f"[Deployer] Running: {name}...")
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            ok = proc.returncode == 0
            output = stdout.decode().strip()
            err = stderr.decode().strip()
            detail = output[-500:] if ok else (err or output)[-500:]
            if ok:
                logger.info(f"[Deployer] ✅ {name}")
            else:
                logger.warning(f"[Deployer] ❌ {name}: {detail[:200]}")
            return {"name": name, "ok": ok, "detail": detail}
        except asyncio.TimeoutError:
            logger.error(f"[Deployer] ❌ {name}: timeout ({timeout}s)")
            return {"name": name, "ok": False, "detail": f"timeout ({timeout}s)"}
        except Exception as e:
            logger.error(f"[Deployer] ❌ {name}: {e}")
            return {"name": name, "ok": False, "detail": str(e)}

    async def _health_check(self) -> dict:
        """Check if the deployed service is responding."""
        logger.info(f"[Deployer] Health check: {self.health_check_url}")
        # Wait a few seconds for services to start
        await asyncio.sleep(5)
        try:
            proc = await asyncio.create_subprocess_shell(
                f'curl -sf -o /dev/null -w "%{{http_code}}" {self.health_check_url}',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            status_code = stdout.decode().strip()
            ok = status_code.startswith("2") or status_code == "301"
            detail = f"HTTP {status_code}"
            if ok:
                logger.info(f"[Deployer] ✅ Health check: {detail}")
            else:
                logger.warning(f"[Deployer] ❌ Health check: {detail}")
            return {"name": "health-check", "ok": ok, "detail": detail}
        except Exception as e:
            return {"name": "health-check", "ok": False, "detail": str(e)}

    def _ssh_prefix(self) -> str:
        """Build SSH command prefix with sshpass."""
        return (
            f'sshpass -p \'{self.password}\' ssh '
            f'-p {self.port} -o StrictHostKeyChecking=no '
            f'{self.user}@{self.host}'
        )
