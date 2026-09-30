import uvicorn

from app.config import Settings
from app.main import create_app

if __name__ == "__main__":
    try:
        settings = Settings.load()
    except ValueError:
        raise SystemExit("配置错误：.env 中端口和等待秒数必须是整数。") from None
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="warning")
