import sys

from lack_bot.runner import run_command


def test_run_command_captures_success_exit_code_and_tail():
    result = run_command(
        [sys.executable, "-c", "print('first'); print('second')"],
        name="success smoke",
        tail_lines=1,
    )

    assert result.name == "success smoke"
    assert result.exit_code == 0
    assert result.stdout_tail == ["second"]
    assert result.stderr_tail == []
    assert result.duration_seconds >= 0


def test_run_command_captures_failure_and_stderr_tail():
    result = run_command(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
        ],
        name="failure smoke",
        tail_lines=2,
    )

    assert result.exit_code == 3
    assert result.stdout_tail == ["out"]
    assert result.stderr_tail == ["err"]
