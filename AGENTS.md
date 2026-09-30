# 项目协作约定

开发和验收 Demo 时读取 `SPEC.md`；业务术语以 `CONTEXT.md` 为准。规格中标记为待确认的内容应保持其草案状态，不能把未完成的验收报告为已通过。

## Agent skills

### Issue tracker

规格和任务使用 `lzh-zzz/AI-Email-FollowUp` 的 GitHub Issues，通过 `gh` 操作。具体约定见 `docs/agents/issue-tracker.md`。

### Triage labels

使用默认标签：`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`。含义见 `docs/agents/triage-labels.md`。

### Domain docs

采用单一业务上下文：根目录 `CONTEXT.md` 和按需创建的 `docs/adr/`。读取规则见 `docs/agents/domain.md`。
