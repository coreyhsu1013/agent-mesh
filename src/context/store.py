"""
Agent Mesh v0.6.5 — Context Store
SQLite-based persistence for tasks, runs, and state.
Supports resume after crash/interrupt.
"""

from __future__ import annotations
import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime
from typing import Optional

from ..models.task import Task, TaskPlan, TaskStatus

logger = logging.getLogger(__name__)

DB_DIR = ".agent-mesh"
DB_FILE = "context.db"
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "db", "schema.sql")


class ContextStore:
    """SQLite-backed store for task execution state."""

    def __init__(self, repo_dir: str):
        self.db_dir = os.path.join(repo_dir, DB_DIR)
        os.makedirs(self.db_dir, exist_ok=True)
        self.db_path = os.path.join(self.db_dir, DB_FILE)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        """Initialize DB schema if not exists."""
        cursor = self.conn.cursor()
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                agent_type TEXT DEFAULT '',
                complexity TEXT DEFAULT 'M',
                module TEXT DEFAULT 'core',
                target_files TEXT DEFAULT '[]',
                dependencies TEXT DEFAULT '[]',
                acceptance_criteria TEXT DEFAULT '',
                priority INTEGER DEFAULT 1,
                status TEXT DEFAULT 'pending',
                agent_used TEXT DEFAULT '',
                attempts INTEGER DEFAULT 1,
                react_history TEXT DEFAULT '[]',
                observation TEXT DEFAULT '',
                routed_by TEXT DEFAULT 'auto',
                duration_sec REAL DEFAULT 0.0,
                diff TEXT DEFAULT '',
                error TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                plan_json TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                total_tasks INTEGER DEFAULT 0,
                completed_tasks INTEGER DEFAULT 0,
                failed_tasks INTEGER DEFAULT 0,
                config_version TEXT DEFAULT '0.6.5',
                claude_tasks INTEGER DEFAULT 0,
                deepseek_tasks INTEGER DEFAULT 0,
                gemini_tasks INTEGER DEFAULT 0,
                total_duration_sec REAL DEFAULT 0.0,
                total_react_attempts INTEGER DEFAULT 0
            );
        """)
        self.conn.commit()

    # ── Task Operations ──

    def save_plan(self, plan: TaskPlan, run_id: str | None = None) -> str:
        """Save all tasks from a plan. Returns run_id."""
        if not run_id:
            run_id = str(uuid.uuid4())

        cursor = self.conn.cursor()

        # Save run
        cursor.execute(
            "INSERT OR REPLACE INTO runs (id, plan_json, total_tasks, config_version) VALUES (?, ?, ?, ?)",
            (run_id, json.dumps(plan.to_dict(), ensure_ascii=False), len(plan.tasks), "0.6.5")
        )

        # Save tasks
        for task in plan.tasks:
            self._upsert_task(cursor, task)

        self.conn.commit()
        logger.info(f"[Store] Saved plan with {len(plan.tasks)} tasks (run={run_id})")
        return run_id

    def _upsert_task(self, cursor: sqlite3.Cursor, task: Task):
        """Insert or update a task. Preserves completed status on re-import."""
        # Check if task already exists with completed status
        cursor.execute("SELECT status FROM tasks WHERE id = ?", (task.id,))
        existing = cursor.fetchone()
        if existing and existing[0] == "completed":
            # Don't overwrite completed tasks — just update metadata
            cursor.execute("""
                UPDATE tasks SET
                    title = ?, description = ?, complexity = ?, module = ?,
                    target_files = ?, dependencies = ?, acceptance_criteria = ?,
                    priority = ?
                WHERE id = ?
            """, (
                task.title, task.description, task.complexity, task.module,
                json.dumps(task.target_files), json.dumps(task.dependencies),
                task.acceptance_criteria, task.priority, task.id,
            ))
            return

        cursor.execute("""
            INSERT OR REPLACE INTO tasks
            (id, title, description, agent_type, complexity, module,
             target_files, dependencies, acceptance_criteria, priority,
             status, agent_used, attempts, react_history, observation,
             routed_by, duration_sec, diff, error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task.id, task.title, task.description, task.agent_type,
            task.complexity, task.module,
            json.dumps(task.target_files), json.dumps(task.dependencies),
            task.acceptance_criteria, task.priority, task.status,
            task.agent_used, task.attempts, task.react_history,
            "", task.routed_by, task.duration_sec, task.diff, task.error,
            datetime.now().isoformat(),
        ))

    def update_task(self, task: Task):
        """Update task status and execution results."""
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE tasks SET
                status = ?, agent_used = ?, attempts = ?,
                react_history = ?, routed_by = ?,
                duration_sec = ?, diff = ?, error = ?,
                updated_at = ?
            WHERE id = ?
        """, (
            task.status, task.agent_used, task.attempts,
            task.react_history, task.routed_by,
            task.duration_sec,
            task.diff,
            task.error,
            datetime.now().isoformat(),
            task.id,
        ))
        self.conn.commit()

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get a single task by ID."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_task(row)

    def get_all_tasks(self) -> list[Task]:
        """Get all tasks from the current run."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM tasks ORDER BY priority, module")
        return [self._row_to_task(row) for row in cursor.fetchall()]

    def get_pending_tasks(self) -> list[Task]:
        """Get tasks that haven't been completed or skipped."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM tasks WHERE status IN ('pending', 'failed') ORDER BY priority, module"
        )
        return [self._row_to_task(row) for row in cursor.fetchall()]

    def get_completed_tasks(self) -> list[Task]:
        """Get completed tasks."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE status = 'completed' ORDER BY priority")
        return [self._row_to_task(row) for row in cursor.fetchall()]

    # ── Run Operations ──

    def update_run(self, run_id: str, **kwargs):
        """Update run statistics."""
        set_parts = []
        values = []
        for k, v in kwargs.items():
            set_parts.append(f"{k} = ?")
            values.append(v)
        values.append(run_id)

        cursor = self.conn.cursor()
        cursor.execute(
            f"UPDATE runs SET {', '.join(set_parts)} WHERE id = ?",
            values
        )
        self.conn.commit()

    def get_execution_stats(self) -> dict:
        """Get execution statistics for the current run."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status")
        status_counts = {row["status"]: row["cnt"] for row in cursor.fetchall()}

        cursor.execute("SELECT agent_used, COUNT(*) as cnt FROM tasks WHERE agent_used != '' GROUP BY agent_used")
        agent_counts = {row["agent_used"]: row["cnt"] for row in cursor.fetchall()}

        cursor.execute("SELECT SUM(attempts) as total_attempts, AVG(attempts) as avg_attempts FROM tasks WHERE status = 'completed'")
        row = cursor.fetchone()
        react_stats = {
            "total_attempts": row["total_attempts"] or 0,
            "avg_attempts": round(row["avg_attempts"] or 0, 1),
        }

        cursor.execute("SELECT COUNT(*) as cnt FROM tasks WHERE status = 'completed' AND attempts = 1")
        react_stats["first_attempt_success"] = cursor.fetchone()["cnt"]

        cursor.execute("SELECT COUNT(*) as cnt FROM tasks WHERE status = 'completed' AND attempts > 1")
        react_stats["required_retry"] = cursor.fetchone()["cnt"]

        return {
            "status": status_counts,
            "agents": agent_counts,
            "react": react_stats,
        }

    # ── Helpers ──

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        """Convert DB row to Task object."""
        d = dict(row)
        # Parse JSON fields
        for field in ("target_files", "dependencies"):
            if isinstance(d.get(field), str):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    d[field] = []
        return Task.from_dict(d)

    def close(self):
        """Close DB connection."""
        if self.conn:
            self.conn.close()
