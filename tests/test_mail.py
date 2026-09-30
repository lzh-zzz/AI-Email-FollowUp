import smtplib

import pytest

from app.config import Settings
from app.mail import QQMailer, SendFailed, SendUncertain


class SMTPStub:
    def __init__(self, failure=None, close_error=False):
        self.failure = failure
        self.close_error = close_error
        self.messages = []

    def login(self, username, password):
        if self.failure == "auth":
            raise smtplib.SMTPAuthenticationError(535, b"private authorization failure")

    def send_message(self, message):
        self.messages.append(message)
        if self.failure == "disconnect":
            raise smtplib.SMTPServerDisconnected("connection lost after submission")
        if self.failure == "reject":
            raise smtplib.SMTPDataError(550, b"rejected")
        return {}

    def close(self):
        if self.close_error:
            raise OSError("socket close failed")


def mailer(monkeypatch, smtp):
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **kw: smtp)
    return QQMailer(
        Settings(
            smtp_username="sender@example.com",
            smtp_password="private-password",
        )
    )


LEAD = {"email": "test@example.com"}
TASK = {
    "subject": "Re: Packaging samples",
    "body": "Can we discuss your sample packaging needs?",
    "message_id": "<followup@packpilot.demo>",
}


def test_smtp_acceptance_keeps_stable_id_and_thread_even_if_close_fails(monkeypatch):
    smtp = SMTPStub(close_error=True)
    mailer(monkeypatch, smtp).send(LEAD, TASK, {"message_id": "<first@packpilot.demo>"})
    message = smtp.messages[0]
    assert message["Message-ID"] == TASK["message_id"]
    assert message["In-Reply-To"] == message["References"] == "<first@packpilot.demo>"
    assert message["To"] == LEAD["email"]


@pytest.mark.parametrize(
    "failure,error", [("auth", SendFailed), ("reject", SendFailed), ("disconnect", SendUncertain)]
)
def test_smtp_classifies_failure_without_exposing_authorization(monkeypatch, failure, error):
    smtp = SMTPStub(failure)
    with pytest.raises(error) as exc:
        mailer(monkeypatch, smtp).send(LEAD, TASK)
    assert "private" not in str(exc.value)
    assert len(smtp.messages) == (0 if failure == "auth" else 1)


def test_smtp_connection_failure_can_be_retried(monkeypatch):
    def fail(*args, **kwargs):
        raise TimeoutError("connect timeout")

    smtp_mailer = mailer(monkeypatch, SMTPStub())
    monkeypatch.setattr(smtplib, "SMTP_SSL", fail)
    with pytest.raises(SendFailed):
        smtp_mailer.send(LEAD, TASK)


@pytest.mark.parametrize("address", ["customer@example.com", "another@example.org"])
def test_mail_boundary_sends_to_any_recipient(monkeypatch, address):
    smtp = SMTPStub()
    mailer(monkeypatch, smtp).send({"email": address}, TASK)
    assert len(smtp.messages) == 1
    assert smtp.messages[0]["To"] == address


def test_actual_mime_message_contains_uploaded_files_and_reply_headers(monkeypatch):
    smtp = SMTPStub()
    task = {
        **TASK,
        "attachments": [
            {"filename": "资料.pdf", "content_type": "application/pdf", "content": b"exact uploaded bytes"}
        ],
    }
    mailer(monkeypatch, smtp).send(LEAD, task, {"message_id": "<first@packpilot.demo>"})
    message = smtp.messages[0]
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "资料.pdf"
    assert attachments[0].get_payload(decode=True) == b"exact uploaded bytes"
    assert attachments[0].get_content_type() == "application/pdf"
    assert message["References"] == "<first@packpilot.demo>"
