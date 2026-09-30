# AI 邮件代发与自动跟进 Demo

本地运行的海外销售获客工作台。输入客户背景后，真实 AI 生成画像、切入点和英文开发邮件，通过 QQ SMTP 发送；60 秒未录入回复时自动生成并发送一封跟进。录入模拟回复后判断意向、提供建议和回复草稿，拒绝、退订或人工停止会结束自动跟进。

产品 **PackPilot** 和三组客户公司均为虚构演示资料。邮件确实发出，只允许使用本人或公司提供的测试邮箱。

## 快速启动

准备条件：安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，准备百炼 API Key、QQ SMTP 授权码、获准测试收件地址。`gh` 不是启动依赖。

```powershell
git clone https://github.com/lzh-zzz/AI-Email-FollowUp.git
cd AI-Email-FollowUp
Copy-Item .env.example .env
```

如果已经有 `.env`，保留原文件，不要覆盖。编辑 `.env`：

| 参数 | 填写内容 |
| --- | --- |
| `DASHSCOPE_API_KEY` | 百炼模型调用 API Key |
| `DASHSCOPE_BASE_URL` | 与 Key 地域、业务空间匹配的兼容接口地址，末尾 `/compatible-mode/v1` |
| `DASHSCOPE_MODEL` | 默认 `qwen3.7-flash`，本 Demo 已实测可调用 |
| `SMTP_USERNAME` | 发件人的完整 QQ 邮箱地址 |
| `SMTP_PASSWORD` | 同一邮箱的 SMTP 授权码，**不是 QQ 登录密码** |
| `TEST_RECIPIENTS` | 测试收件白名单；多个地址用英文逗号分隔 |

北京地域接口格式：

```text
https://业务空间ID.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
```

QQ 邮箱设置中开启 SMTP 服务并生成授权码。默认 `smtp.qq.com:465`、SSL 加密。发件地址用于登录和发送，测试地址用于收信，允许二者相同。不要把授权码写进源码、CSV 或 GitHub。

Windows 双击 `start.bat`，或在项目目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

跨平台也可直接运行：

```sh
uv sync --locked
uv run --no-sync python run.py
```

打开 **http://127.0.0.1:8000**。首次运行安装锁定依赖和所需 Python；之后通常数秒内启动。`Ctrl+C` 停止。凭据准备完成、运行环境和网络可用后可在五分钟内启动。无需前端编译或单独数据库服务。

不使用 uv 时，可在 Python 3.12+ 虚拟环境运行 `python -m pip install -r requirements.txt`，再运行 `python run.py`。开发验收推荐 uv，以使用完整锁定依赖及测试工具。

## 使用方式

1. 点击“录入”，选择虚构样例，填写测试收件地址并保存。姓名、公司、职位、官网、邮箱、行业、国家/地区、公司背景均必填。
2. 点击“AI 分析并发送首封”。这会真实调用模型并发信；客户资料导入本身不会发送。
3. 查看中文画像、依据与推测、英文邮件、发送状态和 Token 用量。
4. 保持应用运行，不录入回复，约一分钟后发送唯一一封自动跟进。生成与 SMTP 连接增加几秒耗时。
5. 录入模拟回复，查看真实 AI 意向、建议和回复草稿。草稿仅展示，**不会发出**。
6. 录入明确拒绝或退订，或点击“停止自动跟进”，查看停止原因。停止后不能重新开发同一客户。

下载 CSV 样例，替换示例邮箱再导入。支持中文或英文表头、UTF-8（含 BOM）、最多 100 行 / 250 KB；逐行报告错误和重复。数据库按标准化邮箱去重，一个测试地址只能对应一个客户。

凭据或白名单变更后点击“重新加载配置”，任务执行时暂不允许加载。修改监听地址或端口需要重启。同名系统环境变量优先于 `.env`。

## 技术栈与架构

- Python 3.12+、FastAPI：本地 API 和静态 HTML/CSS/JavaScript 页面。
- LangGraph：首封生成、跟进生成、回复分类和停止/草稿分支。
- SQLite：客户、会话、消息、发送任务、操作去重记录。
- APScheduler：每 2 秒检查数据库的到期任务。
- 百炼 OpenAI 兼容接口：`qwen3.7-flash`，默认关闭深度思考。
- Python 标准库 `smtplib`：QQ SMTP SSL/TLS 真实发送。

```mermaid
flowchart TD
    UI[本地网页：客户 / CSV / 模拟回复 / 停止] --> API[FastAPI 业务服务]
    API <--> DB[(SQLite 业务状态)]
    API --> Graph[LangGraph AI 流程]
    Graph --> Bailian[百炼模型 API]
    Graph --> API
    Timer[APScheduler 每 2 秒检查] --> DB
    Timer --> API
    API --> Sender[发送前再次检查状态和白名单]
    Sender --> SMTP[QQ SMTP]
    SMTP --> Inbox[获准测试邮箱]
    Sender --> DB
```

本地组件在同一进程。LangGraph 负责 AI 分支，数据库负责持久业务状态，调度器负责计时。模型不会自行循环调用发信工具。画像和首封合并一次调用，回复摘要、意向和草稿合并一次调用，跟进复用已保存画像以控制 Token。

目录：`app/` 后端；`static/` 页面与 CSV；`tests/` 测试；`scripts/` 联调工具；`data/demo.db` 本地数据库；`docs/demo-guide.md` 演示讲稿。规格见 `SPEC.md`，术语见 `CONTEXT.md`。

## 异常与防重复

| 异常 | 行为 |
| --- | --- |
| 模型限流、超时、临时 5xx | 最多额外两次退避重试，失败保存原因 |
| Key / 模型权限异常 | 明确报错，修正配置后手动重试 |
| 模型结构异常或已识别的违规内容 | 最多一次重新生成，仍不合格不发送 |
| QQ 授权失败、SMTP 明确拒收 | 保存原因；重试原任务，有效正文不重复生成 |
| 提交时断线或重启前停在发送中 | 标记“结果待核实”，禁止重发，需人工查收件箱 |
| 重复点击、调度器重复触发 | 操作标识、唯一任务约束、原子领取防重复 |
| 回复或停止与跟进并发 | 回复先保存并取消未发任务；发送前再次检查状态 |
| 应用重启 | 恢复到期检查，已发送/已取消任务不再执行 |

内容校验包含主题换行、字段、枚举、长度，以及部分明确的未提供承诺与占位符。它不能证明所有文本事实正确，演示前应阅读生成内容；本项目不作为生产营销系统直接使用。

## 测试与真实验收

```sh
uv run pytest -q
uv run ruff check app scripts tests run.py
```

自动测试使用临时 SQLite、真实 LangGraph 及外部模型/SMTP 替身，不发真实邮件。覆盖到期、重复/并发、取消跟进、拒绝/退订、模型异常、SMTP 失败/不确定结果和重启恢复。

下列工具读取本地配置并产生真实模型费用，**均不发送邮件**：

```sh
uv run python -X utf8 scripts/check_services.py
uv run python -X utf8 scripts/verify_ai.py
```

前者调用一次 AI 并验证 QQ SMTP 登录；后者生成三组客户邮件并分析五类回复。结果在被 Git 忽略的 `artifacts/`。真实邮件通过页面验收，并人工查看测试邮箱；“SMTP 已接受”不等于已到达收件箱。

测试账号不随仓库提供，每位演示者填写自己的百炼 Key、QQ 授权码和测试邮箱。应用无需登录。验收记录见 `docs/verification.md`。

## 5–10 分钟演示

按 [演示讲稿](docs/demo-guide.md) 执行。使用一位客户演示首封 → 等待一分钟 → 自动跟进 → 录入回复 → 判断意向 → 拒绝停止。要展示“倒计时内回复会取消跟进”，用另一个不同白名单邮箱的客户。

只有一个测试邮箱而需要重新演示时，先停止应用，将整个 `data` 目录**改名保留为备份**，再重新启动。应用创建新数据库，原记录仍在备份目录，可在停止应用后恢复。不要在运行中修改数据库。

## 已知限制

- 回复仅在页面手动模拟；QQ 收件箱收到回复不会自动被识别。
- 每个客户最多一封首封和一封跟进；不自动发回复草稿，不恢复已停止客户。
- 不抓取官网；画像依赖录入资料，推测单独展示。
- 仅本地单进程，默认 `127.0.0.1`，无多用户验证，不配置多个 worker。
- 关闭或休眠时不执行任务；重启后处理仍符合条件的到期任务。
- SMTP 不保证外部投递恰好一次；不查询垃圾邮件、退信或已读状态。
- `data/` 保存测试邮箱与往来内容；`.env`、数据库和联调结果不提交到 Git。
