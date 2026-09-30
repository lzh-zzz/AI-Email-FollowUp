import hashlib
import json
import sqlite3
import time
import uuid

from app.ai import ModelError
from app.mail import SendFailed, SendUncertain
from app.schemas import LeadInput


class RuleError(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status


class Service:
    def __init__(self, store, settings, agent, mailer, clock=time.time):
        self.store = store
        self.settings = settings
        self.agent = agent
        self.mailer = mailer
        self.clock = clock

    @staticmethod
    def dumps(value):
        return json.dumps(value, ensure_ascii=False)

    def create_lead(self, data: LeadInput):
        values = data.model_dump()
        now = self.clock()
        try:
            with self.store.connect(transaction=True) as db:
                names = list(values)
                cursor = db.execute(
                    f"INSERT INTO leads ({','.join(names)},created_at,updated_at) VALUES ({','.join('?' for _ in names)},?,?)",
                    [*values.values(), now, now],
                )
                lead_id = cursor.lastrowid
                db.execute("INSERT INTO conversations(lead_id) VALUES (?)", (lead_id,))
        except sqlite3.IntegrityError:
            raise RuleError("该邮箱已存在，重复导入已跳过。", 409) from None
        return self.store.detail(lead_id)

    def lead(self, lead_id):
        lead = self.store.detail(lead_id)
        if not lead:
            raise RuleError("客户不存在。", 404)
        return lead

    def _operation(self, db, operation_id, lead_id, kind, payload=""):
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        existing = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if existing:
            if (existing["lead_id"], existing["kind"], existing["fingerprint"]) != (
                lead_id,
                kind,
                fingerprint,
            ):
                raise RuleError("操作标识已用于其他请求，请重新执行操作。", 409)
            return dict(existing)
        db.execute("INSERT INTO operations VALUES (?,?,?,?,NULL)", (operation_id, lead_id, kind, fingerprint))
        return None

    def _require_sender(self):
        if self.settings.issues():
            raise RuleError("配置尚未就绪，请填写：" + "、".join(self.settings.issues()), 503)

    def start(self, lead_id, operation_id):
        self.lead(lead_id)
        self._require_sender()
        with self.store.connect(transaction=True) as db:
            existing = self._operation(db, operation_id, lead_id, "start")
            if existing:
                return {"task_id": existing["result_id"], "scheduled": False}
            current = db.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()
            if current["status"] == "stopped":
                raise RuleError("客户已停止，不能重新启动开发。", 409)
            task = db.execute(
                "SELECT id FROM email_tasks WHERE lead_id=? AND kind='first'", (lead_id,)
            ).fetchone()
            if task:
                task_id = task["id"]
                scheduled = False
            else:
                cursor = db.execute(
                    "INSERT INTO email_tasks(lead_id,kind,due_at,message_id) VALUES (?,'first',?,?)",
                    (lead_id, self.clock(), f"<{uuid.uuid4().hex}@packpilot.demo>"),
                )
                task_id, scheduled = cursor.lastrowid, True
            db.execute("UPDATE operations SET result_id=? WHERE operation_id=?", (task_id, operation_id))
        return {"task_id": task_id, "scheduled": scheduled}

    def retry_task(self, task_id, operation_id):
        task = self.store.one("SELECT * FROM email_tasks WHERE id=?", (task_id,))
        if not task:
            raise RuleError("发送任务不存在。", 404)
        lead = self.lead(task["lead_id"])
        self._require_sender()
        with self.store.connect(transaction=True) as db:
            existing = self._operation(db, operation_id, lead["id"], "retry_task", str(task_id))
            if existing:
                return {"task_id": task_id, "scheduled": False}
            current = db.execute("SELECT status FROM email_tasks WHERE id=?", (task_id,)).fetchone()
            status = db.execute("SELECT status FROM leads WHERE id=?", (lead["id"],)).fetchone()["status"]
            allowed = "new" if task["kind"] == "first" else "awaiting_reply"
            if current["status"] != "failed" or status != allowed:
                raise RuleError("只有确认未发送的失败任务可重试；已回复、已停止或结果不确定时禁止重发。", 409)
            db.execute(
                "UPDATE email_tasks SET status='pending',due_at=?,error='' WHERE id=?",
                (self.clock(), task_id),
            )
            db.execute("UPDATE operations SET result_id=? WHERE operation_id=?", (task_id, operation_id))
        return {"task_id": task_id, "scheduled": True}

    def stop(self, lead_id, operation_id):
        self.lead(lead_id)
        with self.store.connect(transaction=True) as db:
            existing = self._operation(db, operation_id, lead_id, "stop")
            if not existing:
                db.execute(
                    "UPDATE leads SET status='stopped',stop_reason='人工停止',updated_at=? WHERE id=?",
                    (self.clock(), lead_id),
                )
                db.execute("UPDATE conversations SET draft='' WHERE lead_id=?", (lead_id,))
                self._cancel(db, lead_id)
        return self.lead(lead_id)

    @staticmethod
    def _cancel(db, lead_id):
        db.execute(
            "UPDATE email_tasks SET status='cancelled' WHERE lead_id=? AND status IN ('pending','processing','failed')",
            (lead_id,),
        )

    def record_reply(self, lead_id, operation_id, text):
        self.lead(lead_id)
        with self.store.connect(transaction=True) as db:
            existing = self._operation(db, operation_id, lead_id, "reply", text)
            if existing:
                return {"reply_id": existing["result_id"], "scheduled": False}
            first = db.execute(
                "SELECT id FROM email_tasks WHERE lead_id=? AND kind='first' AND status='sent'", (lead_id,)
            ).fetchone()
            if not first:
                raise RuleError("请先成功发送首封，再录入模拟回复。", 409)
            cursor = db.execute(
                "INSERT INTO messages(lead_id,direction,source,body,created_at,status) VALUES (?,'inbound','manual_simulation',?,?,'analyzing')",
                (lead_id, text, self.clock()),
            )
            reply_id = cursor.lastrowid
            db.execute(
                "UPDATE leads SET status=CASE WHEN status='stopped' THEN status ELSE 'replied' END,updated_at=? WHERE id=?",
                (self.clock(), lead_id),
            )
            self._cancel(db, lead_id)
            db.execute(
                "UPDATE conversations SET latest_reply_id=?,summary='',reason='',suggestion='',draft='' WHERE lead_id=?",
                (reply_id, lead_id),
            )
            db.execute("UPDATE operations SET result_id=? WHERE operation_id=?", (reply_id, operation_id))
        return {"reply_id": reply_id, "scheduled": True}

    def retry_reply(self, reply_id, operation_id):
        reply = self.store.one("SELECT * FROM messages WHERE id=? AND direction='inbound'", (reply_id,))
        if not reply:
            raise RuleError("回复不存在。", 404)
        with self.store.connect(transaction=True) as db:
            existing = self._operation(db, operation_id, reply["lead_id"], "retry_reply", str(reply_id))
            if existing:
                return {"reply_id": reply_id, "scheduled": False}
            message = db.execute("SELECT status FROM messages WHERE id=?", (reply_id,)).fetchone()
            latest = db.execute(
                "SELECT latest_reply_id FROM conversations WHERE lead_id=?", (reply["lead_id"],)
            ).fetchone()
            if message["status"] != "failed" or latest["latest_reply_id"] != reply_id:
                raise RuleError("只能重试最近一条分析失败的回复。", 409)
            db.execute("UPDATE messages SET status='analyzing',error='' WHERE id=?", (reply_id,))
            db.execute("UPDATE operations SET result_id=? WHERE operation_id=?", (reply_id, operation_id))
        return {"reply_id": reply_id, "scheduled": True}

    def analyze_reply(self, reply_id):
        reply = self.store.one("SELECT * FROM messages WHERE id=?", (reply_id,))
        if not reply or reply["status"] != "analyzing":
            return
        lead = self.lead(reply["lead_id"])
        context = {
            "lead": self._lead_context(lead),
            "reply": reply["body"],
            "history": [
                {"subject": t["subject"], "body": t["body"]} for t in lead["tasks"] if t["status"] == "sent"
            ],
        }
        try:
            output, usage = self.agent.run("reply", context)
        except Exception as exc:
            error = str(exc) if isinstance(exc, ModelError) else "回复分析失败，请重试；未发送跟进已取消。"
            with self.store.connect() as db:
                db.execute(
                    "UPDATE messages SET status='failed',error=? WHERE id=?",
                    (self.settings.redact(error), reply_id),
                )
            return
        with self.store.connect(transaction=True) as db:
            db.execute(
                "UPDATE messages SET status='completed',analysis=?,usage=?,error='' WHERE id=?",
                (self.dumps(output), self.dumps(usage), reply_id),
            )
            latest = db.execute(
                "SELECT latest_reply_id FROM conversations WHERE lead_id=?", (lead["id"],)
            ).fetchone()
            if latest["latest_reply_id"] != reply_id:
                # A slower analysis of an earlier opt-out must still stop this lead.
                if output["stop"]:
                    db.execute(
                        "UPDATE leads SET status='stopped',stop_reason=CASE WHEN status='stopped' THEN stop_reason ELSE ? END,updated_at=? WHERE id=?",
                        (output["stop_reason"], self.clock(), lead["id"]),
                    )
                    db.execute("UPDATE conversations SET draft='' WHERE lead_id=?", (lead["id"],))
                    self._cancel(db, lead["id"])
                return
            current = db.execute("SELECT status,stop_reason FROM leads WHERE id=?", (lead["id"],)).fetchone()
            stopped = current["status"] == "stopped" or output["stop"]
            reason = current["stop_reason"] if current["status"] == "stopped" else output["stop_reason"]
            db.execute(
                "UPDATE leads SET intent=?,status=?,stop_reason=?,updated_at=? WHERE id=?",
                (output["intent"], "stopped" if stopped else "replied", reason, self.clock(), lead["id"]),
            )
            db.execute(
                "UPDATE conversations SET summary=?,reason=?,suggestion=?,draft=? WHERE lead_id=?",
                (
                    output["summary"],
                    output["reason"],
                    output["suggestion"],
                    "" if stopped else output["draft"],
                    lead["id"],
                ),
            )

    @staticmethod
    def _lead_context(lead):
        keys = [
            "name",
            "company",
            "title",
            "website",
            "industry",
            "country",
            "background",
            "profile",
            "angle",
        ]
        return {key: lead[key] for key in keys}

    def process_task(self, task_id):
        with self.store.connect(transaction=True) as db:
            row = db.execute("SELECT * FROM email_tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["status"] != "pending" or row["due_at"] > self.clock():
                return
            task = dict(row)
            lead_status = db.execute("SELECT status FROM leads WHERE id=?", (task["lead_id"],)).fetchone()[
                "status"
            ]
            expected = "new" if task["kind"] == "first" else "awaiting_reply"
            if lead_status != expected:
                db.execute("UPDATE email_tasks SET status='cancelled' WHERE id=?", (task_id,))
                return
            uncertain = db.execute(
                "SELECT id FROM email_tasks WHERE lead_id=? AND status='uncertain'", (task["lead_id"],)
            ).fetchone()
            if uncertain:
                return
            db.execute(
                "UPDATE email_tasks SET status='processing',attempts=attempts+1,error='',error_phase='' WHERE id=?",
                (task_id,),
            )
        lead = self.lead(task["lead_id"])
        try:
            self._require_sender()
            if not task["body"]:
                context = {"lead": self._lead_context(lead)}
                if task["kind"] == "followup":
                    first = next(t for t in lead["tasks"] if t["kind"] == "first")
                    context["first_email"] = {"subject": first["subject"], "body": first["body"]}
                output, usage = self.agent.run(task["kind"], context)
                with self.store.connect(transaction=True) as db:
                    db.execute(
                        "UPDATE email_tasks SET subject=?,body=?,usage=? WHERE id=?",
                        (output["subject"], output["body"], self.dumps(usage), task_id),
                    )
                    if task["kind"] == "first":
                        db.execute(
                            "UPDATE leads SET profile=?,angle=?,evidence=?,assumptions=?,updated_at=? WHERE id=?",
                            (
                                output["profile"],
                                output["angle"],
                                self.dumps(output["evidence"]),
                                self.dumps(output["assumptions"]),
                                self.clock(),
                                lead["id"],
                            ),
                        )
        except Exception as exc:
            message = str(exc) if isinstance(exc, (ModelError, RuleError)) else "AI 生成失败，请重试任务。"
            self._task_error(task_id, message, "ai")
            return
        with self.store.connect(transaction=True) as db:
            row = db.execute("SELECT status FROM email_tasks WHERE id=?", (task_id,)).fetchone()
            current = db.execute("SELECT status FROM leads WHERE id=?", (lead["id"],)).fetchone()["status"]
            if row["status"] != "processing" or current != expected:
                db.execute(
                    "UPDATE email_tasks SET status='cancelled' WHERE id=? AND status='processing'", (task_id,)
                )
                return
            db.execute("UPDATE email_tasks SET status='sending' WHERE id=?", (task_id,))
        task = self.store.one("SELECT * FROM email_tasks WHERE id=?", (task_id,))
        first = (
            self.store.one("SELECT * FROM email_tasks WHERE lead_id=? AND kind='first'", (lead["id"],))
            if task["kind"] == "followup"
            else None
        )
        try:
            self.mailer.send(lead, task, first)
        except SendFailed as exc:
            self._task_error(task_id, str(exc), "smtp")
            return
        except Exception as exc:
            message = (
                str(exc)
                if isinstance(exc, SendUncertain)
                else "SMTP 提交结果不确定，禁止重发，请核实收件箱。"
            )
            self._task_error(task_id, message, "smtp", uncertain=True)
            return
        now = self.clock()
        with self.store.connect(transaction=True) as db:
            db.execute("UPDATE email_tasks SET status='sent',sent_at=?,error='' WHERE id=?", (now, task_id))
            db.execute(
                "INSERT OR IGNORE INTO messages(lead_id,direction,source,body,created_at,task_id) VALUES (?,'outbound','qq_smtp',?,?,?)",
                (lead["id"], task["body"], now, task_id),
            )
            current = db.execute("SELECT status FROM leads WHERE id=?", (lead["id"],)).fetchone()["status"]
            if current != expected:
                return
            if task["kind"] == "first":
                db.execute(
                    "UPDATE leads SET status='awaiting_reply',updated_at=? WHERE id=?", (now, lead["id"])
                )
                db.execute(
                    "INSERT OR IGNORE INTO email_tasks(lead_id,kind,due_at,message_id) VALUES (?,'followup',?,?)",
                    (lead["id"], now + self.settings.followup_delay, f"<{uuid.uuid4().hex}@packpilot.demo>"),
                )
            else:
                db.execute(
                    "UPDATE leads SET status='followup_complete',updated_at=? WHERE id=?", (now, lead["id"])
                )

    def _task_error(self, task_id, message, phase, uncertain=False):
        with self.store.connect() as db:
            db.execute(
                "UPDATE email_tasks SET status=?,error=?,error_phase=? WHERE id=? AND status IN ('processing','sending')",
                ("uncertain" if uncertain else "failed", self.settings.redact(message), phase, task_id),
            )

    def process_due(self):
        for task in self.store.all(
            "SELECT id FROM email_tasks WHERE status='pending' AND due_at<=? ORDER BY due_at LIMIT 20",
            (self.clock(),),
        ):
            self.process_task(task["id"])
