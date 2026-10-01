# 本地 Demo 交付说明

交付整理：2026-10-01。用户选择本地运行方式；接收方获取代码后在自己的电脑配置第三方服务，无需线上 Demo 地址。

## 获取固定版本

交付标签：`demo-local-2026-10-01`。该标签固定本次功能代码与交付文档，后续 main 更新不会改变它。

- [浏览交付版本](https://github.com/lzh-zzz/AI-Email-FollowUp/tree/demo-local-2026-10-01)
- [下载交付 ZIP](https://github.com/lzh-zzz/AI-Email-FollowUp/archive/refs/tags/demo-local-2026-10-01.zip)：解压后在项目目录操作，不要求安装 Git。

也可用 Git 获取：

```powershell
git clone --branch demo-local-2026-10-01 --depth 1 https://github.com/lzh-zzz/AI-Email-FollowUp.git
cd AI-Email-FollowUp
```

## 接收方启动

1. 准备 uv、Python 3.12 运行环境、自己的百炼 API Key、QQ SMTP 授权码和获准测试邮箱。uv 可以自动下载 Python；首次运行需要可用网络。
2. 将 `.env.example` 复制为 `.env`，按 [README](../README.md#快速启动) 填写配置；已有 `.env` 时保留原文件。
3. Windows 双击 `start.bat`，或执行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1`。
4. 浏览器打开 http://127.0.0.1:8000，确认配置就绪，再按 [演示讲稿](demo-guide.md) 操作。生成与发送只能使用本人或公司提供的测试邮箱。

第三方账号与运行环境准备完成后的启动目标为五分钟内；首次账号申请、授权码设置和下载耗时取决于接收方环境。没有提供公共测试账号，接收方必须配置自己的凭据。仓库不包含开发者的 `.env`、数据库、真实邮箱记录或联调原始文件。

如 uv 自动安装 Python 提示目标目录缺失，先准备正常的 Python 3.12 安装，然后在项目目录显式指定其 `python.exe` 路径执行 `uv sync --locked --python <实际路径>`，再运行启动脚本。也可按 README 使用 Python 3.12+ 虚拟环境和 requirements.txt 安装。

## 本次独立验证

- 通过实际 GitHub 读取确认远程 main 的功能代码基线为 `e740bfa5cae600b95a75c656e024bd49a831eba0`，与本地一致；从远程克隆独立目录。
- 独立环境使用本机已准备的 Python 3.12.14 和锁定依赖；未复制开发者 `.env` 或业务数据库。首次 uv 托管 Python 自动安装出现目标目录缺失，改用正常运行时后通过；不将安装失败记录为启动成功。
- 该远程功能代码运行 **82 项测试全部通过**，Ruff 与前端 JavaScript 语法检查通过。
- 在独立目录实际运行 `start.ps1`，使用空凭据及独立端口，3.45 秒健康就绪；首页和 JavaScript 返回 HTTP 200。缺少凭据正确显示配置未就绪。
- 创建虚构客户后重启，1.56 秒健康就绪，记录仍在，邮件任务数量为零。本次独立启动验证没有调用真实模型或发送真实邮件。
- 本次收尾仅更新交付文档和计划记录；功能代码沿用上述已测试基线。最终交付提交以标签解析结果为准，可运行 `git rev-parse HEAD` 与 `git ls-remote origin refs/tags/demo-local-2026-10-01` 比对。
- 交付文档提交 `5195a20bc39600223c2ef9657883b8b7d774c18e` 已推送 GitHub，并在独立目录重新 fetch 和 checkout 核验取得。本记录随后随交付标签固定；再次比对确认 app、static、tests、scripts、启动文件及锁定依赖与通过测试的功能基线完全相同。仓库未包含 `.env`、业务数据库、联调原始文件或本机配置的真实凭据。

依赖安装使用本机缓存；上述实测时间不能作为所有新电脑首次下载所需时间的承诺。详细证据和历史真实服务联调见 [verification.md](verification.md)。

## 验收边界

用户已确认首封和自动跟进均实际收到；按用户要求不记录收件日期和时间。附件实际到达及可打开的独立确认、推荐测试边界正式确认仍保留为待确认，不将它们报告为已通过。

本项目回复使用页面手动模拟，符合 Demo 范围；没有自动读取邮箱回复。仅支持本地单进程运行，关闭或休眠时不执行跟进，重启后检查到期任务。代码仓库用于获取源码，http://127.0.0.1:8000 是接收方电脑上的本地地址。
