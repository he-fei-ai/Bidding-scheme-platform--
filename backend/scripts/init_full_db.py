"""Initialize full database schema"""
import os, sqlite3

db_dir = os.path.join(os.path.dirname(__file__), 'data')
os.makedirs(db_dir, exist_ok=True)
db_path = os.path.join(db_dir, 'bid_assistant.sqlite3')

if os.path.exists(db_path):
    os.remove(db_path)
    print('Deleted existing database')

conn = sqlite3.connect(db_path)
conn.executescript('''
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        industry TEXT NOT NULL DEFAULT '',
        bidder_name TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        current_step INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
    
    CREATE TABLE IF NOT EXISTS uploaded_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        filename TEXT NOT NULL,
        path TEXT NOT NULL,
        file_type TEXT NOT NULL,
        file_role TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        extracted_text_path TEXT,
        message TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    );
    
    CREATE TABLE IF NOT EXISTS analysis (
        project_id INTEGER PRIMARY KEY,
        summary_json TEXT NOT NULL DEFAULT '{}',
        scoring_points_json TEXT NOT NULL DEFAULT '[]',
        confirmed_summary INTEGER NOT NULL DEFAULT 0,
        confirmed_scoring INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    );
    
    CREATE TABLE IF NOT EXISTS usage_limits (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        total_limit INTEGER NOT NULL DEFAULT 10,
        used_count INTEGER NOT NULL DEFAULT 0
    );
    
    CREATE TABLE IF NOT EXISTS project_usage (
        project_id INTEGER PRIMARY KEY,
        consumed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    
    CREATE TABLE IF NOT EXISTS sections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL DEFAULT 0,
        parent_id INTEGER DEFAULT NULL,
        title TEXT NOT NULL DEFAULT '',
        level INTEGER NOT NULL DEFAULT 1,
        sort_order INTEGER NOT NULL DEFAULT 0,
        section_type TEXT NOT NULL DEFAULT 'text',
        scoring_point_id TEXT DEFAULT NULL,
        ai_prompt_snapshot TEXT DEFAULT NULL,
        flowchart_json TEXT DEFAULT NULL,
        gantt_json TEXT DEFAULT NULL,
        illustration_type TEXT DEFAULT NULL,
        illustration_data TEXT DEFAULT NULL,
        locked INTEGER NOT NULL DEFAULT 0,
        content_mode TEXT DEFAULT NULL,
        content TEXT NOT NULL DEFAULT '',
        target_words INTEGER DEFAULT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
        FOREIGN KEY(parent_id) REFERENCES sections(id) ON DELETE CASCADE
    );
    
    CREATE TABLE IF NOT EXISTS exports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        filename TEXT NOT NULL,
        file_path TEXT NOT NULL,
        file_size INTEGER DEFAULT 0,
        status TEXT DEFAULT 'completed',
        config_json TEXT DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    );
    
    CREATE TABLE IF NOT EXISTS ai_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL DEFAULT 0,
        provider TEXT NOT NULL DEFAULT 'agnes',
        api_key TEXT DEFAULT NULL,
        base_url TEXT DEFAULT NULL,
        model_name TEXT NOT NULL DEFAULT 'agnes-2.0-flash',
        temperature REAL DEFAULT 0.7,
        max_tokens INTEGER DEFAULT 4096,
        timeout INTEGER DEFAULT 120,
        created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
    
    CREATE TABLE IF NOT EXISTS prompt_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL DEFAULT 0,
        template_key TEXT NOT NULL,
        template_text TEXT NOT NULL,
        version INTEGER DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    );
    
    CREATE TABLE IF NOT EXISTS knowledge_base (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        content TEXT NOT NULL DEFAULT '',
        source TEXT DEFAULT '',
        file_id INTEGER DEFAULT NULL,
        vector_id TEXT DEFAULT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    );
    
    CREATE TABLE IF NOT EXISTS generation_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        strategy TEXT DEFAULT 'v51',
        progress_json TEXT DEFAULT '{}',
        total_sections INTEGER DEFAULT 0,
        completed_sections INTEGER DEFAULT 0,
        error_message TEXT DEFAULT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
    );
    
    CREATE INDEX IF NOT EXISTS idx_sections_project ON sections(project_id);
    CREATE INDEX IF NOT EXISTS idx_sections_parent ON sections(parent_id);
    CREATE INDEX IF NOT EXISTS idx_exports_project ON exports(project_id);
    CREATE INDEX IF NOT EXISTS idx_uploaded_files_project ON uploaded_files(project_id);
    CREATE INDEX IF NOT EXISTS idx_knowledge_project ON knowledge_base(project_id);
    CREATE INDEX IF NOT EXISTS idx_generation_tasks_project ON generation_tasks(project_id);
    CREATE INDEX IF NOT EXISTS idx_ai_config_project ON ai_config(project_id);
    CREATE INDEX IF NOT EXISTS idx_prompt_templates_key ON prompt_templates(template_key);
''')
conn.commit()
print('Full database schema created successfully')

tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
print('Tables:', [t[0] for t in tables])
conn.close()
print('Done!')
