from __future__ import annotations

import json
import logging
import sys
import uuid
from pathlib import Path
from collections.abc import Sequence

import httpx
from pydantic import ValidationError
import typer
import uvicorn

from lark_bot.adapters.codex import CodexEvent, codex_event_to_notification
from lark_bot.config import Settings, build_config_checks, get_settings, public_settings_summary
from lark_bot.detector import detect_output
from lark_bot.daemon import MAX_HOOK_BYTES, build_runtime, create_daemon_app, ensure_daemon_token
from lark_bot.hooks import check_hooks, install_hooks, uninstall_hooks
from lark_bot.models import DetectionResult, NotificationRequest, TaskResult, TaskStatus
from lark_bot.notifier.lark import LarkBotClient
from lark_bot.runner import run_command
from lark_bot.storage.sqlite import SQLiteNotificationStore

app = typer.Typer(help="Lark Bot: Lark/Feishu notifications for code agent tasks.")
codex_app = typer.Typer(help="Manage Codex sessions through the local daemon.")
hooks_app = typer.Typer(help="Manage project Codex hooks.")
app.add_typer(codex_app, name="codex")
codex_app.add_typer(hooks_app, name="hooks")


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(levelname)s %(name)s: %(message)s")


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(
    ctx: typer.Context,
    name: str = typer.Option("task", "--name", "-n", help="Human-readable task name."),
) -> None:
    """Run a command and send a Lark/Feishu notification when it finishes."""
    command = list(ctx.args)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise typer.BadParameter("Provide a command after --.")

    settings = get_settings()
    configure_logging(settings.log_level)
    task = run_command(command, name=name, tail_lines=settings.output_tail_lines)
    detection = detect_output(task.combined_tail_text, task.exit_code)
    request = NotificationRequest(task=task, detection=detection)
    _send_with_dedupe(request, settings)
    raise typer.Exit(task.exit_code)


@app.command("send-test")
def send_test(message: str = typer.Option("hello from lark-bot", "--message", "-m")) -> None:
    """Send a test notification through the configured Lark/Feishu app Bot."""
    settings = get_settings()
    configure_logging(settings.log_level)
    task = TaskResult(
        name="send-test",
        command=["lark-bot", "send-test"],
        exit_code=0,
        duration_seconds=0,
        stdout_tail=[message],
        stderr_tail=[],
    )
    detection = DetectionResult(status=TaskStatus.SUCCEEDED, tags=["manual_test"])
    _send_with_dedupe(NotificationRequest(task=task, detection=detection), settings)


@app.command("config")
def config_command(json_output: bool = typer.Option(False, "--json", help="Print JSON output.")) -> None:
    """Show safe configuration diagnostics."""
    settings = get_settings()
    checks = build_config_checks(settings)
    summary = public_settings_summary(settings)
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "settings": summary,
                    "checks": [check.model_dump() for check in checks],
                    "ok": all(check.ok for check in checks),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    typer.echo("Lark Bot configuration")
    for key, value in summary.items():
        typer.echo(f"{key}: {value}")
    typer.echo("")
    typer.echo("Checks")
    for check in checks:
        mark = "ok" if check.ok else "missing"
        typer.echo(f"- {mark}: {check.name} - {check.message}")


@app.command("codex-event")
def codex_event(
    file: Path | None = typer.Option(
        None,
        "--file",
        "-f",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        help="Read a Codex event JSON object from a file. Defaults to stdin.",
    )
) -> None:
    """Send a notification from a Codex event JSON payload."""
    payload = file.read_text(encoding="utf-8") if file else sys.stdin.read()
    try:
        request = build_codex_notification_from_json(payload)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    settings = get_settings()
    configure_logging(settings.log_level)
    _send_with_dedupe(request, settings)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    """Run the optional FastAPI callback server."""
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run("lark_bot.server.app:app", host=host, port=port, factory=False)


@app.command("daemon")
def daemon_command(
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port", min=1, max=65535),
) -> None:
    """Run the authenticated Codex orchestration daemon."""
    settings = get_settings()
    configure_logging(settings.log_level)
    token = ensure_daemon_token(settings.daemon_token_path)
    runtime = build_runtime(settings)
    uvicorn.run(
        create_daemon_app(runtime, token=token),
        host=host or settings.daemon_host,
        port=port or settings.daemon_port,
    )


def _daemon_request(method: str, path: str, *, json_body: dict | None = None) -> object:
    settings = get_settings()
    try:
        token = ensure_daemon_token(settings.daemon_token_path)
        response = httpx.request(
            method,
            f"http://{settings.daemon_host}:{settings.daemon_port}/api/v1/codex{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=json_body,
            timeout=min(settings.http_timeout_seconds, 10.0),
        )
        response.raise_for_status()
        return response.json()
    except (OSError, RuntimeError, httpx.HTTPError) as error:
        raise typer.BadParameter(f"Local Codex daemon request failed ({type(error).__name__}).") from None


def _emit_result(value: object, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(value, ensure_ascii=False, indent=2))
    elif isinstance(value, list):
        for item in value:
            typer.echo(f"{item.get('id')}  {item.get('status')}  {item.get('name')}")
    elif isinstance(value, dict):
        typer.echo(f"{value.get('id', 'ok')}  {value.get('status', 'ok')}  {value.get('name', '')}".rstrip())


@codex_app.command("start")
def codex_start(
    prompt: str = typer.Argument(...),
    name: str = typer.Option("task", "--name", "-n"),
    cwd: Path = typer.Option(Path("."), "--cwd"),
    model: str | None = typer.Option(None, "--model"),
    sandbox: str = typer.Option("workspace-write", "--sandbox"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    if sandbox not in {"read-only", "workspace-write"}:
        raise typer.BadParameter("sandbox must be read-only or workspace-write")
    text = sys.stdin.read() if prompt == "-" else prompt
    result = _daemon_request("POST", "/sessions", json_body={"name": name, "cwd": str(cwd.resolve()), "prompt": text, "model": model, "sandbox": sandbox})
    _emit_result(result, json_output)


@codex_app.command("list")
def codex_list(status: str | None = typer.Option(None, "--status"), json_output: bool = typer.Option(False, "--json")) -> None:
    query = f"?status={status}" if status else ""
    _emit_result(_daemon_request("GET", f"/sessions{query}"), json_output)


@codex_app.command("show")
def codex_show(session_id: str, json_output: bool = typer.Option(False, "--json")) -> None:
    _emit_result(_daemon_request("GET", f"/sessions/{session_id}"), json_output)


@codex_app.command("cancel")
def codex_cancel(session_id: str, json_output: bool = typer.Option(False, "--json")) -> None:
    _emit_result(_daemon_request("POST", f"/sessions/{session_id}/cancel"), json_output)


@hooks_app.command("install")
def hooks_install(project: Path = typer.Option(Path("."), "--project")) -> None:
    typer.echo(f"installed: {install_hooks(project)}")


@hooks_app.command("check")
def hooks_check(project: Path = typer.Option(Path("."), "--project")) -> None:
    result = check_hooks(project)
    typer.echo(result.status)
    if result.status != "installed":
        raise typer.Exit(1)


@hooks_app.command("uninstall")
def hooks_uninstall(project: Path = typer.Option(Path("."), "--project")) -> None:
    typer.echo(f"uninstalled: {uninstall_hooks(project)}")


@app.command("codex-hook")
def codex_hook() -> None:
    """Forward a Codex project hook without ever blocking Codex."""
    raw = sys.stdin.read(MAX_HOOK_BYTES + 1)
    try:
        encoded_size = len(raw.encode("utf-8"))
    except UnicodeError:
        return
    if encoded_size > MAX_HOOK_BYTES:
        return
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError):
        return
    if not isinstance(payload, dict):
        return
    event_name = next((payload.get(key) for key in ("hook_event_name", "event_name", "hook_name") if isinstance(payload.get(key), str)), None)
    if event_name not in {"SessionStart", "PermissionRequest", "Stop"}:
        return
    safe = {"hook_event_name": event_name}
    if isinstance(payload.get("event_id"), str):
        safe["event_id"] = payload["event_id"][:200]
    try:
        _daemon_request("POST", "/hooks", json_body=safe)
    except Exception:
        try:
            spool = get_settings().daemon_token_path.parent / "spool"
        except Exception:
            spool = Path(".lark-bot/spool")
        try:
            spool.mkdir(parents=True, exist_ok=True)
            path = spool / f"hook-{uuid.uuid4().hex}.json"
            path.write_text(json.dumps(safe, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass


def _send_with_dedupe(request: NotificationRequest, settings: Settings) -> None:
    _validate_lark_settings(settings)
    store = SQLiteNotificationStore(settings.sqlite_path)
    if not store.should_send(request.dedupe_key, settings.cooldown_seconds):
        logging.getLogger(__name__).info("Notification suppressed by cooldown.")
        return
    client = LarkBotClient(
        app_id=settings.lark_app_id,
        app_secret=settings.lark_app_secret,
        receive_id=settings.lark_receive_id,
        receive_id_type=settings.lark_receive_id_type,
        base_url=settings.lark_base_url,
        timeout_seconds=settings.http_timeout_seconds,
    )
    try:
        client.send(request)
    finally:
        client.close()
    store.record_sent(request.dedupe_key, request.detection.status.value)


def _validate_lark_settings(settings: Settings) -> None:
    missing = [
        name
        for name, value in {
            "LARK_BOT_LARK_APP_ID": settings.lark_app_id,
            "LARK_BOT_LARK_APP_SECRET": settings.lark_app_secret,
            "LARK_BOT_LARK_RECEIVE_ID": settings.lark_receive_id,
        }.items()
        if not value
    ]
    if missing:
        raise typer.BadParameter(f"Missing required Lark settings: {', '.join(missing)}")


def build_codex_notification_from_json(payload: str) -> NotificationRequest:
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("Codex event payload must be valid JSON.") from exc
    if not isinstance(raw, dict):
        raise ValueError("Codex event payload must be a JSON object.")
    try:
        event = CodexEvent.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"Invalid Codex event payload: {exc}") from exc
    return codex_event_to_notification(event)
