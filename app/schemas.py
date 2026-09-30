import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class LeadInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    company: str = Field(min_length=1, max_length=150)
    title: str = Field(min_length=1, max_length=150)
    website: str = Field(min_length=1, max_length=500)
    email: EmailStr
    industry: str = Field(min_length=1, max_length=100)
    country: str = Field(min_length=1, max_length=100)
    background: str = Field(min_length=1, max_length=3000)
    source: str = Field(default="手动录入", max_length=100)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v):
        return str(v).lower()

    @field_validator("website")
    @classmethod
    def validate_website(cls, v):
        if not v.startswith(("https://", "http://")) or " " in v:
            raise ValueError("官网请填写完整 http:// 或 https:// 地址")
        return v


class EmailContent(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    subject: str = Field(min_length=1, max_length=160)
    body: str = Field(
        min_length=30,
        max_length=4000,
        description="English email. Sign as PackPilot Team; no placeholders, prices or free sample promises.",
    )

    @field_validator("body")
    @classmethod
    def grounded_body(cls, v):
        if re.search(r"\[(?:your|sender|insert|company|date|time|link)[^\]]*\]", v, re.I):
            raise ValueError("邮件不得含姓名、公司或签名占位符；使用 PackPilot Team 签名")
        if re.search(
            r"\bat no cost\b|\bno[- ]cost\b|\bcomplimentary\b|\bfree[- ](?:sample|pack|prototyp|shipping)|[$€£]\s*\d",
            v,
            re.I,
        ):
            raise ValueError("产品资料没有价格、免费样品或免费寄送，不能做出这些承诺")
        if re.search(r"\b(?:i|we)(?:'ve|’ve| have) been following\b", v, re.I):
            raise ValueError("未开展官网调查或长期观察，不能声称一直关注客户公司")
        return v

    @field_validator("subject")
    @classmethod
    def no_header_injection(cls, v):
        if "\n" in v or "\r" in v:
            raise ValueError("主题不能包含换行")
        return v


class OutreachResult(EmailContent):
    profile: str = Field(
        min_length=5, max_length=1200, description="中文客户画像，80-160字，体现行业和职位职责"
    )
    angle: str = Field(min_length=5, max_length=800, description="中文切入点，40-100字，针对客户背景")
    evidence: list[str] = Field(min_length=1, max_length=6, description="中文，来自输入的已知依据")
    assumptions: list[str] = Field(
        default_factory=list, max_length=6, description="中文，最多三条推测，不能当事实写入邮件"
    )

    @field_validator("profile", "angle")
    @classmethod
    def chinese_analysis(cls, v):
        if not re.search(r"[\u4e00-\u9fff]", v):
            raise ValueError("客户画像和切入点必须用中文")
        return v


class ReplyResult(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    intent: Literal["高", "中", "低", "拒绝"] | None
    stop: bool
    stop_reason: str = Field(max_length=300)
    reason: str = Field(min_length=1, max_length=1000)
    summary: str = Field(min_length=1, max_length=800)
    suggestion: str = Field(min_length=1, max_length=1200)
    draft: str = Field(default="", max_length=4000)

    @field_validator("draft")
    @classmethod
    def grounded_draft(cls, v):
        return EmailContent.grounded_body(v)


class Operation(BaseModel):
    operation_id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class ReplyInput(Operation):
    text: str = Field(min_length=1, max_length=5000)

    @field_validator("text")
    @classmethod
    def require_content(cls, v):
        if not v.strip():
            raise ValueError("回复内容不能为空")
        return v.strip()
