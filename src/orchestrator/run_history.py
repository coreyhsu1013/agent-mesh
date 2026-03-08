"""
Agent Mesh v1.3 — Run History Recorder

結構化記錄每一步：model / duration / cost / merge / build / verify / gaps
未來用於評估模型效能、routing 策略、收斂速度。

存檔：.agent-mesh/run-history.json
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import subprocess
import time
from typing import Any

logger = logging.getLogger("agent-mesh")


class RunHistoryRecorder:
    """Append-only structured run history."""

    def __init__(self, repo_dir: str):
        self.repo_dir = repo_dir
        self.mesh_dir = os.path.join(repo_dir, ".agent-mesh")
        self.history_path = os.path.join(self.mesh_dir, "run-history.json")
        os.makedirs(self.mesh_dir, exist_ok=True)

        # Current run state (in-memory, flushed to disk after each write)
        self._current_run: dict | None = None
        self._chunks: dict[str, dict] = {}  # chunk_id → chunk data
        self._run_start: float = 0

    # ── Run lifecycle ──

    def start_run(self, run_id: str, config: dict) -> None:
        """Start a new run entry."""
        self._run_start = time.time()
        self._current_run = {
            "run_id": run_id,
            "started_at": _now(),
            "finished_at": None,
            "config": {
                "spec_old": config.get("spec_old", ""),
                "spec_new": config.get("spec_new", ""),
                "max_parallel": config.get("max_parallel") or config.get("dispatcher", {}).get("max_parallel", 4),
                "force_model": config.get("force_model", None),
                "manual_mode": config.get("manual_mode", False),
            },
            "summary": {},
            "chunks": [],
        }
        self._chunks = {}
        logger.info(f"[RunHistory] Started run: {run_id}")

    def end_run(self) -> None:
        """Finalize current run with summary stats."""
        if not self._current_run:
            return

        self._current_run["finished_at"] = _now()

        # Build summary from chunks
        chunks = list(self._chunks.values())
        total_cycles = sum(len(c.get("cycles", [])) for c in chunks)
        total_tasks = sum(
            sum(
                cy.get("execution", {}).get("task_count", 0)
                for cy in c.get("cycles", [])
            )
            for c in chunks
        )
        total_cost = sum(c.get("total_cost_usd", 0) for c in chunks)

        self._current_run["summary"] = {
            "total_chunks": len(chunks),
            "completed_chunks": sum(
                1 for c in chunks if c.get("status") == "completed"
            ),
            "total_cost_usd": round(total_cost, 4),
            "total_duration_sec": round(time.time() - self._run_start, 1),
            "total_cycles": total_cycles,
            "total_tasks_executed": total_tasks,
        }

        self._current_run["chunks"] = chunks
        self._flush()
        logger.info(
            f"[RunHistory] Run complete: {len(chunks)} chunks, "
            f"${total_cost:.4f}, {total_cycles} cycles"
        )

    # ── Chunk lifecycle ──

    def start_chunk(self, chunk_id: str, title: str, wave_order: int = 0) -> None:
        """Start tracking a new chunk."""
        self._chunks[chunk_id] = {
            "chunk_id": chunk_id,
            "title": title,
            "wave_order": wave_order,
            "started_at": _now(),
            "finished_at": None,
            "status": "in_progress",
            "total_cycles": 0,
            "final_gaps": None,
            "total_cost_usd": 0,
            "cycles": [],
        }

    def end_chunk(self, chunk_id: str, status: str, final_gaps: int) -> None:
        """Finalize a chunk."""
        chunk = self._chunks.get(chunk_id)
        if not chunk:
            return

        chunk["finished_at"] = _now()
        chunk["status"] = status
        chunk["final_gaps"] = final_gaps
        chunk["total_cycles"] = len(chunk["cycles"])
        chunk["total_cost_usd"] = round(
            sum(cy.get("cost_usd", 0) for cy in chunk["cycles"]), 4
        )
        self._flush()

    # ── Cycle recording ──

    def record_cycle(
        self,
        chunk_id: str,
        cycle: int,
        *,
        duration_sec: float = 0,
        cost_usd: float = 0,
        commit_before: str = "",
        commit_after: str = "",
        execution: dict | None = None,
        merge: dict | None = None,
        verify: dict | None = None,
        escalation: dict | None = None,
    ) -> None:
        """Record a complete cycle's data."""
        chunk = self._chunks.get(chunk_id)
        if not chunk:
            # Auto-create chunk if not started
            self.start_chunk(chunk_id, chunk_id)
            chunk = self._chunks[chunk_id]

        # Auto-increment: use sequential number to handle retries
        actual_cycle = len(chunk["cycles"]) + 1

        cycle_data = {
            "cycle": actual_cycle,
            "timestamp": _now(),
            "duration_sec": round(duration_sec, 1),
            "cost_usd": round(cost_usd, 4),
            "commit_before": commit_before,
            "commit_after": commit_after,
            "execution": execution or {},
            "merge": merge or {},
            "verify": verify or {},
            "escalation": escalation or {},
        }

        chunk["cycles"].append(cycle_data)
        self._flush()

        gap_count = (verify or {}).get("total_gaps", "?")
        logger.info(
            f"[RunHistory] Recorded: {chunk_id}/cycle-{actual_cycle} "
            f"gaps={gap_count} cost=${cost_usd:.4f}"
        )

    # ── Helpers ──

    def get_current_commit(self) -> str:
        """Get current git HEAD commit hash."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=self.repo_dir, capture_output=True, text=True,
            )
            return result.stdout.strip()
        except Exception:
            return "unknown"

    def _flush(self) -> None:
        """Write current run to disk (append to existing history)."""
        if not self._current_run:
            return

        # Update chunks in current run
        self._current_run["chunks"] = list(self._chunks.values())

        # Load existing history
        history = self._load_history()

        # Find and update current run, or append
        run_id = self._current_run["run_id"]
        found = False
        for i, run in enumerate(history["runs"]):
            if run["run_id"] == run_id:
                history["runs"][i] = self._current_run
                found = True
                break
        if not found:
            history["runs"].append(self._current_run)

        # Write atomically
        tmp_path = self.history_path + ".tmp"
        with open(tmp_path, 'w') as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, self.history_path)

    def _load_history(self) -> dict:
        """Load existing history file."""
        if os.path.exists(self.history_path):
            try:
                with open(self.history_path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {"runs": []}

    # ── Static query methods ──

    @staticmethod
    def load(repo_dir: str) -> dict:
        """Load full history for analysis."""
        path = os.path.join(repo_dir, ".agent-mesh", "run-history.json")
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {"runs": []}

    @staticmethod
    def print_summary(repo_dir: str) -> None:
        """Print human-readable summary of all runs."""
        history = RunHistoryRecorder.load(repo_dir)
        runs = history.get("runs", [])
        if not runs:
            print("No run history recorded yet.")
            return

        print(f"\n{'='*70}")
        print(f"  Run History ({len(runs)} runs)")
        print(f"{'='*70}")

        for run in runs:
            s = run.get("summary", {})
            print(
                f"\n  {run['run_id']}  "
                f"{run.get('started_at', '?')[:19]}"
            )
            print(
                f"    Chunks: {s.get('completed_chunks', '?')}/{s.get('total_chunks', '?')}  "
                f"Cycles: {s.get('total_cycles', '?')}  "
                f"Tasks: {s.get('total_tasks_executed', '?')}  "
                f"Cost: ${s.get('total_cost_usd', 0):.4f}  "
                f"Time: {s.get('total_duration_sec', 0):.0f}s"
            )

            for chunk in run.get("chunks", []):
                status_icon = "✅" if chunk.get("status") == "completed" else "❌"
                print(
                    f"    {status_icon} {chunk['chunk_id']}  "
                    f"cycles={chunk.get('total_cycles', '?')}  "
                    f"gaps={chunk.get('final_gaps', '?')}  "
                    f"${chunk.get('total_cost_usd', 0):.4f}"
                )

                for cy in chunk.get("cycles", []):
                    v = cy.get("verify", {})
                    e = cy.get("execution", {})
                    gaps = v.get("total_gaps", "?")
                    build = "✅" if v.get("build_ok") else "❌"
                    tasks_ok = e.get("completed", "?")
                    tasks_total = e.get("task_count", "?")
                    model_summary = _summarize_models(e.get("tasks", []))
                    print(
                        f"      cycle {cy['cycle']}: "
                        f"tasks={tasks_ok}/{tasks_total}  "
                        f"build={build}  "
                        f"gaps={gaps}  "
                        f"${cy.get('cost_usd', 0):.4f}  "
                        f"{cy.get('duration_sec', 0):.0f}s  "
                        f"models=[{model_summary}]"
                    )

        print()


def _now() -> str:
    return datetime.datetime.now().isoformat()


def _summarize_models(tasks: list[dict]) -> str:
    """Summarize models used across tasks."""
    models: dict[str, int] = {}
    for t in tasks:
        m = t.get("final_model", "?")
        # Shorten model name
        short = m.split("/")[-1] if "/" in m else m
        models[short] = models.get(short, 0) + 1
    return ", ".join(f"{m}×{n}" for m, n in models.items())
