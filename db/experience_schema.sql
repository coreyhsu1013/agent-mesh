-- Agent Mesh v0.9 — Experience DB Schema
-- Central experience database at ~/.agent-mesh/experience.db
-- Cross-project: accumulates data from all projects

CREATE TABLE IF NOT EXISTS task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL,
    project_type TEXT,                       -- "web" | "erp" | "embedded" | "iot" | "chip"
    task_id TEXT NOT NULL,
    task_title TEXT,
    complexity TEXT,                          -- L/S/M/H
    category TEXT,                            -- backend/frontend/fullstack
    module TEXT,
    model_used TEXT NOT NULL,
    attempt_number INTEGER,
    success INTEGER NOT NULL DEFAULT 0,      -- 0/1
    duration_sec REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    error_type TEXT,                          -- null | "timeout" | "parse_error" | "build_fail" | "test_fail" | "review_reject"
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS model_stats (
    -- Materialized aggregate, refreshed after each wave
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
    project_type TEXT,                       -- "web" | "erp" | "embedded" | "iot" | "chip"
    repo_path TEXT,
    language TEXT,                            -- "typescript" | "python" | "c++" | ...
    framework TEXT,                           -- "nextjs" | "fastapi" | "react-native" | ...
    total_tasks INTEGER DEFAULT 0,
    total_cost_usd REAL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_task_runs_project ON task_runs(project_name, created_at);
CREATE INDEX IF NOT EXISTS idx_task_runs_model ON task_runs(model_used, success);
CREATE INDEX IF NOT EXISTS idx_task_runs_complexity ON task_runs(complexity, model_used, success);
