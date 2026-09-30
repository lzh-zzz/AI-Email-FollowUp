import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ai import BailianClient, EmailAgent, ModelError
from app.config import Settings
from app.mail import SendFailed, SendUncertain
from app.main import create_app

LEAD = dict(
    name="Emma Wilson",
    company="Northline Beauty",
    title="Head of Procurement",
    website="https://northline.example.com",
    email="test@example.com",
    industry="美容护肤",
    country="UK",
    background="为敏感肌护肤套装采购小批量包装，关注材料选择与试销采购量。",
)


class FakeModel:
    def __init__(self):
        self.calls = []
        self.failure = False
        self.hook = None

    def generate(self, kind, context):
        self.calls.append(kind)
        if self.hook:
            self.hook(kind)
        if self.failure:
            raise ModelError("模型暂时不可用，请重试。")
        if kind == "first":
            result = dict(
                profile="采购负责人关注试销阶段采购量与材料选择。",
                angle="以小批量包装打样支持护肤套装试销。",
                evidence=[context["lead"]["background"]],
                assumptions=[],
                subject="Sample packaging for your skincare trial",
                body="Hi Emma,\nWe offer small-batch recyclable packaging and sample prototyping. Would reviewing samples for your skincare trial be useful?\nPackPilot",
            )
        elif kind == "followup":
            result = dict(
                subject="Re: Sample packaging for your skincare trial",
                body="Hi Emma,\nFollowing up on sample packaging for your skincare trial. We could explore a small prototype before a larger order. Would this be helpful?\nPackPilot",
            )
        else:
            text = context["reply"]
            stop = "unsubscribe" in text or "not interested" in text
            result = dict(
                intent="拒绝" if stop else "高",
                stop=stop,
                stop_reason="客户要求退订" if stop else "",
                reason="根据回复内容判断",
                summary="客户要求停止联系" if stop else "客户希望了解打样信息",
                suggestion="停止跟进" if stop else "提供打样资料",
                draft="" if stop else "Thanks for your interest. We can share sample options.",
            )
        return result, {
            "model": "fake",
            "calls": 1,
            "reported": True,
            "input_tokens": 100,
            "output_tokens": 50,
        }


class FakeMailer:
    def __init__(self):
        self.sent = []
        self.failure = None
        self.lock = threading.Lock()

    def send(self, lead, task, first=None):
        if self.failure:
            raise self.failure
        with self.lock:
            self.sent.append(task["message_id"])


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        api_key="test-key",
        base_url="https://model.example.com/v1",
        model="test-model",
        smtp_username="sender@example.com",
        smtp_password="test-password",
        recipients=("test@example.com",),
        db_path=tmp_path / "demo.db",
    )
    model, mailer, now = FakeModel(), FakeMailer(), [1_800_000_000.0]
    app = create_app(settings, EmailAgent(model), mailer, clock=lambda: now[0], scheduler_enabled=False)
    with TestClient(app) as client:
        yield client, app, model, mailer, now, settings


def add(client, **overrides):
    response = client.post("/api/leads", json={**LEAD, **overrides})
    assert response.status_code == 201
    return response.json()["id"]


def start(client, lead_id, operation="start-operation"):
    return client.post(f"/api/leads/{lead_id}/start", json={"operation_id": operation})


def reply(client, lead_id, text="Please share sample options", operation="reply-operation"):
    return client.post(f"/api/leads/{lead_id}/reply", json={"operation_id": operation, "text": text})


def detail(client, lead_id):
    return client.get(f"/api/leads/{lead_id}").json()


def test_first_followup_and_repeated_requests(env):
    client, app, model, mailer, now, _ = env
    lead_id = add(client)
    assert start(client, lead_id).status_code == 202
    result = detail(client, lead_id)
    assert result["status"] == "awaiting_reply"
    assert result["profile"] and result["tasks"][0]["usage"]["input_tokens"] == 100
    assert result["tasks"][1]["due_at"] == now[0] + 60
    assert start(client, lead_id).json()["scheduled"] is False
    assert start(client, lead_id, "different-operation").json()["scheduled"] is False
    now[0] += 59
    app.state.service.process_due()
    assert len(mailer.sent) == 1
    now[0] += 1
    app.state.service.process_due()
    app.state.service.process_due()
    assert len(mailer.sent) == len(set(mailer.sent)) == 2
    assert detail(client, lead_id)["status"] == "followup_complete"
    assert model.calls == ["first", "followup"]


def test_reply_cancels_followup_and_only_displays_draft(env):
    client, app, _, mailer, now, _ = env
    lead_id = add(client)
    start(client, lead_id)
    assert reply(client, lead_id).status_code == 202
    result = detail(client, lead_id)
    assert result["status"] == "replied" and result["intent"] == "高"
    assert result["tasks"][1]["status"] == "cancelled"
    assert result["conversation"]["draft"]
    assert result["messages"][-1]["source"] == "manual_simulation"
    assert reply(client, lead_id).json()["scheduled"] is False
    now[0] += 90
    app.state.service.process_due()
    assert len(mailer.sent) == 1
    assert len(detail(client, lead_id)["messages"]) == 2


@pytest.mark.parametrize("text", ["Please unsubscribe me", "We are not interested"])
def test_refusal_or_unsubscribe_stops(env, text):
    client, app, _, mailer, now, _ = env
    lead_id = add(client)
    start(client, lead_id)
    reply(client, lead_id, text)
    result = detail(client, lead_id)
    assert result["status"] == "stopped"
    assert result["stop_reason"] and result["conversation"]["draft"] == ""
    now[0] += 90
    app.state.service.process_due()
    assert len(mailer.sent) == 1
    assert start(client, lead_id, "new-start-request").status_code == 409


def test_manual_stop_during_followup_generation(env):
    client, app, model, mailer, now, _ = env
    lead_id = add(client)
    start(client, lead_id)
    model.hook = lambda kind: (
        client.post(f"/api/leads/{lead_id}/stop", json={"operation_id": "stop-operation"})
        if kind == "followup"
        else None
    )
    now[0] += 61
    app.state.service.process_due()
    assert len(mailer.sent) == 1
    result = detail(client, lead_id)
    assert result["status"] == "stopped" and result["tasks"][1]["status"] == "cancelled"


def test_reply_during_followup_generation(env):
    client, app, model, mailer, now, _ = env
    lead_id = add(client)
    start(client, lead_id)
    model.hook = lambda kind: reply(client, lead_id) if kind == "followup" else None
    now[0] += 61
    app.state.service.process_due()
    assert len(mailer.sent) == 1
    assert detail(client, lead_id)["status"] == "replied"


def test_failed_reply_analysis_does_not_restore_followup(env):
    client, app, model, mailer, now, _ = env
    lead_id = add(client)
    start(client, lead_id)
    model.failure = True
    reply(client, lead_id)
    result = detail(client, lead_id)
    assert result["status"] == "replied" and result["tasks"][1]["status"] == "cancelled"
    assert result["messages"][-1]["status"] == "failed"
    model.failure = False
    reply_id = result["messages"][-1]["id"]
    assert (
        client.post(
            f"/api/replies/{reply_id}/retry", json={"operation_id": "retry-reply-operation"}
        ).status_code
        == 202
    )
    assert detail(client, lead_id)["messages"][-1]["status"] == "completed"
    now[0] += 70
    app.state.service.process_due()
    assert len(mailer.sent) == 1


def test_first_generation_failure_and_manual_retry(env):
    client, _, model, mailer, _, _ = env
    lead_id = add(client)
    model.failure = True
    start(client, lead_id)
    result = detail(client, lead_id)
    assert result["status"] == "new" and len(result["tasks"]) == 1
    assert result["tasks"][0]["status"] == "failed" and not mailer.sent
    model.failure = False
    task_id = result["tasks"][0]["id"]
    client.post(f"/api/tasks/{task_id}/retry", json={"operation_id": "retry-operation"})
    result = detail(client, lead_id)
    assert result["status"] == "awaiting_reply" and result["tasks"][0]["attempts"] == 2


def test_known_smtp_failure_reuses_ai_output(env):
    client, _, model, mailer, _, _ = env
    lead_id = add(client)
    mailer.failure = SendFailed("授权失败")
    start(client, lead_id)
    task = detail(client, lead_id)["tasks"][0]
    assert task["status"] == "failed" and task["body"]
    mailer.failure = None
    client.post(f"/api/tasks/{task['id']}/retry", json={"operation_id": "retry-operation"})
    assert len(model.calls) == 1 and len(mailer.sent) == 1


def test_uncertain_smtp_is_never_retried(env):
    client, app, _, mailer, now, _ = env
    lead_id = add(client)
    mailer.failure = SendUncertain("结果待核实")
    start(client, lead_id)
    task = detail(client, lead_id)["tasks"][0]
    assert task["status"] == "uncertain"
    assert (
        client.post(f"/api/tasks/{task['id']}/retry", json={"operation_id": "retry-operation"}).status_code
        == 409
    )
    mailer.failure = None
    now[0] += 80
    app.state.service.process_due()
    start(client, lead_id, "second-start-operation")
    assert not mailer.sent


def test_whitelist_missing_config_and_validation(env):
    client, app, _, mailer, _, settings = env
    lead_id = add(client, email="unrelated@example.com")
    assert start(client, lead_id).status_code == 403
    settings.api_key = ""
    second_id = add(client)
    assert start(client, second_id, "second-start-operation").status_code == 503
    assert not mailer.sent
    assert client.post("/api/leads", json={**LEAD, "email": "invalid"}).status_code == 422
    assert client.post("/api/leads", json={**LEAD, "website": "javascript:alert(1)"}).status_code == 422
    assert reply(client, second_id).status_code == 409
    assert client.get("/api/leads/9999").status_code == 404
    assert app.state.store.one("SELECT COUNT(*) AS n FROM email_tasks")["n"] == 0


def test_csv_partial_import_bom_duplicates_and_missing_fields(env):
    client, _, _, _, _, _ = env
    csv = "\ufeff姓名,公司,职位,官网,邮箱,行业,国家/地区,公司背景\nEmma,Northline,Buyer,https://example.com,test@example.com,Beauty,UK,小批量采购包装\nEmma,Northline,Buyer,https://example.com,test@example.com,Beauty,UK,小批量采购包装\nBad,Company,Buyer,https://example.com,invalid,Beauty,UK,background\n"
    result = client.post("/api/leads/import", files={"file": ("test.csv", csv.encode(), "text/csv")})
    assert result.status_code == 200
    assert [r["status"] for r in result.json()["results"]] == ["created", "skipped", "failed"]
    assert len(client.get("/api/leads").json()["leads"]) == 1


def test_operation_id_cannot_be_reused_for_different_payload(env):
    client, _, _, _, _, _ = env
    lead_id = add(client)
    start(client, lead_id)
    reply(client, lead_id)
    assert reply(client, lead_id, "Different reply").status_code == 409


def test_repeated_concurrent_dispatch_submits_once(env):
    client, app, _, mailer, _, _ = env
    lead_id = add(client)
    task = app.state.service.start(lead_id, "start-operation")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: app.state.service.process_task(task["task_id"]), range(6)))
    assert len(mailer.sent) == 1
    assert detail(client, lead_id)["status"] == "awaiting_reply"


def test_restart_recovers_due_and_does_not_resend(env):
    client, _, model, mailer, now, settings = env
    lead_id = add(client)
    start(client, lead_id)
    now[0] += 65
    restarted = create_app(settings, EmailAgent(model), mailer, clock=lambda: now[0], scheduler_enabled=False)
    with TestClient(restarted) as other:
        restarted.state.service.process_due()
        restarted.state.service.process_due()
        assert detail(other, lead_id)["status"] == "followup_complete"
        assert len(mailer.sent) == 2


def test_restart_parks_inflight_smtp_for_verification(env):
    client, app, model, mailer, now, settings = env
    lead_id = add(client)
    task = app.state.service.start(lead_id, "start-operation")
    with app.state.store.connect() as db:
        db.execute("UPDATE email_tasks SET status='sending' WHERE id=?", (task["task_id"],))
    restarted = create_app(settings, EmailAgent(model), mailer, clock=lambda: now[0], scheduler_enabled=False)
    with TestClient(restarted) as other:
        restarted.state.service.process_due()
        assert detail(other, lead_id)["tasks"][0]["status"] == "uncertain"
        assert not mailer.sent


def test_manual_stop_cannot_be_overwritten_by_reply_analysis(env):
    client, _, model, _, _, _ = env
    lead_id = add(client)
    start(client, lead_id)
    model.hook = lambda kind: (
        client.post(f"/api/leads/{lead_id}/stop", json={"operation_id": "stop-operation"})
        if kind == "reply"
        else None
    )
    reply(client, lead_id)
    result = detail(client, lead_id)
    assert result["status"] == "stopped" and result["stop_reason"] == "人工停止"
    assert result["conversation"]["draft"] == ""


def test_configuration_never_returns_secrets(env):
    client, _, _, _, _, settings = env
    content = client.get("/api/config").text
    assert settings.api_key not in content and settings.smtp_password not in content


def test_earlier_refusal_finishing_late_still_stops(env):
    client, app, _, mailer, now, _ = env
    lead_id = add(client)
    start(client, lead_id)
    earlier = app.state.service.record_reply(lead_id, "earlier-reply-operation", "Please unsubscribe me")
    reply(client, lead_id, "Please share sample options", "later-reply-operation")
    assert detail(client, lead_id)["conversation"]["draft"]
    app.state.service.analyze_reply(earlier["reply_id"])
    result = detail(client, lead_id)
    assert result["status"] == "stopped" and result["stop_reason"]
    assert result["conversation"]["draft"] == ""
    now[0] += 100
    app.state.service.process_due()
    assert len(mailer.sent) == 1


def model_response(output, status=200):
    return httpx.Response(
        status,
        json={
            "choices": [{"finish_reason": "stop", "message": {"content": output}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        },
    )


def test_real_model_adapter_repairs_invalid_output_and_counts_usage():
    calls = []
    valid = dict(
        subject="Packaging sample options",
        body="We would be happy to share sample packaging options for your upcoming product launch.",
    )

    def handle(request):
        calls.append(json.loads(request.content))
        return model_response("not JSON" if len(calls) == 1 else json.dumps(valid))

    model = BailianClient(
        Settings(api_key="secret", base_url="https://model.example.com"), httpx.MockTransport(handle)
    )
    try:
        output, usage = model.generate("followup", {})
        assert output == valid
        assert usage["calls"] == 2 and usage["input_tokens"] == 200
        assert calls[0]["enable_thinking"] is False
    finally:
        model.close()


def test_model_repairs_unprovided_offer_before_content_can_be_sent():
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        body = (
            "We offer free samples at no cost. Best regards, [Your Name]"
            if len(calls) == 1
            else "Would reviewing recyclable packaging sample options be helpful? Best regards, PackPilot Team"
        )
        return model_response(json.dumps({"subject": "Sample options", "body": body}))

    model = BailianClient(
        Settings(api_key="secret", base_url="https://model.example.com"), httpx.MockTransport(handle)
    )
    try:
        output, usage = model.generate("followup", {})
        assert "free" not in output["body"] and "[Your Name]" not in output["body"]
        assert usage["calls"] == 2
    finally:
        model.close()


def test_model_invalid_output_is_bounded_and_key_error_does_not_retry():
    calls = []

    def malformed(request):
        calls.append(1)
        return model_response("{}")

    model = BailianClient(
        Settings(api_key="secret", base_url="https://model.example.com"), httpx.MockTransport(malformed)
    )
    with pytest.raises(ModelError):
        model.generate("first", {})
    model.close()
    assert len(calls) == 2
    calls.clear()

    def forbidden(request):
        calls.append(1)
        return httpx.Response(401, json={"error": "secret"})

    model = BailianClient(
        Settings(api_key="secret", base_url="https://model.example.com"), httpx.MockTransport(forbidden)
    )
    with pytest.raises(ModelError) as exc:
        model.generate("first", {})
    model.close()
    assert len(calls) == 1 and "secret" not in str(exc.value)
