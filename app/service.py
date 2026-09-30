import hashlib
import json
import mimetypes
import sqlite3
import time
import uuid

from app.ai import ModelError
from app.mail import SendFailed, SendUncertain
from app.schemas import DraftInput, LeadInput, claims_attachment
from app.website import WebsiteError, WebsiteReader

MAX_ATTACHMENT_SIZE = 5 * 1024 * 1024
MAX_ATTACHMENT_TOTAL = 10 * 1024 * 1024
MAX_ATTACHMENT_COUNT = 5


class RuleError(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status


class Service:
    def __init__(self, store, settings, agent, mailer, clock=time.time, website_reader=None):
        self.store = store
        self.settings = settings
        self.agent = agent
        self.mailer = mailer
        self.clock = clock
        self.website_reader = website_reader or WebsiteReader()

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
        lead = self.lead(lead_id)
        self._require_sender()
        with self.store.connect(transaction=True) as db:
            research = db.execute(
                "SELECT status,summary FROM website_research WHERE lead_id=?", (lead_id,)
            ).fetchone()
            if research and research["status"] in {"pending", "reading"}:
                raise RuleError("官网正在读取，请等待背景提取完成后再发送首封。", 409)
            if not lead["background"] and not (
                research and research["status"] == "completed" and research["summary"]
            ):
                raise RuleError("请先读取官网提取背景，或在录入时填写公司背景，再启动开发。", 422)
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

    def start_website(self, lead_id, operation_id):
        self.lead(lead_id)
        issues = [issue for issue in self.settings.issues() if issue.startswith("DASHSCOPE")]
        if issues:
            raise RuleError("请先配置百炼模型：" + "、".join(issues), 503)
        with self.store.connect(transaction=True) as db:
            existing = self._operation(db, operation_id, lead_id, "website")
            if existing:
                return {"scheduled": False}
            if db.execute("SELECT id FROM email_tasks WHERE lead_id=?", (lead_id,)).fetchone():
                raise RuleError("请在创建首封任务前读取官网；已有邮件使用原背景，历史记录不会改写。", 409)
            lead = db.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()
            if lead["status"] != "new":
                raise RuleError("只有待开发客户可读取官网。", 409)
            current = db.execute("SELECT status FROM website_research WHERE lead_id=?", (lead_id,)).fetchone()
            if current and current["status"] in {"pending", "reading"}:
                return {"scheduled": False}
            db.execute(
                "INSERT INTO website_research(lead_id,status,operation_id,updated_at) VALUES (?,'pending',?,?) "
                "ON CONFLICT(lead_id) DO UPDATE SET status='pending',operation_id=excluded.operation_id,pages='[]',warnings='[]',summary='',facts='[]',usage='{}',error='',updated_at=excluded.updated_at",
                (lead_id, operation_id, self.clock()),
            )
        return {"scheduled": True}

    def save_background(self, lead_id, data):
        self.lead(lead_id)
        with self.store.connect(transaction=True) as db:
            existing = self._operation(db, data.operation_id, lead_id, "background", data.background)
            if not existing:
                if (
                    db.execute("SELECT id FROM email_tasks WHERE lead_id=?", (lead_id,)).fetchone()
                    or db.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()[0] != "new"
                ):
                    raise RuleError("只能在创建首封任务前修改公司背景。", 409)
                db.execute(
                    "UPDATE leads SET background=?,updated_at=? WHERE id=?",
                    (data.background, self.clock(), lead_id),
                )
        return self.lead(lead_id)

    def process_website(self, lead_id):
        with self.store.connect(transaction=True) as db:
            claimed = db.execute(
                "UPDATE website_research SET status='reading' WHERE lead_id=? AND status='pending'",
                (lead_id,),
            )
            if not claimed.rowcount:
                return
            operation = db.execute(
                "SELECT operation_id FROM website_research WHERE lead_id=?", (lead_id,)
            ).fetchone()[0]
        lead = self.lead(lead_id)
        try:
            result = self.website_reader.read(lead["website"])
            with self.store.connect(transaction=True) as db:
                db.execute(
                    "UPDATE website_research SET pages=?,warnings=? WHERE lead_id=? AND operation_id=?",
                    (self.dumps(result["pages"]), self.dumps(result["warnings"]), lead_id, operation),
                )
            output, usage = self.agent.run("website", {"company": lead["company"], "pages": result["pages"]})
            with self.store.connect(transaction=True) as db:
                status = db.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()[0]
                db.execute(
                    "UPDATE website_research SET status=?,summary=?,facts=?,usage=?,error=?,updated_at=? WHERE lead_id=? AND operation_id=?",
                    (
                        "completed" if status == "new" else "cancelled",
                        output["summary"] if status == "new" else "",
                        self.dumps(output["facts"]),
                        self.dumps(usage),
                        "" if status == "new" else "客户已停止，官网结果不用于发信。",
                        self.clock(),
                        lead_id,
                        operation,
                    ),
                )
        except (WebsiteError, ModelError) as exc:
            with self.store.connect(transaction=True) as db:
                db.execute(
                    "UPDATE website_research SET status='failed',error=?,updated_at=? WHERE lead_id=? AND operation_id=?",
                    (self.settings.redact(str(exc)), self.clock(), lead_id, operation),
                )

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
            if current["status"] != "failed" or not self._task_allowed(db, task):
                raise RuleError("只有确认未发送的失败任务可重试；已回复、已停止或结果不确定时禁止重发。", 409)
            db.execute(
                "UPDATE email_tasks SET status='pending',due_at=?,error='' WHERE id=?",
                (self.clock(), task_id),
            )
            db.execute("UPDATE operations SET result_id=? WHERE operation_id=?", (task_id, operation_id))
        return {"task_id": task_id, "scheduled": True}

    @staticmethod
    def _task_allowed(db, task):
        lead = db.execute("SELECT status FROM leads WHERE id=?", (task["lead_id"],)).fetchone()
        expected = {"first": "new", "followup": "awaiting_reply", "reply": "replied"}[task["kind"]]
        if lead["status"] != expected:
            return False
        if task["kind"] == "reply":
            latest = db.execute(
                "SELECT latest_reply_id FROM conversations WHERE lead_id=?", (task["lead_id"],)
            ).fetchone()
            if latest["latest_reply_id"] != task["reply_id"]:
                return False
        return True

    def _writable_draft(self, db, lead_id, reply_id):
        lead = db.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()
        conversation = db.execute("SELECT * FROM conversations WHERE lead_id=?", (lead_id,)).fetchone()
        reply = db.execute(
            "SELECT status FROM messages WHERE id=? AND lead_id=? AND direction='inbound'",
            (reply_id, lead_id),
        ).fetchone()
        if not lead or not conversation:
            raise RuleError("客户不存在。", 404)
        if (
            lead["status"] != "replied"
            or conversation["latest_reply_id"] != reply_id
            or not reply
            or reply["status"] != "completed"
        ):
            raise RuleError("只能处理最近一次已分析的回复；会话已停止、已发送或回复已更新时不能发送。", 409)
        if not conversation["draft"]:
            raise RuleError("本次没有需要发送的回复草稿，可以结束会话。", 409)
        if db.execute(
            "SELECT id FROM email_tasks WHERE lead_id=? AND status='uncertain'", (lead_id,)
        ).fetchone():
            raise RuleError("会话存在待核实的发送结果，请先核实收件箱。", 409)
        if db.execute(
            "SELECT id FROM email_tasks WHERE lead_id=? AND status IN ('pending','processing','sending')",
            (lead_id,),
        ).fetchone():
            raise RuleError("上一封邮件仍在处理，请等待发送结果后再处理新草稿。", 409)
        if db.execute("SELECT id FROM email_tasks WHERE reply_id=?", (reply_id,)).fetchone():
            raise RuleError("回复发送任务已创建，内容和附件已固定；明确失败时请重试原任务。", 409)

    def save_draft(self, lead_id, data: DraftInput):
        self.lead(lead_id)
        with self.store.connect(transaction=True) as db:
            existing = self._operation(
                db,
                data.operation_id,
                lead_id,
                "save_draft",
                self.dumps(data.model_dump(exclude={"operation_id"})),
            )
            if not existing:
                self._writable_draft(db, lead_id, data.reply_id)
                db.execute(
                    "UPDATE conversations SET draft=?,draft_subject=? WHERE lead_id=?",
                    (data.body, data.subject, lead_id),
                )
        return self.lead(lead_id)

    def upload_attachment(self, lead_id, reply_id, operation_id, filename, content):
        self.lead(lead_id)
        filename = (filename or "attachment").replace("\\", "/").split("/")[-1]
        if not filename or len(filename) > 160 or any(ord(c) < 32 or ord(c) == 127 for c in filename):
            raise RuleError("附件文件名无效，最长 160 字符且不能包含控制字符。", 422)
        if not content or len(content) > MAX_ATTACHMENT_SIZE:
            raise RuleError("附件不能为空，单个文件最大 5MB。", 413)
        digest = hashlib.sha256(content).hexdigest()
        with self.store.connect(transaction=True) as db:
            existing = self._operation(
                db, operation_id, lead_id, "upload_attachment", self.dumps([reply_id, filename, digest])
            )
            if existing:
                return {"attachment_id": existing["result_id"]}
            self._writable_draft(db, lead_id, reply_id)
            duplicate = db.execute(
                "SELECT id FROM attachments WHERE reply_id=? AND filename=? AND digest=?",
                (reply_id, filename, digest),
            ).fetchone()
            if duplicate:
                attachment_id = duplicate["id"]
            else:
                totals = db.execute(
                    "SELECT COUNT(*) AS n,COALESCE(SUM(size),0) AS size FROM attachments WHERE reply_id=?",
                    (reply_id,),
                ).fetchone()
                if (
                    totals["n"] >= MAX_ATTACHMENT_COUNT
                    or totals["size"] + len(content) > MAX_ATTACHMENT_TOTAL
                ):
                    raise RuleError("每封邮件最多 5 个附件，总大小不能超过 10MB。", 413)
                content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                if content_type.split("/")[0] not in {"application", "text", "image", "audio", "video"}:
                    content_type = "application/octet-stream"
                cursor = db.execute(
                    "INSERT INTO attachments(lead_id,reply_id,filename,content_type,size,digest,content,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (lead_id, reply_id, filename, content_type, len(content), digest, content, self.clock()),
                )
                attachment_id = cursor.lastrowid
            db.execute(
                "UPDATE operations SET result_id=? WHERE operation_id=?", (attachment_id, operation_id)
            )
        return {"attachment_id": attachment_id}

    def delete_attachment(self, lead_id, attachment_id):
        with self.store.connect(transaction=True) as db:
            attachment = db.execute(
                "SELECT * FROM attachments WHERE id=? AND lead_id=?", (attachment_id, lead_id)
            ).fetchone()
            if not attachment:
                raise RuleError("附件不存在。", 404)
            self._writable_draft(db, lead_id, attachment["reply_id"])
            db.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
        return {"deleted": True}

    def send_draft(self, lead_id, data: DraftInput):
        self.lead(lead_id)
        self._require_sender()
        payload = self.dumps(data.model_dump(exclude={"operation_id"}))
        with self.store.connect(transaction=True) as db:
            existing = self._operation(db, data.operation_id, lead_id, "send_draft", payload)
            if existing:
                return {"task_id": existing["result_id"], "scheduled": False}
            previous = db.execute(
                "SELECT id,subject,body FROM email_tasks WHERE lead_id=? AND reply_id=?",
                (lead_id, data.reply_id),
            ).fetchone()
            if previous:
                if (previous["subject"], previous["body"]) != (data.subject, data.body):
                    raise RuleError("本条回复已有发送任务，不能改动内容或另建任务。", 409)
                task_id, scheduled = previous["id"], False
            else:
                self._writable_draft(db, lead_id, data.reply_id)
                count = db.execute(
                    "SELECT COUNT(*) FROM attachments WHERE reply_id=?", (data.reply_id,)
                ).fetchone()[0]
                if not count and claims_attachment(data.body):
                    raise RuleError("正文声称已附资料，但没有上传附件。请先上传文件，或修改正文。", 422)
                db.execute(
                    "UPDATE conversations SET draft=?,draft_subject=? WHERE lead_id=?",
                    (data.body, data.subject, lead_id),
                )
                cursor = db.execute(
                    "INSERT INTO email_tasks(lead_id,kind,subject,body,due_at,message_id,reply_id) VALUES (?,'reply',?,?,?,?,?)",
                    (
                        lead_id,
                        data.subject,
                        data.body,
                        self.clock(),
                        f"<{uuid.uuid4().hex}@packpilot.demo>",
                        data.reply_id,
                    ),
                )
                task_id, scheduled = cursor.lastrowid, True
                db.execute("UPDATE attachments SET task_id=? WHERE reply_id=?", (task_id, data.reply_id))
            db.execute("UPDATE operations SET result_id=? WHERE operation_id=?", (task_id, data.operation_id))
        return {"task_id": task_id, "scheduled": scheduled}

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
                "UPDATE conversations SET latest_reply_id=?,summary='',reason='',suggestion='',draft='',draft_subject='' WHERE lead_id=?",
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
                {
                    "direction": m["direction"],
                    "body": m["body"][:2000],
                    "attachments": [
                        a["filename"]
                        for a in lead["attachments"]
                        if a["task_id"] == m["task_id"] and m["task_id"] is not None
                    ],
                }
                for m in lead["messages"]
                if m["id"] != reply_id
            ][-6:],
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
        context = {key: lead[key] for key in keys}
        research = lead.get("website_research")
        if research and research["status"] == "completed":
            context["website_research"] = {"summary": research["summary"], "facts": research["facts"]}
        return context

    def process_task(self, task_id):
        with self.store.connect(transaction=True) as db:
            row = db.execute("SELECT * FROM email_tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["status"] != "pending" or row["due_at"] > self.clock():
                return
            task = dict(row)
            if not self._task_allowed(db, task):
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
            if row["status"] != "processing" or not self._task_allowed(db, task):
                db.execute(
                    "UPDATE email_tasks SET status='cancelled' WHERE id=? AND status='processing'", (task_id,)
                )
                return
            db.execute("UPDATE email_tasks SET status='sending' WHERE id=?", (task_id,))
        task = self.store.one("SELECT * FROM email_tasks WHERE id=?", (task_id,))
        task["attachments"] = self.store.all(
            "SELECT filename,content_type,content FROM attachments WHERE task_id=? ORDER BY id", (task_id,)
        )
        first = (
            self.store.one("SELECT * FROM email_tasks WHERE lead_id=? AND kind='first'", (lead["id"],))
            if task["kind"] in {"followup", "reply"}
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
            if not self._task_allowed(db, task):
                return
            if task["kind"] == "first":
                db.execute(
                    "UPDATE leads SET status='awaiting_reply',updated_at=? WHERE id=?", (now, lead["id"])
                )
                db.execute(
                    "INSERT OR IGNORE INTO email_tasks(lead_id,kind,due_at,message_id) VALUES (?,'followup',?,?)",
                    (lead["id"], now + self.settings.followup_delay, f"<{uuid.uuid4().hex}@packpilot.demo>"),
                )
            elif task["kind"] == "followup":
                db.execute(
                    "UPDATE leads SET status='followup_complete',updated_at=? WHERE id=?", (now, lead["id"])
                )
            else:
                db.execute(
                    "UPDATE leads SET status='awaiting_customer',updated_at=? WHERE id=?", (now, lead["id"])
                )
                db.execute("UPDATE conversations SET draft='' WHERE lead_id=?", (lead["id"],))

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
