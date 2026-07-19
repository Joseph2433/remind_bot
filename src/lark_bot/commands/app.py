from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from collections.abc import Sequence

import httpx
import click
import typer
import uvicorn
from typer.core import TyperGroup

from lark_bot.config import build_config_checks, get_settings, public_settings_summary
from lark_bot.modules.claude.claude_service import build_claude_notification_from_json
from lark_bot.modules.claude.claude_hook_adapter import (
    handle_callback as handle_claude_callback,
    read_stdin_payload as read_claude_stdin_payload,
)
from lark_bot.modules.claude.claude_hook_installer import (
    check_hooks as check_claude_hooks,
    install_hooks as install_claude_hooks,
    uninstall_hooks as uninstall_claude_hooks,
)
from lark_bot.modules.claude.claude_tui import (
    ClaudeTuiLauncher,
    ClaudeTuiOptions,
    disabled_hook_environment,
)
from lark_bot.modules.codex.codex_hook_adapter import forward_existing_notify, handle_callback, read_stdin_payload
from lark_bot.modules.codex.codex_tui import CodexTuiLauncher, CodexTuiOptions
from lark_bot.modules.task.task_detector import detect_output
from lark_bot.server.daemon import build_runtime, create_daemon_app, ensure_daemon_token
from lark_bot.modules.codex.codex_hook import check_hooks, install_hooks, uninstall_hooks
from lark_bot.modules.notification.notification_model import NotificationRequest
from lark_bot.modules.task.task_model import DetectionResult, TaskResult, TaskStatus
from lark_bot.modules.task.task_runner import run_command
from lark_bot.commands.codex_args import uses_remote_resume_picker as _uses_remote_resume_picker
from lark_bot.commands.common import (
    build_codex_notification_from_json,
    configure_logging,
    send_with_dedupe as _send_with_dedupe,
)

app = typer.Typer(help="Lark Bot: Lark/Feishu notifications for code agent tasks.")


class _CodexFallbackGroup(TyperGroup):
    """Treat unknown Codex subcommands/prompts as native TUI arguments."""

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        command = super().get_command(ctx, cmd_name)
        if command is not None:
            return command

        @click.pass_context
        def forward(sub_ctx: click.Context) -> None:
            parent = sub_ctx.parent
            no_lark = bool(parent and parent.params.get("no_lark"))
            _run_codex_tui([cmd_name, *sub_ctx.args], no_lark=no_lark)

        return click.Command(
            name=cmd_name,
            callback=forward,
            add_help_option=False,
            context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
            hidden=True,
        )


class _ClaudeFallbackGroup(TyperGroup):
    """Treat unknown Claude subcommands/options as native CLI arguments."""

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        command = super().get_command(ctx, cmd_name)
        if command is not None:
            return command

        @click.pass_context
        def forward(sub_ctx: click.Context) -> None:
            parent = sub_ctx.parent
            no_lark = bool(parent and parent.params.get("no_lark"))
            _run_claude_tui([cmd_name, *sub_ctx.args], no_lark=no_lark)

        return click.Command(
            name=cmd_name,
            callback=forward,
            add_help_option=False,
            context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
            hidden=True,
        )


codex_app = typer.Typer(
    cls=_CodexFallbackGroup,
    help="Launch native Codex or manage unattended jobs.",
    invoke_without_command=True,
    no_args_is_help=False,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
job_app = typer.Typer(help="Manage unattended Codex sessions through the local daemon.")
hooks_app = typer.Typer(help="Manage project Codex hooks.")
claude_app = typer.Typer(
    cls=_ClaudeFallbackGroup,
    help="Launch native Claude Code or manage unattended jobs.",
    invoke_without_command=True,
    no_args_is_help=False,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
claude_job_app = typer.Typer(help="Manage unattended Claude sessions through the local daemon.")
claude_hooks_app = typer.Typer(help="Manage project Claude hooks.")
app.add_typer(codex_app, name="codex")
app.add_typer(claude_app, name="claude")
codex_app.add_typer(job_app, name="job")
codex_app.add_typer(hooks_app, name="hooks")
claude_app.add_typer(claude_job_app, name="job")
claude_app.add_typer(claude_hooks_app, name="hooks")

REMOTE_RESUME_PICKER_MESSAGE = (
    "Use resume --last or an explicit session ID; the remote session picker "
    "is unsupported; use --no-lark."
)
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
def send_test(
    message: str = typer.Option("hello from lark-bot", "--message", "-m"),
    message_format: str | None = typer.Option(
        None,
        "--format",
        help="Outbound format: card or text. Defaults to LARK_BOT_MESSAGE_FORMAT.",
    ),
) -> None:
    """Send a test notification through the configured Lark/Feishu app Bot."""
    settings = get_settings()
    if message_format is not None:
        normalized = message_format.strip().casefold()
        if normalized not in {"card", "text"}:
            raise typer.BadParameter("--format must be card or text")
        settings = settings.model_copy(update={"message_format": normalized})
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


@app.command("claude-event")
def claude_event(
    file: Path | None = typer.Option(
        None,
        "--file",
        "-f",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        help="Read a Claude event JSON object from a file. Defaults to stdin.",
    )
) -> None:
    """Send a notification from a Claude Code Hook/event JSON payload."""

    payload = file.read_text(encoding="utf-8") if file else sys.stdin.read()
    try:
        request = build_claude_notification_from_json(payload)
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


def _agent_daemon_request(
    agent: str,
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
) -> object:
    settings = get_settings()
    try:
        token = ensure_daemon_token(settings.daemon_token_path)
        response = httpx.request(
            method,
            f"http://{settings.daemon_host}:{settings.daemon_port}/api/v1/agents/{agent}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=json_body,
            timeout=min(settings.http_timeout_seconds, 10.0),
        )
        response.raise_for_status()
        return response.json()
    except (OSError, RuntimeError, httpx.HTTPError) as error:
        raise typer.BadParameter(f"Local {agent.title()} daemon request failed ({type(error).__name__}).") from None


# Descriptive alias used by integrations that call the generic helper directly.
_daemon_agent_request = _agent_daemon_request


def _daemon_request(method: str, path: str, *, json_body: dict | None = None) -> object:
    """Backward-compatible Codex daemon request wrapper."""
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


def _daemon_hook_request(payload: dict[str, str]) -> None:
    """Use a very short bound so Codex notify never waits on a sick daemon."""

    settings = get_settings()
    token = ensure_daemon_token(settings.daemon_token_path)
    response = httpx.post(
        f"http://{settings.daemon_host}:{settings.daemon_port}/api/v1/codex/hooks",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=min(settings.http_timeout_seconds, 0.25),
    )
    response.raise_for_status()


def _emit_result(value: object, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(value, ensure_ascii=False, indent=2))
    elif isinstance(value, list):
        for item in value:
            typer.echo(f"{item.get('id')}  {item.get('status')}  {item.get('name')}")
    elif isinstance(value, dict):
        typer.echo(f"{value.get('id', 'ok')}  {value.get('status', 'ok')}  {value.get('name', '')}".rstrip())


def _run_codex_tui(args: Sequence[str], *, no_lark: bool = False) -> None:
    if not no_lark and _uses_remote_resume_picker(args):
        raise typer.BadParameter(REMOTE_RESUME_PICKER_MESSAGE)

    settings = get_settings()
    descriptor: dict[str, object] | None = None
    try:
        if not no_lark:
            try:
                value = _daemon_request(
                    "POST",
                    "/interactive-sessions",
                    json_body={
                        "name": "interactive",
                        "cwd": os.getcwd(),
                        "sandbox": "workspace-write",
                    },
                )
            except typer.BadParameter:
                raise typer.BadParameter(
                    "Local Codex daemon is unavailable. Start it with `lark-bot daemon`, "
                    "or use `lark-bot codex --no-lark`."
                ) from None
            if not isinstance(value, dict):
                raise typer.BadParameter("Local Codex daemon returned an invalid interactive session.")
            descriptor = value
        exit_code = CodexTuiLauncher().run(
            CodexTuiOptions(
                args=list(args),
                codex_path=settings.codex_path,
                callback_command=[] if no_lark else [sys.executable, "-m", "lark_bot", "codex-hook"],
                remote_endpoint=(
                    str(descriptor["endpoint"]) if descriptor is not None else None
                ),
                remote_auth_token=(
                    str(descriptor["remote_auth_token"])
                    if descriptor is not None
                    else None
                ),
            )
        )
    except FileNotFoundError as error:
        raise typer.BadParameter(str(error)) from None
    finally:
        if descriptor is not None and "session_id" in descriptor:
            try:
                _daemon_request(
                    "DELETE",
                    f"/interactive-sessions/{descriptor['session_id']}",
                )
            except Exception:
                # The daemon also reaps sessions during shutdown; cleanup must
                # not replace the native TUI's exit status or original error.
                pass
    raise typer.Exit(exit_code)


def _run_claude_tui(args: Sequence[str], *, no_lark: bool = False) -> None:
    settings = get_settings()
    environment = disabled_hook_environment() if no_lark else None
    try:
        exit_code = ClaudeTuiLauncher().run(
            ClaudeTuiOptions(
                args=list(args),
                claude_path=getattr(settings, "claude_path", "claude"),
                env=environment,
            )
        )
    except FileNotFoundError as error:
        raise typer.BadParameter(str(error)) from None
    raise typer.Exit(exit_code)


@codex_app.callback()
def codex_command(
    ctx: typer.Context,
    no_lark: bool = typer.Option(False, "--no-lark", help="Launch Codex directly without the Lark gateway."),
) -> None:
    """Launch the native Codex TUI when no Lark Bot subcommand is selected."""

    if ctx.invoked_subcommand is None:
        _run_codex_tui(ctx.args, no_lark=no_lark)


@claude_app.callback()
def claude_command(
    ctx: typer.Context,
    no_lark: bool = typer.Option(
        False,
        "--no-lark",
        help="Launch Claude directly without Claude hook delivery.",
    ),
) -> None:
    """Launch the native Claude Code TUI when no subcommand is selected."""
    if ctx.invoked_subcommand is None:
        _run_claude_tui(ctx.args, no_lark=no_lark)


@job_app.command("start")
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


@job_app.command("list")
def codex_list(status: str | None = typer.Option(None, "--status"), json_output: bool = typer.Option(False, "--json")) -> None:
    query = f"?status={status}" if status else ""
    _emit_result(_daemon_request("GET", f"/sessions{query}"), json_output)


@job_app.command("show")
def codex_show(session_id: str, json_output: bool = typer.Option(False, "--json")) -> None:
    _emit_result(_daemon_request("GET", f"/sessions/{session_id}"), json_output)


@job_app.command("cancel")
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


@claude_job_app.command("start")
def claude_start(
    prompt: str | None = typer.Argument(None),
    name: str = typer.Option("task", "--name", "-n"),
    cwd: Path = typer.Option(Path("."), "--cwd"),
    model: str | None = typer.Option(None, "--model"),
    permission: str | None = typer.Option(None, "--permission", "--permission-mode"),
    sandbox: str = typer.Option("workspace-write", "--sandbox"),
    resume: str | None = typer.Option(None, "--resume"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    if sandbox not in {"read-only", "workspace-write"}:
        raise typer.BadParameter("sandbox must be read-only or workspace-write")
    text = sys.stdin.read() if prompt in {None, "-"} else prompt
    result = _agent_daemon_request(
        "claude",
        "POST",
        "/sessions",
        json_body={
            "name": name,
            "cwd": str(cwd.resolve()),
            "prompt": text,
            "model": model,
            "sandbox": sandbox,
            "permission_mode": permission,
            "resume_id": resume,
        },
    )
    _emit_result(result, json_output)


@claude_job_app.command("list")
def claude_list(
    status: str | None = typer.Option(None, "--status"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    query = f"?status={status}" if status else ""
    _emit_result(_agent_daemon_request("claude", "GET", f"/sessions{query}"), json_output)


@claude_job_app.command("show")
def claude_show(session_id: str, json_output: bool = typer.Option(False, "--json")) -> None:
    _emit_result(_agent_daemon_request("claude", "GET", f"/sessions/{session_id}"), json_output)


@claude_job_app.command("cancel")
def claude_cancel(session_id: str, json_output: bool = typer.Option(False, "--json")) -> None:
    _emit_result(
        _agent_daemon_request("claude", "POST", f"/sessions/{session_id}/cancel"),
        json_output,
    )


@claude_hooks_app.command("install")
def claude_hooks_install(project: Path = typer.Option(Path("."), "--project")) -> None:
    typer.echo(f"installed: {install_claude_hooks(project)}")


@claude_hooks_app.command("check")
def claude_hooks_check(project: Path = typer.Option(Path("."), "--project")) -> None:
    result = check_claude_hooks(project)
    typer.echo(result.status)
    if result.status != "installed":
        raise typer.Exit(1)


@claude_hooks_app.command("uninstall")
def claude_hooks_uninstall(project: Path = typer.Option(Path("."), "--project")) -> None:
    typer.echo(f"uninstalled: {uninstall_claude_hooks(project)}")


@app.command(
    "codex-hook",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def codex_hook(ctx: typer.Context) -> None:
    """Forward a Codex project hook without ever blocking Codex."""
    callback_argv = ["codex-hook", *ctx.args]
    raw = read_stdin_payload(callback_argv, sys.stdin.read)
    try:
        spool = get_settings().daemon_token_path.parent / "spool"
    except Exception:
        spool = Path(".lark-bot/spool")
    handle_callback(
        argv=callback_argv,
        stdin=raw,
        sender=_daemon_hook_request,
        spool_dir=spool,
    )
    forward_existing_notify(argv=callback_argv, stdin=raw)


def _daemon_agent_hook_request(agent: str, payload: dict[str, str]) -> None:
    settings = get_settings()
    token = ensure_daemon_token(settings.daemon_token_path)
    response = httpx.post(
        f"http://{settings.daemon_host}:{settings.daemon_port}/api/v1/agents/{agent}/hooks",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=min(settings.http_timeout_seconds, 0.25),
    )
    response.raise_for_status()


@app.command(
    "claude-hook",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def claude_hook(ctx: typer.Context) -> None:
    """Forward a Claude project hook without blocking Claude Code."""
    callback_argv = ["claude-hook", *ctx.args]
    raw = read_claude_stdin_payload(callback_argv, sys.stdin.read)
    try:
        spool = get_settings().daemon_token_path.parent / "spool"
    except Exception:
        spool = Path(".lark-bot/spool")
    handle_claude_callback(
        argv=callback_argv,
        stdin=raw,
        sender=lambda payload: _daemon_agent_hook_request("claude", payload),
        spool_dir=spool,
    )
