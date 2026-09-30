import csv
import io
from contextlib import asynccontextmanager
from urllib.parse import quote

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, FastAPI, File, Form, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app.ai import BailianClient, EmailAgent
from app.config import ROOT, Settings
from app.mail import QQMailer
from app.schemas import BackgroundInput, DraftInput, LeadInput, Operation, ReplyInput
from app.service import MAX_ATTACHMENT_SIZE, RuleError, Service
from app.store import Store

SAMPLES = [
    {
        "name": "Emma Wilson",
        "company": "Northline Beauty",
        "title": "Head of Procurement",
        "website": "https://northline-beauty.example.com",
        "industry": "美容护肤",
        "country": "United Kingdom",
        "background": "虚构公司，销售敏感肌护肤产品，准备为旅行套装采购小批量包装，希望控制试销阶段的采购量并评估材料。",
    },
    {
        "name": "Lucas Martin",
        "company": "Fieldwork Coffee",
        "title": "Brand Director",
        "website": "https://fieldwork-coffee.example.com",
        "industry": "精品咖啡",
        "country": "France",
        "background": "虚构公司，经营精品咖啡线上品牌，规划季节性礼盒，关注包装品牌呈现和材料选择，需要在批量采购前查看样品。",
    },
    {
        "name": "Sofia Chen",
        "company": "Waypoint Outdoor",
        "title": "Operations Manager",
        "website": "https://waypoint-outdoor.example.com",
        "industry": "户外用品",
        "country": "Canada",
        "background": "虚构公司，销售户外旅行配件，产品尺寸多、库存波动较大，希望探索分批采购和适配不同 SKU 的可回收包装。",
    },
]


def create_app(
    settings=None, agent=None, mailer=None, clock=None, scheduler_enabled=True, website_reader=None
):
    settings = settings or Settings.load()
    store = Store(settings.db_path)
    model_client = None if agent else BailianClient(settings)
    agent = agent or EmailAgent(model_client)
    mailer = mailer or QQMailer(settings)
    service = Service(
        store, settings, agent, mailer, website_reader=website_reader, **({"clock": clock} if clock else {})
    )
    scheduler = BackgroundScheduler(timezone="UTC")

    @asynccontextmanager
    async def lifespan(app):
        store.recover()
        if scheduler_enabled:
            scheduler.add_job(service.process_due, "interval", seconds=2, max_instances=1, coalesce=True)
            scheduler.start()
        yield
        if scheduler_enabled:
            scheduler.shutdown(wait=True)
        if model_client:
            model_client.close()

    app = FastAPI(title="PackPilot 邮件开发工作台", lifespan=lifespan)
    app.state.service = service
    app.state.store = store
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.exception_handler(RuleError)
    async def rule_error(request, exc):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        details = [".".join(str(x) for x in err["loc"][1:]) + ": " + err["msg"] for err in exc.errors()]
        return JSONResponse({"error": "资料格式不正确：" + "；".join(details)}, status_code=422)

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/api/config")
    def config():
        return service.settings.public()

    @app.post("/api/config/reload")
    def reload_config():
        busy = store.one("SELECT COUNT(*) AS n FROM email_tasks WHERE status IN ('processing','sending')")[
            "n"
        ]
        busy += store.one("SELECT COUNT(*) AS n FROM messages WHERE status='analyzing'")["n"]
        busy += store.one("SELECT COUNT(*) AS n FROM website_research WHERE status IN ('pending','reading')")[
            "n"
        ]
        if busy:
            raise RuleError("任务正在执行，请完成后再重新加载配置。", 409)
        try:
            updated = Settings.load()
        except ValueError:
            raise RuleError("配置中的端口或等待秒数必须是整数，请检查 .env。", 422) from None
        updated.db_path = service.settings.db_path
        service.settings = updated
        if model_client:
            model_client.settings = updated
        if isinstance(mailer, QQMailer):
            mailer.settings = updated
        return updated.public()

    @app.get("/api/samples")
    def samples():
        return {"samples": SAMPLES}

    @app.get("/api/leads")
    def leads():
        return {
            "leads": store.all(
                "SELECT id,name,company,title,email,industry,country,status,intent,created_at FROM leads ORDER BY id DESC"
            )
        }

    @app.post("/api/leads", status_code=201)
    def create_lead(data: LeadInput):
        return service.create_lead(data)

    @app.post("/api/leads/import")
    async def import_csv(file: UploadFile = File(...)):
        content = await file.read(256001)
        if len(content) > 256000:
            raise RuleError("CSV 最大 250KB。", 413)
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise RuleError("请将 CSV 保存为 UTF-8 编码。", 422) from None
        aliases = {
            "姓名": "name",
            "公司": "company",
            "职位": "title",
            "官网": "website",
            "邮箱": "email",
            "行业": "industry",
            "国家/地区": "country",
            "国家": "country",
            "公司背景": "background",
            "公司简介": "background",
            "来源": "source",
        }
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise RuleError("CSV 必须包含表头。", 422)
        results = []
        for index, row in enumerate(reader, start=2):
            if index > 101:
                results.append(
                    {"row": index, "status": "failed", "reason": "一次最多导入 100 行，其余未处理。"}
                )
                break
            values = {
                aliases.get(str(key).strip(), str(key).strip()): value for key, value in row.items() if key
            }
            values.setdefault("source", "CSV 导入")
            try:
                lead = service.create_lead(LeadInput.model_validate(values))
                results.append({"row": index, "status": "created", "lead_id": lead["id"]})
            except ValidationError as exc:
                results.append(
                    {
                        "row": index,
                        "status": "failed",
                        "reason": "；".join(str(e["loc"][0]) + ": " + e["msg"] for e in exc.errors()),
                    }
                )
            except RuleError as exc:
                results.append({"row": index, "status": "skipped", "reason": exc.message})
        return {"results": results}

    @app.get("/api/leads/{lead_id}")
    def detail(lead_id: int):
        return service.lead(lead_id)

    @app.post("/api/leads/{lead_id}/start", status_code=202)
    def start(lead_id: int, operation: Operation, background: BackgroundTasks):
        result = service.start(lead_id, operation.operation_id)
        if result["scheduled"]:
            background.add_task(service.process_task, result["task_id"])
        return result

    @app.post("/api/leads/{lead_id}/stop")
    def stop(lead_id: int, operation: Operation):
        return service.stop(lead_id, operation.operation_id)

    @app.post("/api/leads/{lead_id}/reply", status_code=202)
    def reply(lead_id: int, data: ReplyInput, background: BackgroundTasks):
        result = service.record_reply(lead_id, data.operation_id, data.text)
        if result["scheduled"]:
            background.add_task(service.analyze_reply, result["reply_id"])
        return result

    @app.post("/api/tasks/{task_id}/retry", status_code=202)
    def retry_task(task_id: int, operation: Operation, background: BackgroundTasks):
        result = service.retry_task(task_id, operation.operation_id)
        if result["scheduled"]:
            background.add_task(service.process_task, result["task_id"])
        return result

    @app.post("/api/leads/{lead_id}/draft")
    def save_draft(lead_id: int, data: DraftInput):
        return service.save_draft(lead_id, data)

    @app.post("/api/leads/{lead_id}/website", status_code=202)
    def read_website(lead_id: int, operation: Operation, background: BackgroundTasks):
        result = service.start_website(lead_id, operation.operation_id)
        if result["scheduled"]:
            background.add_task(service.process_website, lead_id)
        return result

    @app.post("/api/leads/{lead_id}/background")
    def save_background(lead_id: int, data: BackgroundInput):
        return service.save_background(lead_id, data)

    @app.post("/api/leads/{lead_id}/send-draft", status_code=202)
    def send_draft(lead_id: int, data: DraftInput, background: BackgroundTasks):
        result = service.send_draft(lead_id, data)
        if result["scheduled"]:
            background.add_task(service.process_task, result["task_id"])
        return result

    @app.post("/api/leads/{lead_id}/attachments", status_code=201)
    async def upload_attachment(
        lead_id: int,
        reply_id: int = Form(gt=0),
        operation_id: str = Form(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_-]+$"),
        file: UploadFile = File(...),
    ):
        content = await file.read(MAX_ATTACHMENT_SIZE + 1)
        return service.upload_attachment(lead_id, reply_id, operation_id, file.filename, content)

    @app.delete("/api/leads/{lead_id}/attachments/{attachment_id}")
    def delete_attachment(lead_id: int, attachment_id: int):
        return service.delete_attachment(lead_id, attachment_id)

    @app.get("/api/attachments/{attachment_id}")
    def download_attachment(attachment_id: int):
        attachment = store.one("SELECT filename,content FROM attachments WHERE id=?", (attachment_id,))
        if not attachment:
            raise RuleError("附件不存在。", 404)
        return Response(
            attachment["content"],
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(attachment["filename"]),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/replies/{reply_id}/retry", status_code=202)
    def retry_reply(reply_id: int, operation: Operation, background: BackgroundTasks):
        result = service.retry_reply(reply_id, operation.operation_id)
        if result["scheduled"]:
            background.add_task(service.analyze_reply, result["reply_id"])
        return result

    @app.get("/api/health")
    def health():
        return {"status": "ok", "configuration_ready": not service.settings.issues()}

    return app
