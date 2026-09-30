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
                    latest_reply_id INTEGER, draft_subject TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS email_tasks (
                    id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id),
                    kind TEXT NOT NULL CHECK(kind IN ('first','followup','reply')),
                    status TEXT NOT NULL DEFAULT 'pending', subject TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL DEFAULT '', due_at REAL NOT NULL,
                    sent_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', error_phase TEXT NOT NULL DEFAULT '',
                    message_id TEXT UNIQUE NOT NULL, usage TEXT NOT NULL DEFAULT '{}',
                    reply_id INTEGER UNIQUE REFERENCES messages(id)
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
                CREATE TABLE IF NOT EXISTS attachments (
                    id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id),
                    reply_id INTEGER NOT NULL REFERENCES messages(id),
                    task_id INTEGER REFERENCES email_tasks(id),
                    filename TEXT NOT NULL, content_type TEXT NOT NULL,
                    size INTEGER NOT NULL, digest TEXT NOT NULL, content BLOB NOT NULL,
                    created_at REAL NOT NULL, UNIQUE(reply_id,filename,digest)
                );
                CREATE TABLE IF NOT EXISTS website_research (
                    lead_id INTEGER PRIMARY KEY REFERENCES leads(id),
                    status TEXT NOT NULL DEFAULT 'pending', operation_id TEXT NOT NULL,
                    pages TEXT NOT NULL DEFAULT '[]', warnings TEXT NOT NULL DEFAULT '[]',
                    summary TEXT NOT NULL DEFAULT '', facts TEXT NOT NULL DEFAULT '[]',
                    usage TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
            """)
            self._migrate(db)

    @staticmethod
    def _migrate(db):
        if "reply_id" not in {row["name"] for row in db.execute("PRAGMA table_info(email_tasks)")}:
            # Rebuild without renaming the old table, keeping existing foreign-key targets intact.
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE email_tasks_upgrade (
                id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id),
                kind TEXT NOT NULL CHECK(kind IN ('first','followup','reply')),
                status TEXT NOT NULL DEFAULT 'pending', subject TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '', due_at REAL NOT NULL, sent_at REAL,
                attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                error_phase TEXT NOT NULL DEFAULT '', message_id TEXT UNIQUE NOT NULL,
                usage TEXT NOT NULL DEFAULT '{}', reply_id INTEGER UNIQUE REFERENCES messages(id)
            )""")
            columns = "id,lead_id,kind,status,subject,body,due_at,sent_at,attempts,error,error_phase,message_id,usage"
            db.execute(f"INSERT INTO email_tasks_upgrade({columns}) SELECT {columns} FROM email_tasks")
            db.execute("DROP TABLE email_tasks")
            db.execute("ALTER TABLE email_tasks_upgrade RENAME TO email_tasks")
            if list(db.execute("PRAGMA foreign_key_check")):
                raise RuntimeError("数据库迁移失败：关联记录不完整，原数据未提交变更。")
            db.commit()
            db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS one_initial_task ON email_tasks(lead_id,kind) WHERE kind IN ('first','followup')"
        )
        if "draft_subject" not in {row["name"] for row in db.execute("PRAGMA table_info(conversations)")}:
            db.execute("ALTER TABLE conversations ADD COLUMN draft_subject TEXT NOT NULL DEFAULT ''")

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
        lead["website_research"] = self.one("SELECT * FROM website_research WHERE lead_id=?", (lead_id,))
        if lead["website_research"]:
            for key in ["pages", "warnings", "facts", "usage"]:
                lead["website_research"][key] = json.loads(lead["website_research"][key])
        lead["tasks"] = self.all("SELECT * FROM email_tasks WHERE lead_id=? ORDER BY id", (lead_id,))
        lead["messages"] = self.all("SELECT * FROM messages WHERE lead_id=? ORDER BY id", (lead_id,))
        lead["attachments"] = self.all(
            "SELECT id,reply_id,task_id,filename,content_type,size,created_at FROM attachments WHERE lead_id=? ORDER BY id",
            (lead_id,),
        )
        if not lead["conversation"]["draft_subject"] and lead["tasks"]:
            subject = next(
                (task["subject"] for task in reversed(lead["tasks"]) if task["subject"]), "PackPilot"
            )
            lead["conversation"]["draft_subject"] = (
                subject if subject.lower().startswith("re:") else "Re: " + subject[:156]
            )
        for task in lead["tasks"]:
            task["usage"] = json.loads(task["usage"])
        for message in lead["messages"]:
            for field in ["analysis", "usage"]:
                message[field] = json.loads(message[field])
        return lead

    def recover(self):
        with self.connect(transaction=True) as db:
            db.execute(
                "UPDATE website_research SET status='failed',error='上次官网读取中断，可重新读取；人工背景保留。' WHERE status IN ('pending','reading')"
            )
            db.execute(
                "UPDATE email_tasks SET status='uncertain', error='上次运行在 SMTP 提交期间中断，请核实收件箱；不会自动重发。' WHERE status='sending'"
            )
            db.execute("UPDATE email_tasks SET status='pending' WHERE status='processing'")
            db.execute(
                "UPDATE messages SET status='failed',error='上次运行中断；跟进仍已取消，可重试回复分析。' WHERE status='analyzing'"
            )
