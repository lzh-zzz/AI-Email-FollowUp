# AI 邮件 Demo 实现计划

目标：按已讨论范围实现本地可运行 Demo，接入百炼和 QQ SMTP；只使用 .env 白名单测试地址。

## 阶段

1. 配置检查、运行环境与项目骨架 — complete
2. SQLite 业务状态、LangGraph 流程、模型与 SMTP 接入 — complete
3. 客户录入 / CSV / 发信记录 / 模拟回复界面 — complete
4. API 集成测试与真实服务联调 — in_progress
5. README、演示步骤、启动验证与交付 — in_progress

## 约束

- 每轮最多自动跟进一次，成功后等待 60 秒。
- 任意回复先取消跟进；拒绝、退订、人工停止不可自动恢复。
- SMTP 不确定结果不得自动重发。
- 不输出凭据；真实收件确认与 SMTP 接受分别报告。
- GitHub Issue 发布不作为开发前置条件；当前没有 gh。

## 错误记录

暂无实现错误。
