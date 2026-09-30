"""Read local credentials without displaying them. Authenticate only; do not send email."""

import json
import smtplib
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ai import BailianClient, ModelError
from app.config import ROOT, Settings
from app.main import SAMPLES


def main():
    settings = Settings.load()
    if settings.issues():
        print(json.dumps({"configuration_ready": False, "missing": settings.issues()}, ensure_ascii=False))
        return 1
    report = {"configuration_ready": True}
    model = BailianClient(settings)
    try:
        output, usage = model.generate("first", {"lead": SAMPLES[0]})
        report["ai"] = {
            "ok": True,
            "model": settings.model,
            "usage": usage,
            "has_profile": bool(output["profile"]),
        }
        artifact = ROOT / "artifacts"
        artifact.mkdir(exist_ok=True)
        (artifact / "model-probe.json").write_text(
            json.dumps({"output": output, "usage": usage}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except ModelError as exc:
        report["ai"] = {"ok": False, "reason": str(exc)}
    finally:
        model.close()
    try:
        with smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=12, context=ssl.create_default_context()
        ) as smtp:
            smtp.login(settings.smtp_username, settings.smtp_password)
        report["smtp_auth"] = {"ok": True, "email_sent": False}
    except smtplib.SMTPAuthenticationError:
        report["smtp_auth"] = {"ok": False, "reason": "QQ 邮箱授权失败，请检查授权码和 SMTP 服务。"}
    except (OSError, smtplib.SMTPException):
        report["smtp_auth"] = {"ok": False, "reason": "QQ SMTP 连接失败，请检查网络和端口。"}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ai"]["ok"] and report["smtp_auth"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
