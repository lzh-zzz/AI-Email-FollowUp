import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, company TEXT NOT NULL,
                    title TEXT NOT NULL, website TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
                    industry TEXT NOT NULL, country TEXT NOT NULL, background TEXT NOT NULL,
                    source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
                    profile TEXT NOT NULL DEFAULT '', angle TEXT NOT NULL DEFAULT '',
                    evidence TEXT NOT NULL DEFAULT '[]', assumptions TEXT NOT NULL DEFAULT '[]',
                    intent TEXT, stop_reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    lead_id INTEGER PRIMARY KEY REFERENCES leads(id),
                    summary TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
                    suggestion TEXT NOT NULL DEFAULT '', draft TEXT NOT NULL DEFAULT '',
                    latest_reply_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS email_tasks (
                    id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id),
                    kind TEXT NOT NULL CHECK(kind IN ('first','followup')),
                    status TEXT NOT NULL DEFAULT 'pending', subject TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL DEFAULT '', due_at REAL NOT NULL,
                    sent_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', error_phase TEXT NOT NULL DEFAULT '',
                    message_id TEXT UNIQUE NOT NULL, usage TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(lead_id,kind)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id),
                    direction TEXT NOT NULL, source TEXT NOT NULL, body TEXT NOT NULL,
                    created_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'completed',
                    error TEXT NOT NULL DEFAULT '', analysis TEXT NOT NULL DEFAULT '{}',
                    usage TEXT NOT NULL DEFAULT '{}', task_id INTEGER UNIQUE REFERENCES email_tasks(id)
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id),
                    kind TEXT NOT NULL, fingerprint TEXT NOT NULL, result_id INTEGER
                );
            """)

    @contextmanager
    def connect(self, transaction=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if transaction:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def one(self, sql, params=()):
        with self.connect() as db:
            row = db.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql, params=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, params).fetchall()]

    def detail(self, lead_id):
        lead = self.one("SELECT * FROM leads WHERE id=?", (lead_id,))
        if not lead:
            return None
        for field in ["evidence", "assumptions"]:
            lead[field] = json.loads(lead[field])
        lead["conversation"] = self.one("SELECT * FROM conversations WHERE lead_id=?", (lead_id,))
        lead["tasks"] = self.all("SELECT * FROM email_tasks WHERE lead_id=? ORDER BY id", (lead_id,))
        lead["messages"] = self.all("SELECT * FROM messages WHERE lead_id=? ORDER BY id", (lead_id,))
        for task in lead["tasks"]:
            task["usage"] = json.loads(task["usage"])
        for message in lead["messages"]:
            for field in ["analysis", "usage"]:
                message[field] = json.loads(message[field])
        return lead

    def recover(self):
        with self.connect(transaction=True) as db:
            db.execute(
                "UPDATE email_tasks SET status='uncertain', error='上次运行在 SMTP 提交期间中断，请核实收件箱；不会自动重发。' WHERE status='sending'"
            )
            db.execute("UPDATE email_tasks SET status='pending' WHERE status='processing'")
            db.execute(
                "UPDATE messages SET status='failed',error='上次运行中断；跟进仍已取消，可重试回复分析。' WHERE status='analyzing'"
            )
