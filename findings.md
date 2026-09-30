# 实现发现

- 本地 uv 可用，系统 python 是 WindowsApps 占位；可使用桌面内置 Python 创建虚拟环境。
- 百炼 OpenAI 兼容接口支持 JSON 输出；按 .env 中的完整 Base URL 与模型名调用。
- LangGraph StateGraph 支持 TypedDict、条件边和编译执行；业务状态以 SQLite 为准。
- APScheduler 采用 3.x 单进程后台调度，任务计划保存在业务表，启动时恢复检查。
- Gmail 已替换为 QQ SMTP；仅需发信，模拟回复由界面录入。
- 官网仅保存参考链接，客户背景来自录入资料。

参考：
- https://docs.langchain.com/oss/python/langgraph/graph-api
- https://help.aliyun.com/zh/model-studio/qwen-structured-output
- https://apscheduler.readthedocs.io/en/3.x/userguide.html
- https://fastapi.tiangolo.com/advanced/events/
