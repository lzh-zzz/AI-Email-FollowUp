import pytest
from fastapi.testclient import TestClient
from test_conversation import payload, prepared, send, upload
from test_workflow import add, detail
from test_workflow import env as env

from app.ai import EmailAgent
from app.main import create_app


def test_delete_full_conversation_cleans_related_files_and_keeps_other_lead(env):
    client, app, _, mailer, now, _ = env
    lead_id = prepared(client)
    data = payload(client, lead_id)
    attachment_id = upload(client, lead_id, data["reply_id"]).json()["attachment_id"]
    assert send(client, lead_id, data).status_code == 202
    with app.state.store.connect(transaction=True) as db:
        db.execute(
            "INSERT INTO website_research(lead_id,status,operation_id,summary,updated_at) VALUES (?,'completed','fixture-website','客户官网摘要',?)",
            (lead_id, now[0]),
        )
    other = add(client, email="second@example.com")
    assert client.delete(f"/api/leads/{lead_id}").json() == {"deleted": True}
    assert client.get(f"/api/leads/{lead_id}").status_code == 404
    assert client.get(f"/api/attachments/{attachment_id}").status_code == 404
    for table in [
        "messages",
        "email_tasks",
        "operations",
        "attachments",
        "conversations",
        "website_research",
    ]:
        assert app.state.store.all(f"SELECT * FROM {table} WHERE lead_id=?", (lead_id,)) == []
    assert detail(client, other)["email"] == "second@example.com"
    assert app.state.store.all("PRAGMA foreign_key_check") == []
    assert client.delete(f"/api/leads/{lead_id}").json() == {"deleted": False}
    now[0] += 120
    app.state.service.process_due()
    assert len(mailer.sent) == 2


def test_deleted_ids_are_not_reused_after_restart_and_old_queued_jobs_do_nothing(env):
    client, app, model, mailer, now, settings = env
    lead_id = add(client)
    first = app.state.service.start(lead_id, "queued-first-operation")["task_id"]
    assert client.delete(f"/api/leads/{lead_id}").json()["deleted"]
    restarted = create_app(settings, EmailAgent(model), mailer, clock=lambda: now[0], scheduler_enabled=False)
    with TestClient(restarted) as other:
        new_id = add(other)
        assert new_id > lead_id
        new_task = restarted.state.service.start(new_id, "new-first-operation")["task_id"]
        assert new_task > first
        restarted.state.service.process_task(first)
        assert not mailer.sent and not model.calls
        restarted.state.service.process_task(new_task)
        assert len(mailer.sent) == 1
        assert (
            other.post(
                f"/api/leads/{lead_id}/start", json={"operation_id": "stale-browser-operation"}
            ).status_code
            == 404
        )


@pytest.mark.parametrize("active", ["processing", "sending", "reply-analysis", "website-reading"])
def test_delete_refuses_active_work_without_removing_records(env, active):
    client, app, _, _, now, _ = env
    lead_id = prepared(client)
    with app.state.store.connect(transaction=True) as db:
        if active in {"processing", "sending"}:
            db.execute("UPDATE email_tasks SET status=? WHERE lead_id=? AND kind='first'", (active, lead_id))
        elif active == "reply-analysis":
            db.execute(
                "UPDATE messages SET status='analyzing' WHERE lead_id=? AND direction='inbound'", (lead_id,)
            )
        else:
            db.execute(
                "INSERT INTO website_research(lead_id,status,operation_id,updated_at) VALUES (?,'reading','active-website',?)",
                (lead_id, now[0]),
            )
    before = detail(client, lead_id)
    assert client.delete(f"/api/leads/{lead_id}").status_code == 409
    assert detail(client, lead_id) == before
    assert app.state.store.all("PRAGMA foreign_key_check") == []


def test_delete_during_real_flow_claim_is_blocked_and_send_remains_recorded(env):
    client, app, model, mailer, _, _ = env
    lead_id = add(client)
    blocked = []

    def during_model(kind):
        blocked.append(client.delete(f"/api/leads/{lead_id}").status_code)

    model.hook = during_model
    original = mailer.send

    def during_smtp(lead, task, first):
        blocked.append(client.delete(f"/api/leads/{lead_id}").status_code)
        original(lead, task, first)

    mailer.send = during_smtp
    app.state.service.process_task(app.state.service.start(lead_id, "active-first-operation")["task_id"])
    assert blocked == [409, 409]
    assert detail(client, lead_id)["tasks"][0]["status"] == "sent"
    assert client.delete(f"/api/leads/{lead_id}").json()["deleted"]


def test_message_and_attachment_identifiers_not_reused(env):
    client, app, _, _, _, _ = env
    first = prepared(client)
    old_message = detail(client, first)["conversation"]["latest_reply_id"]
    old_attachment = upload(client, first, old_message).json()["attachment_id"]
    client.delete(f"/api/leads/{first}")
    new_id = prepared(client)
    new_message = detail(client, new_id)["conversation"]["latest_reply_id"]
    new_attachment = upload(client, new_id, new_message).json()["attachment_id"]
    assert new_message > old_message and new_attachment > old_attachment
    app.state.service.analyze_reply(old_message)
    assert client.get(f"/api/attachments/{old_attachment}").status_code == 404
    assert detail(client, new_id)["conversation"]["latest_reply_id"] == new_message


def test_cancelled_generation_finishing_after_delete_cannot_touch_new_lead(env):
    client, app, model, mailer, _, _ = env
    lead_id = add(client)
    new_ids = []

    def cancel_and_delete(kind):
        app.state.service.stop(lead_id, "stop-before-delete")
        assert client.delete(f"/api/leads/{lead_id}").json()["deleted"]
        new_ids.append(add(client))

    model.hook = cancel_and_delete
    task = app.state.service.start(lead_id, "cancelled-generation")
    app.state.service.process_task(task["task_id"])
    assert not mailer.sent
    assert detail(client, new_ids[0])["profile"] == ""
    assert not detail(client, new_ids[0])["tasks"]
