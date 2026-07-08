from __future__ import annotations

import json
import logging
from collections.abc import Sequence

import typer
import uvicorn

from lack_bot.config import Settings, build_config_checks, get_settings, public_settings_summary
from lack_bot.detector import detect_output
from lack_bot.models import DetectionResult, NotificationRequest, TaskResult, TaskStatus
from lack_bot.notifier.lark import LarkBotClient
from lack_bot.runner import run_command
from lack_bot.storage.sqlite import SQLiteNotificationStore

app = typer.Typer(help="Lack Bot: Lark/Feishu notifications for code agent tasks.")


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
def send_test(message: str = typer.Option("hello from lack-bot", "--message", "-m")) -> None:
    """Send a test notification through the configured Lark/Feishu app Bot."""
    settings = get_settings()
    configure_logging(settings.log_level)
    task = TaskResult(
        name="send-test",
        command=["lack-bot", "send-test"],
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

    typer.echo("Lack Bot configuration")
    for key, value in summary.items():
        typer.echo(f"{key}: {value}")
    typer.echo("")
    typer.echo("Checks")
    for check in checks:
        mark = "ok" if check.ok else "missing"
        typer.echo(f"- {mark}: {check.name} - {check.message}")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    """Run the optional FastAPI callback server."""
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run("lack_bot.server.app:app", host=host, port=port, factory=False)


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
            "LACK_BOT_LARK_APP_ID": settings.lark_app_id,
            "LACK_BOT_LARK_APP_SECRET": settings.lark_app_secret,
            "LACK_BOT_LARK_RECEIVE_ID": settings.lark_receive_id,
        }.items()
        if not value
    ]
    if missing:
        raise typer.BadParameter(f"Missing required Lark settings: {', '.join(missing)}")
