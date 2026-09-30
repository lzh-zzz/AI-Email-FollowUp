$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (!(Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'Please install uv first: https://docs.astral.sh/uv/getting-started/installation/'
}
uv sync --locked
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
uv run --no-sync python run.py
