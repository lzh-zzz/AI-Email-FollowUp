import sqlite3

from fastapi.testclient import TestClient
from test_workflow import add, detail, reply, start
from test_workflow import env as env

from app.ai import EmailAgent
from app.mail import SendFailed, SendUncertain
from app.main import create_app
from app.schemas import DraftInput


def prepared(client):
    lead_id = add(client)
    start(client, lead_id)
    reply(client, lead_id)
    return lead_id


def payload(client, lead_id, operation="send-draft-operation", **changes):
    lead = detail(client, lead_id)
    return {
        "operation_id": operation,
        "reply_id": lead["conversation"]["latest_reply_id"],
        "subject": lead["conversation"]["draft_subject"],
        "body": "Hi Emma, please review our packaging overview. Best regards, PackPilot Team",
        **changes,
    }


def upload(
    client, lead_id, reply_id, filename="资料.pdf", body=b"%PDF-demo", operation="upload-file-operation"
):
    return client.post(
        f"/api/leads/{lead_id}/attachments",
        data={"reply_id": reply_id, "operation_id": operation},
        files={"file": (filename, body, "application/pdf")},
    )


def send(client, lead_id, data):
    return client.post(f"/api/leads/{lead_id}/send-draft", json=data)


def test_multiple_reply_rounds_with_editing_attachments_and_no_automatic_resend(env):
    client, app, model, mailer, now, _ = env
    lead_id = prepared(client)
    data = payload(
        client,
        lead_id,
        body="Hi Emma, I have attached our overview. Please share your requirements. PackPilot Team",
    )
    assert (
        client.post(
            f"/api/leads/{lead_id}/draft", json={**data, "operation_id": "save-draft-operation"}
        ).status_code
        == 200
    )
    assert detail(client, lead_id)["conversation"]["draft"] == data["body"]
    result = upload(client, lead_id, data["reply_id"])
    assert result.status_code == 201
    attachment_id = result.json()["attachment_id"]
    downloaded = client.get(f"/api/attachments/{attachment_id}")
    assert downloaded.content == b"%PDF-demo"
    assert "filename*=UTF-8''" in downloaded.headers["content-disposition"]
    assert send(client, lead_id, data).status_code == 202
    assert send(client, lead_id, data).json()["scheduled"] is False
    assert (
        send(client, lead_id, {**data, "operation_id": "another-send-operation"}).json()["scheduled"] is False
    )
    lead = detail(client, lead_id)
    assert lead["status"] == "awaiting_customer" and lead["conversation"]["draft"] == ""
    assert len(mailer.sent) == 2
    assert mailer.deliveries[-1]["body"] == data["body"]
    assert mailer.deliveries[-1]["attachments"][0]["content"] == b"%PDF-demo"
    assert "content" not in lead["attachments"][0]
    now[0] += 120
    app.state.service.process_due()
    assert len(mailer.sent) == 2
    reply(client, lead_id, "Could we arrange a call?", "next-customer-reply")
    next_data = payload(client, lead_id, "send-second-reply")
    assert next_data["reply_id"] != data["reply_id"]
    assert send(client, lead_id, next_data).status_code == 202
    assert len(mailer.sent) == 3 and not mailer.deliveries[-1]["attachments"]
    assert len([t for t in detail(client, lead_id)["tasks"] if t["kind"] == "reply"]) == 2
    assert model.calls == ["first", "reply", "reply"]


def test_attachment_claim_without_file_is_rejected_before_creating_task(env):
    client, _, _, mailer, _, _ = env
    lead_id = prepared(client)
    data = payload(
        client,
        lead_id,
        body="Attached please find our detailed materials catalog. Best regards, PackPilot Team",
    )
    assert send(client, lead_id, data).status_code == 422
    assert len(mailer.sent) == 1
    assert not any(t["kind"] == "reply" for t in detail(client, lead_id)["tasks"])
    assert upload(client, lead_id, data["reply_id"]).status_code == 201
    assert send(client, lead_id, data).status_code == 202


def test_failed_reply_retries_same_content_and_files_without_more_ai(env):
    client, _, model, mailer, _, _ = env
    lead_id = prepared(client)
    data = payload(client, lead_id)
    upload(client, lead_id, data["reply_id"])
    mailer.failure = SendFailed("授权失败")
    send(client, lead_id, data)
    task = detail(client, lead_id)["tasks"][-1]
    assert task["status"] == "failed"
    assert upload(client, lead_id, data["reply_id"], operation="upload-another-operation").status_code == 409
    assert (
        send(
            client,
            lead_id,
            {
                **data,
                "operation_id": "changed-draft-operation",
                "body": "A changed draft with different commitments.",
            },
        ).status_code
        == 409
    )
    mailer.failure = None
    assert (
        client.post(
            f"/api/tasks/{task['id']}/retry", json={"operation_id": "retry-outbound-reply"}
        ).status_code
        == 202
    )
    assert len(mailer.sent) == 2 and model.calls == ["first", "reply"]
    assert mailer.deliveries[-1]["attachments"][0]["content"] == b"%PDF-demo"
    assert detail(client, lead_id)["tasks"][-1]["attempts"] == 2


def test_uncertain_reply_pauses_following_round(env):
    client, app, _, mailer, now, _ = env
    lead_id = prepared(client)
    data = payload(client, lead_id)
    mailer.failure = SendUncertain("提交结果不确定")
    send(client, lead_id, data)
    task = detail(client, lead_id)["tasks"][-1]
    assert task["status"] == "uncertain"
    mailer.failure = None
    assert (
        client.post(
            f"/api/tasks/{task['id']}/retry", json={"operation_id": "retry-uncertain-reply"}
        ).status_code
        == 409
    )
    reply(client, lead_id, "More information please", "following-customer-reply")
    assert send(client, lead_id, payload(client, lead_id, "send-following-round")).status_code == 409
    now[0] += 200
    app.state.service.process_due()
    assert len(mailer.sent) == 1


def test_new_customer_reply_cancels_queued_outbound_and_rejects_stale_editor(env):
    client, app, _, mailer, _, _ = env
    lead_id = prepared(client)
    data = payload(client, lead_id)
    task = app.state.service.send_draft(lead_id, DraftInput(**data))
    reply(client, lead_id, "Please use different materials", "newer-customer-reply")
    app.state.service.process_task(task["task_id"])
    assert detail(client, lead_id)["tasks"][-1]["status"] == "cancelled"
    assert (
        send(
            client,
            lead_id,
            {
                **data,
                "operation_id": "stale-editor-operation",
                "body": "A revised old draft that is no longer current.",
            },
        ).status_code
        == 409
    )
    assert upload(client, lead_id, data["reply_id"]).status_code == 409
    assert len(mailer.sent) == 1


def test_stop_or_refusal_blocks_draft_send_and_later_interest_does_not_reopen(env):
    client, _, _, mailer, _, _ = env
    lead_id = prepared(client)
    data = payload(client, lead_id)
    client.post(f"/api/leads/{lead_id}/stop", json={"operation_id": "end-conversation-operation"})
    assert send(client, lead_id, data).status_code == 409
    reply(client, lead_id, "Please share options", "later-interest-operation")
    assert detail(client, lead_id)["conversation"]["draft"] == ""
    assert send(client, lead_id, payload(client, lead_id, "send-after-stop-operation")).status_code == 409
    assert len(mailer.sent) == 1


def test_new_reply_during_smtp_does_not_clear_its_new_draft(env):
    client, _, _, mailer, _, _ = env
    lead_id = prepared(client)
    original_send = mailer.send

    def arrive(lead, task, first):
        if task["kind"] == "reply":
            reply(client, lead_id, "Please clarify sizes", "reply-during-submission")
            assert (
                send(client, lead_id, payload(client, lead_id, "overlapping-send-operation")).status_code
                == 409
            )
        original_send(lead, task, first)

    mailer.send = arrive
    send(client, lead_id, payload(client, lead_id))
    lead = detail(client, lead_id)
    assert lead["status"] == "replied" and lead["conversation"]["draft"]
    assert len(mailer.sent) == 2 and lead["tasks"][-1]["status"] == "sent"


def test_restart_resumes_queued_reply_with_saved_attachment_once(env):
    client, app, model, mailer, now, settings = env
    lead_id = prepared(client)
    data = payload(client, lead_id)
    upload(client, lead_id, data["reply_id"], body=b"persisted attachment bytes")
    app.state.service.send_draft(lead_id, DraftInput(**data))
    restarted = create_app(settings, EmailAgent(model), mailer, clock=lambda: now[0], scheduler_enabled=False)
    with TestClient(restarted) as other:
        restarted.state.service.process_due()
        restarted.state.service.process_due()
        assert detail(other, lead_id)["status"] == "awaiting_customer"
        assert len(mailer.sent) == 2
        assert mailer.deliveries[-1]["attachments"][0]["content"] == b"persisted attachment bytes"
        assert model.calls == ["first", "reply"]


def test_upload_limits_duplicates_name_sanitizing_and_removal(env):
    client, _, _, _, _, _ = env
    lead_id = prepared(client)
    reply_id = detail(client, lead_id)["conversation"]["latest_reply_id"]
    assert upload(client, lead_id, reply_id, body=b"").status_code == 413
    assert upload(client, lead_id, reply_id, body=b"x" * (5 * 1024 * 1024 + 1)).status_code == 413
    first = upload(client, lead_id, reply_id, filename="../资料.pdf")
    assert first.status_code == 201
    same = upload(client, lead_id, reply_id, filename="../资料.pdf", operation="duplicate-upload-operation")
    assert first.json() == same.json()
    assert detail(client, lead_id)["attachments"][0]["filename"] == "资料.pdf"
    for i in range(4):
        assert (
            upload(
                client, lead_id, reply_id, filename=f"file{i}.pdf", operation=f"upload-operation-{i}"
            ).status_code
            == 201
        )
    assert (
        upload(
            client, lead_id, reply_id, filename="sixth.pdf", operation="sixth-upload-operation"
        ).status_code
        == 413
    )
    attachment_id = first.json()["attachment_id"]
    assert client.delete(f"/api/leads/{lead_id}/attachments/{attachment_id}").status_code == 200
    assert client.get(f"/api/attachments/{attachment_id}").status_code == 404


def test_total_attachment_size_limit(env):
    client, _, _, _, _, _ = env
    lead_id = prepared(client)
    reply_id = detail(client, lead_id)["conversation"]["latest_reply_id"]
    for i in range(2):
        assert (
            upload(
                client,
                lead_id,
                reply_id,
                filename=f"large{i}.pdf",
                body=b"x" * (5 * 1024 * 1024),
                operation=f"upload-large-{i}",
            ).status_code
            == 201
        )
    assert (
        upload(
            client, lead_id, reply_id, filename="extra.pdf", operation="upload-too-large-total"
        ).status_code
        == 413
    )


def test_existing_database_migrates_without_losing_sent_messages(env):
    client, app, model, mailer, now, settings = env
    lead_id = add(client)
    start(client, lead_id)
    old_ids = [t["id"] for t in detail(client, lead_id)["tasks"]]
    db = sqlite3.connect(settings.db_path)
    db.executescript("""CREATE TABLE legacy_tasks (
      id INTEGER PRIMARY KEY,lead_id INTEGER NOT NULL REFERENCES leads(id),kind TEXT NOT NULL CHECK(kind IN ('first','followup')),
      status TEXT NOT NULL DEFAULT 'pending',subject TEXT NOT NULL DEFAULT '',body TEXT NOT NULL DEFAULT '',due_at REAL NOT NULL,
      sent_at REAL,attempts INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',error_phase TEXT NOT NULL DEFAULT '',
      message_id TEXT UNIQUE NOT NULL,usage TEXT NOT NULL DEFAULT '{}',UNIQUE(lead_id,kind));
      INSERT INTO legacy_tasks SELECT id,lead_id,kind,status,subject,body,due_at,sent_at,attempts,error,error_phase,message_id,usage FROM email_tasks;
      DROP TABLE email_tasks; ALTER TABLE legacy_tasks RENAME TO email_tasks;
      ALTER TABLE conversations DROP COLUMN draft_subject;
    """)
    db.close()
    restarted = create_app(settings, EmailAgent(model), mailer, clock=lambda: now[0], scheduler_enabled=False)
    with TestClient(restarted) as other:
        lead = detail(other, lead_id)
        assert [t["id"] for t in lead["tasks"]] == old_ids
        assert lead["messages"][0]["task_id"] == old_ids[0]
        assert restarted.state.store.all("PRAGMA foreign_key_check") == []
        reply(other, lead_id)
        assert send(other, lead_id, payload(other, lead_id)).status_code == 202
        assert detail(other, lead_id)["status"] == "awaiting_customer"
