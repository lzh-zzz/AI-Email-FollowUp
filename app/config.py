import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values
from pydantic import EmailStr, TypeAdapter, ValidationError

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    api_key: str = ""
    base_url: str = ""
    model: str = "qwen3.7-flash"
    enable_thinking: bool = False
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    smtp_ssl: bool = True
    followup_delay: int = 60
    host: str = "127.0.0.1"
    port: int = 8000
    db_path: Path = ROOT / "data" / "demo.db"

    @classmethod
    def load(cls, path: Path | None = None):
        values = {**dotenv_values(path or ROOT / ".env"), **os.environ}

        def value(key, default=""):
            return str(values.get(key) or default).strip()

        return cls(
            api_key=value("DASHSCOPE_API_KEY"),
            base_url=value("DASHSCOPE_BASE_URL").rstrip("/"),
            model=value("DASHSCOPE_MODEL", "qwen3.7-flash"),
            enable_thinking=value("DASHSCOPE_ENABLE_THINKING", "false").lower() == "true",
            smtp_username=value("SMTP_USERNAME"),
            smtp_password=value("SMTP_PASSWORD"),
            smtp_host=value("SMTP_HOST", "smtp.qq.com"),
            smtp_port=int(value("SMTP_PORT", "465")),
            smtp_ssl=value("SMTP_USE_SSL", "true").lower() == "true",
            followup_delay=max(1, int(value("FOLLOWUP_DELAY_SECONDS", "60"))),
            host=value("APP_HOST", "127.0.0.1"),
            port=int(value("APP_PORT", "8000")),
        )

    def issues(self) -> list[str]:
        missing = []
        for name, val in [
            ("DASHSCOPE_API_KEY", self.api_key),
            ("DASHSCOPE_BASE_URL", self.base_url),
            ("SMTP_USERNAME", self.smtp_username),
            ("SMTP_PASSWORD", self.smtp_password),
        ]:
            if not val or any(marker in val for marker in ["YOUR_", "你的真实", "请填写"]):
                missing.append(name)
        if self.base_url and (
            urlparse(self.base_url).scheme != "https" or not urlparse(self.base_url).netloc
        ):
            missing.append("DASHSCOPE_BASE_URL 必须是 HTTPS 地址")
        if self.smtp_username:
            try:
                TypeAdapter(EmailStr).validate_python(self.smtp_username)
            except ValidationError:
                missing.append("SMTP_USERNAME 邮箱格式不正确")
        if not 1 <= self.smtp_port <= 65535 or not 1 <= self.port <= 65535:
            missing.append("端口必须在 1–65535 范围内")
        return missing

    def public(self):
        return {
            "ready": not self.issues(),
            "ai_ready": not any(issue.startswith("DASHSCOPE") for issue in self.issues()),
            "missing": self.issues(),
            "model": self.model,
            "sender": self.smtp_username,
            "followup_delay": self.followup_delay,
            "reply_source": "manual_simulation",
            "product": "PackPilot · 定制环保包装服务（虚构演示）",
        }

    def redact(self, text: str) -> str:
        for secret in [self.api_key, self.smtp_password]:
            if secret:
                text = text.replace(secret, "[已隐藏]")
        return text
