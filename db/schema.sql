-- Agent Mesh — DB Schema (v0.6.5)
-- SQLite database stored at .agent-mesh/context.db

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
    -- v0.6.0 fields
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
