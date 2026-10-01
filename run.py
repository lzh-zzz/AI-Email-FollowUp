import asyncio
import os
import socket
import webbrowser


def main():
    print("正在启动 AI 邮件工作台，请稍候……", flush=True)
    try:
        import uvicorn

        from app.config import Settings
        from app.main import create_app
    except ImportError:
        print("启动失败：运行依赖不完整。请先运行 uv sync --locked，或使用 start.bat。", flush=True)
        return 1

    try:
        settings = Settings.load()
    except ValueError:
        print("启动失败：.env 中端口和等待秒数必须是整数，请检查配置。", flush=True)
        return 1
    except OSError:
        print("启动失败：无法读取本地配置，请检查 .env 文件和访问权限。", flush=True)
        return 1
    if not 1 <= settings.port <= 65535:
        print("启动失败：APP_PORT 必须在 1–65535 范围内。", flush=True)
        return 1

    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(settings.host, settings.host)
    browser_host = f"[{host}]" if ":" in host else host
    url = f"http://{browser_host}:{settings.port}"

    class DesktopServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if not self.started:
                return
            print(f"启动成功！访问地址：{url}", flush=True)
            print("服务运行中，请保持终端开启；按 Ctrl+C 停止。", flush=True)
            if settings.issues():
                print("配置未就绪：" + "；".join(settings.issues()) + "。请按页面提示配置。", flush=True)
            print("正在打开默认浏览器……", flush=True)
            try:
                opened = await asyncio.to_thread(webbrowser.open, url, new=1)
            except Exception:
                opened = False
            if not opened:
                print(f"无法自动打开浏览器，请手动打开：{url}（服务已启动）。", flush=True)

    server = None
    phase = "监听地址和端口"
    try:
        family = socket.AF_INET6 if ":" in settings.host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as listener:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Reserve the port before initializing the database or scheduler.
            listener.bind((settings.host, settings.port))
            phase = "初始化应用"
            config = uvicorn.Config(
                create_app(settings),
                host=settings.host,
                port=settings.port,
                log_level="warning",
                lifespan="on",
            )
            server = DesktopServer(config)
            phase = "启动服务"
            server.run(sockets=[listener])
            if not server.started:
                print("启动失败：服务未能就绪，请检查上方错误提示。", flush=True)
                return 1
    except KeyboardInterrupt:
        print("服务已停止。" if server and server.started else "启动已取消。", flush=True)
        return 0
    except SystemExit:
        print("启动失败：服务初始化未完成，请检查上方错误提示。", flush=True)
        return 1
    except Exception as exc:
        label = "服务运行失败" if server and server.started else "启动失败"
        advice = ""
        if phase == "监听地址和端口":
            advice = "请检查 APP_HOST / APP_PORT；端口可能被占用，请关闭其他实例或更换端口。"
        print(f"{label}（{phase}）：{settings.redact(str(exc))}。{advice}", flush=True)
        return 1
    print("服务已停止。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
