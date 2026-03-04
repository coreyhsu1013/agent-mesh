"""
Agent Mesh v0.9 — Experience Store

Central cross-project database at ~/.agent-mesh/experience.db.
Accumulates model performance data across all projects for adaptive routing.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from .cost_tracker import CostResult

logger = logging.getLogger("agent-mesh")


class ExperienceStore:
    """Persistent cross-project experience database."""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_dir = Path.home() / ".agent-mesh"
            db_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(db_dir / "experience.db")
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Create tables if they don't exist."""
        schema_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "db", "experience_schema.sql",
        )
        conn = sqlite3.connect(self.db_path)
        try:
            if os.path.exists(schema_path):
                with open(schema_path) as f:
                    conn.executescript(f.read())
            else:
                # Inline fallback if schema file not found
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS task_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        project_name TEXT NOT NULL,
                        project_type TEXT,
                        task_id TEXT NOT NULL,
                        task_title TEXT,
                        complexity TEXT,
                        category TEXT,
                        module TEXT,
                        model_used TEXT NOT NULL,
                        attempt_number INTEGER,
                        success INTEGER NOT NULL DEFAULT 0,
                        duration_sec REAL,
                        input_tokens INTEGER,
                        output_tokens INTEGER,
                        cost_usd REAL,
                        error_type TEXT,
                        created_at TEXT DEFAULT (datetime('now'))
                    );
                    CREATE TABLE IF NOT EXISTS model_stats (
                        project_type TEXT NOT NULL,
                        complexity TEXT NOT NULL,
                        model TEXT NOT NULL,
                        total_runs INTEGER NOT NULL DEFAULT 0,
                        successes INTEGER NOT NULL DEFAULT 0,
                        avg_duration_sec REAL,
                        avg_cost_usd REAL,
                        success_rate REAL,
                        p50_duration REAL,
                        p90_duration REAL,
                        updated_at TEXT DEFAULT (datetime('now')),
                        PRIMARY KEY (project_type, complexity, model)
                    );
                    CREATE TABLE IF NOT EXISTS project_profiles (
                        project_name TEXT PRIMARY KEY,
                        project_type TEXT,
                        repo_path TEXT,
                        language TEXT,
                        framework TEXT,
                        total_tasks INTEGER DEFAULT 0,
                        total_cost_usd REAL DEFAULT 0,
                        created_at TEXT DEFAULT (datetime('now')),
                        updated_at TEXT DEFAULT (datetime('now'))
                    );
                """)
            conn.commit()
        finally:
            conn.close()

    def record_task_run(
        self,
        project_name: str,
        project_type: str,
        task_id: str,
        task_title: str,
        complexity: str,
        category: str,
        module: str,
        model_used: str,
        attempt_number: int,
        success: bool,
        duration_sec: float,
        cost: CostResult | None = None,
        error_type: str | None = None,
    ):
        """Insert one row per attempt into task_runs."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO task_runs
                   (project_name, project_type, task_id, task_title,
                    complexity, category, module, model_used,
                    attempt_number, success, duration_sec,
                    input_tokens, output_tokens, cost_usd, error_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_name, project_type, task_id, task_title,
                    complexity, category, module, model_used,
                    attempt_number, 1 if success else 0, duration_sec,
                    cost.input_tokens if cost else None,
                    cost.output_tokens if cost else None,
                    cost.estimated_usd if cost else None,
                    error_type,
                ),
            )
            conn.commit()
        except Exception as e:
            logger.warning(f"[ExperienceStore] Failed to record run: {e}")
        finally:
            conn.close()

    def refresh_model_stats(self):
        """Recompute model_stats from task_runs."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM model_stats")
            conn.execute("""
                INSERT INTO model_stats
                    (project_type, complexity, model, total_runs, successes,
                     avg_duration_sec, avg_cost_usd, success_rate,
                     p50_duration, p90_duration, updated_at)
                SELECT
                    COALESCE(project_type, 'unknown'),
                    complexity,
                    model_used,
                    COUNT(*),
                    SUM(success),
                    AVG(duration_sec),
                    AVG(cost_usd),
                    CAST(SUM(success) AS REAL) / COUNT(*),
                    NULL,  -- p50 computed separately if needed
                    NULL,  -- p90 computed separately if needed
                    datetime('now')
                FROM task_runs
                WHERE complexity IS NOT NULL
                GROUP BY COALESCE(project_type, 'unknown'), complexity, model_used
            """)
            conn.commit()
            logger.debug("[ExperienceStore] model_stats refreshed")
        except Exception as e:
            logger.warning(f"[ExperienceStore] Failed to refresh stats: {e}")
        finally:
            conn.close()

    def get_model_success_rate(
        self, project_type: str, complexity: str, model: str
    ) -> tuple[float, int]:
        """
        Returns (success_rate, sample_count) for a specific combination.
        Returns (0.0, 0) if no data.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                """SELECT success_rate, total_runs FROM model_stats
                   WHERE project_type = ? AND complexity = ? AND model = ?""",
                (project_type, complexity, model),
            ).fetchone()
            if row:
                return (row[0] or 0.0, row[1] or 0)
            return (0.0, 0)
        finally:
            conn.close()

    def get_project_profile(self, project_name: str) -> dict | None:
        """Look up cached project classification."""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                """SELECT project_name, project_type, repo_path, language,
                          framework, total_tasks, total_cost_usd
                   FROM project_profiles WHERE project_name = ?""",
                (project_name,),
            ).fetchone()
            if row:
                return {
                    "project_name": row[0],
                    "project_type": row[1],
                    "repo_path": row[2],
                    "language": row[3],
                    "framework": row[4],
                    "total_tasks": row[5],
                    "total_cost_usd": row[6],
                }
            return None
        finally:
            conn.close()

    def update_project_profile(self, project_name: str, **kwargs):
        """Upsert project profile."""
        conn = sqlite3.connect(self.db_path)
        try:
            existing = conn.execute(
                "SELECT project_name FROM project_profiles WHERE project_name = ?",
                (project_name,),
            ).fetchone()

            if existing:
                sets = []
                vals = []
                for k, v in kwargs.items():
                    sets.append(f"{k} = ?")
                    vals.append(v)
                sets.append("updated_at = datetime('now')")
                vals.append(project_name)
                conn.execute(
                    f"UPDATE project_profiles SET {', '.join(sets)} WHERE project_name = ?",
                    vals,
                )
            else:
                cols = ["project_name"] + list(kwargs.keys())
                vals = [project_name] + list(kwargs.values())
                placeholders = ", ".join("?" * len(vals))
                conn.execute(
                    f"INSERT INTO project_profiles ({', '.join(cols)}) VALUES ({placeholders})",
                    vals,
                )
            conn.commit()
        except Exception as e:
            logger.warning(f"[ExperienceStore] Failed to update profile: {e}")
        finally:
            conn.close()

    def add_project_cost(self, project_name: str, cost_usd: float):
        """Increment total_cost_usd for a project."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """UPDATE project_profiles
                   SET total_cost_usd = COALESCE(total_cost_usd, 0) + ?,
                       total_tasks = COALESCE(total_tasks, 0) + 1,
                       updated_at = datetime('now')
                   WHERE project_name = ?""",
                (cost_usd, project_name),
            )
            conn.commit()
        except Exception as e:
            logger.warning(f"[ExperienceStore] Failed to add cost: {e}")
        finally:
            conn.close()

    def get_all_model_stats(self, project_type: str | None = None) -> list[dict]:
        """Get all model stats, optionally filtered by project type."""
        conn = sqlite3.connect(self.db_path)
        try:
            if project_type:
                rows = conn.execute(
                    """SELECT project_type, complexity, model, total_runs,
                              successes, avg_duration_sec, avg_cost_usd, success_rate
                       FROM model_stats WHERE project_type = ?
                       ORDER BY complexity, success_rate DESC""",
                    (project_type,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT project_type, complexity, model, total_runs,
                              successes, avg_duration_sec, avg_cost_usd, success_rate
                       FROM model_stats
                       ORDER BY project_type, complexity, success_rate DESC""",
                ).fetchall()
            return [
                {
                    "project_type": r[0], "complexity": r[1], "model": r[2],
                    "total_runs": r[3], "successes": r[4],
                    "avg_duration_sec": r[5], "avg_cost_usd": r[6],
                    "success_rate": r[7],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def close(self):
        """No persistent connection to close, but kept for interface compatibility."""
        pass
