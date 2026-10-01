# 实现发现

- 本地 uv 可用，系统 python 是 WindowsApps 占位；可使用桌面内置 Python 创建虚拟环境。
- 百炼 OpenAI 兼容接口支持 JSON 输出；按 .env 中的完整 Base URL 与模型名调用。
- LangGraph StateGraph 支持 TypedDict、条件边和编译执行；业务状态以 SQLite 为准。
- APScheduler 采用 3.x 单进程后台调度，任务计划保存在业务表，启动时恢复检查。
- Gmail 已替换为 QQ SMTP；仅需发信，模拟回复由界面录入。
- 初始范围中官网仅作参考；当前版本已支持读取公开首页及同站 About 页面，人工背景与官网提取背景独立保存。

参考：
- https://docs.langchain.com/oss/python/langgraph/graph-api
- https://help.aliyun.com/zh/model-studio/qwen-structured-output
- https://apscheduler.readthedocs.io/en/3.x/userguide.html
- https://fastapi.tiangolo.com/advanced/events/
# 官网读取追加

采用现有 HTTP/AI 边界和 Python 标准库 HTMLParser，无新增运行服务。读取公开 HTML 首页和最多一个同站 About 链接，有限正文进入单次背景提取，再用摘要与引用生成邮件；不执行脚本或抓取整站。参考 Python 官方文档：https://docs.python.org/3/library/http.client.html 和 https://docs.python.org/3/library/html.parser.html。

网页网络连接固定到预先验证的公网 IP，HTTPS 仍验证官网域名证书；每次重定向重新校验目标。读取状态与公司背景独立保存，不覆盖人工背景或历史已发邮件。

真实读取 https://www.python.org/ 和 /about/ 成功，qwen3.7-flash 一次提取返回中文背景及三条原文依据，输入 2058 / 输出 372 Token，不发邮件。About 服务器即使请求 identity 仍返回 gzip，已支持有大小上限的 gzip/deflate 解压并验证压缩炸弹拒绝。

隔离页面已验证空人工背景可保存、首封按钮禁用、点击官网读取显示处理中且不发邮件；等待真实 AI 背景提取结果继续检查来源展示。

隔离页面真实百炼结果已显示首页与 About 两个来源、中文摘要、可展开的原文依据（输入 2058 / 输出 381 Token），首封按钮在完成后启用，邮件记录仍为空。原文依据与手动背景的 details 在读取状态不变时不会被轮询重绘收起。

## 2026-10-01：交付收尾

- 用户选择本地交付，通过 GitHub 获取代码，在接收方电脑配置凭据后启动；不部署线上服务。
- 用户明确确认首封与自动跟进均已收到，并要求跳过收件日期与时间。验收可记录人工确认，但不能编造收信时间、截图或声称已直接读取收件箱。
- 当前 PATH 及常见安装路径未发现 gh；按 issue-tracker.md 报告该限制，不冒充完成远程 Issue 发布。
- 实际读取 GitHub main 得到 e740bfa5cae600b95a75c656e024bd49a831eba0，独立克隆成功，远程功能代码与本地一致；不再把开发阶段网络失败描述为当前未同步。
- 独立环境用已准备的 Python 3.12.14 安装锁定依赖，82 项测试、Ruff、JS 全通过。真实启动脚本 3.45 秒健康，重启 1.56 秒且数据保留；空凭据、零邮件任务，无真实发信。
