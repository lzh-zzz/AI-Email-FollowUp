$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
try {
    if (!(Get-Command uv -ErrorAction SilentlyContinue)) {
        throw '未找到 uv，请先安装：https://docs.astral.sh/uv/getting-started/installation/'
    }
    Write-Host '正在检查并安装运行依赖，请稍候……'
    uv sync --locked
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查网络和 Python 运行环境。' }
    uv run --no-sync python run.py
    $launcherExitCode = $LASTEXITCODE
    if ($launcherExitCode -ne 0) {
        Write-Host '启动失败：应用已退出，请根据上方提示处理。' -ForegroundColor Red
    }
    exit $launcherExitCode
} catch {
    Write-Host ('启动失败：' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
