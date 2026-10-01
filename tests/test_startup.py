import builtins
import json
import os
import runpy
import shutil
import socket
import subprocess
from contextlib import asynccontextmanager
from urllib.request import ProxyHandler, build_opener

import pytest
import uvicorn
from fastapi import FastAPI

from app.config import ROOT, Settings


@pytest.fixture
def startup_settings(tmp_path, monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    settings = Settings(port=port, db_path=tmp_path / "startup.db")
    monkeypatch.setattr(Settings, "load", lambda: settings)

    async def finish_after_startup(self):
        self.should_exit = True

    monkeypatch.setattr(uvicorn.Server, "main_loop", finish_after_startup)
    return settings


def launch():
    try:
        runpy.run_path(str(ROOT / "run.py"), run_name="__main__")
    except SystemExit as exc:
        return exc.code
    return 0


@pytest.mark.parametrize("browser_result", [True, False, "error"])
def test_launch_opens_browser_only_when_own_service_is_ready(
    startup_settings, monkeypatch, capsys, browser_result
):
    calls = []

    def open_browser(url, **options):
        # A real HTTP request at the moment of opening catches premature launches.
        with build_opener(ProxyHandler({})).open(url + "/api/health", timeout=3) as response:
            calls.append((url, json.load(response)))
        if browser_result == "error":
            raise OSError("no browser available")
        return browser_result

    monkeypatch.setattr("webbrowser.open", open_browser)
    assert launch() == 0
    output = capsys.readouterr().out
    assert len(calls) == 1
    assert calls[0] == (
        f"http://127.0.0.1:{startup_settings.port}",
        {"status": "ok", "configuration_ready": False},
    )
    assert "启动成功" in output
    assert "配置未就绪" in output
    assert "Ctrl+C" in output
    if browser_result is not True:
        assert "请手动打开" in output


def test_occupied_port_reports_failure_without_initializing_app_or_opening_browser(
    startup_settings, monkeypatch, capsys
):
    calls = []
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: calls.append(a))
    monkeypatch.setattr("app.main.create_app", lambda *a, **k: pytest.fail("app must not start"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", startup_settings.port))
        listener.listen()
        assert launch() == 1
    assert not calls
    assert "启动失败" in capsys.readouterr().out
    assert not startup_settings.db_path.exists()


def test_invalid_configuration_reports_failure_without_echoing_value(monkeypatch, capsys):
    def invalid_settings():
        raise ValueError("invalid value private-secret")

    monkeypatch.setattr(Settings, "load", invalid_settings)
    assert launch() == 1
    output = capsys.readouterr().out
    assert "启动失败" in output
    assert "private-secret" not in output


def test_unreadable_configuration_reports_failure(monkeypatch, capsys):
    def unreadable_settings():
        raise PermissionError("private path")

    monkeypatch.setattr(Settings, "load", unreadable_settings)
    assert launch() == 1
    output = capsys.readouterr().out
    assert "启动失败" in output
    assert "private path" not in output


def test_application_initialization_failure_reports_failure(
    startup_settings, monkeypatch, capsys
):
    calls = []

    def fail(*args, **kwargs):
        raise OSError("database directory is not writable")

    monkeypatch.setattr("app.main.create_app", fail)
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: calls.append(a))
    assert launch() == 1
    assert "启动失败" in capsys.readouterr().out
    assert not calls
    # Failure must release the reserved port.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", startup_settings.port))


@pytest.mark.parametrize("port", [0, 65536])
def test_invalid_port_is_rejected_before_creating_database(startup_settings, capsys, port):
    startup_settings.port = port
    assert launch() == 1
    assert "启动失败" in capsys.readouterr().out
    assert not startup_settings.db_path.exists()


def test_all_interfaces_opens_loopback_address(startup_settings, monkeypatch):
    startup_settings.host = "0.0.0.0"
    calls = []

    def open_browser(url, **options):
        with build_opener(ProxyHandler({})).open(url + "/api/health", timeout=3) as response:
            calls.append((url, json.load(response)["status"]))
        return True

    monkeypatch.setattr("webbrowser.open", open_browser)
    assert launch() == 0
    assert calls == [(f"http://127.0.0.1:{startup_settings.port}", "ok")]


def test_application_lifespan_failure_does_not_report_success_or_open_browser(
    startup_settings, monkeypatch, capsys
):
    @asynccontextmanager
    async def fail_startup(app):
        raise RuntimeError("startup fixture failure")
        yield

    monkeypatch.setattr("app.main.create_app", lambda *a, **k: FastAPI(lifespan=fail_startup))
    calls = []
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: calls.append(a))
    assert launch() == 1
    output = capsys.readouterr().out
    assert "启动失败" in output
    assert "启动成功" not in output
    assert not calls


def test_missing_dependency_has_installation_advice(monkeypatch, capsys):
    original = builtins.__import__

    def import_without_uvicorn(name, *args, **kwargs):
        if name == "uvicorn":
            raise ModuleNotFoundError("uvicorn")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_uvicorn)
    assert launch() == 1
    output = capsys.readouterr().out
    assert "启动失败" in output
    assert "uv sync --locked" in output


@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell.exe"), reason="Windows launcher")
@pytest.mark.parametrize("failure", ["missing_uv", "dependencies", "application"])
def test_windows_launcher_reports_failure_and_propagates_exit_code(failure):
    setup = {
        "missing_uv": "function Get-Command { return $null }",
        "dependencies": "function uv { $global:LASTEXITCODE = 2 }",
        "application": "function uv { if ($args[0] -eq 'run') { $global:LASTEXITCODE = 7 } "
        "else { $global:LASTEXITCODE = 0 } }",
    }[failure]
    result = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
            + setup + "; & $env:STARTUP_TEST_SCRIPT; exit $LASTEXITCODE",
        ],
        env={**os.environ, "STARTUP_TEST_SCRIPT": str(ROOT / "start.ps1")},
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == (7 if failure == "application" else 1)
    output = result.stdout.decode("utf-8", errors="replace")
    if "启动失败" not in output:
        output = result.stdout.decode("gbk", errors="replace")
    assert "启动失败" in output
