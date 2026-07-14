from __future__ import annotations

from collections.abc import Sequence


CODEX_GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    {
        "-c",
        "--config",
        "--enable",
        "--disable",
        "--remote",
        "--remote-auth-token-env",
        "-i",
        "--image",
        "-m",
        "--model",
        "--local-provider",
        "-p",
        "--profile",
        "-s",
        "--sandbox",
        "-C",
        "--cd",
        "--add-dir",
        "-a",
        "--ask-for-approval",
    }
)
CODEX_GLOBAL_FLAG_OPTIONS = frozenset(
    {
        "--oss",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--search",
        "--no-alt-screen",
        "--strict-config",
    }
)
CODEX_ATTACHED_VALUE_SHORT_PREFIXES = ("-c", "-i", "-m", "-p", "-s", "-C", "-a")


def uses_remote_resume_picker(args: Sequence[str]) -> bool:
    """Return whether resume would require the unsupported remote picker."""

    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            return False
        if token in CODEX_GLOBAL_FLAG_OPTIONS:
            index += 1
            continue

        option, separator, _ = token.partition("=")
        if separator and option in CODEX_GLOBAL_OPTIONS_WITH_VALUE:
            index += 1
            continue
        if any(
            len(token) > len(prefix) and token.startswith(prefix)
            for prefix in CODEX_ATTACHED_VALUE_SHORT_PREFIXES
        ):
            index += 1
            continue
        if token in CODEX_GLOBAL_OPTIONS_WITH_VALUE:
            if index + 1 >= len(args):
                return False
            index += 2
            continue
        if token.startswith("-") or token != "resume":
            return False

        resume_args = args[index:]
        if any(token in {"--last", "--last=true"} for token in resume_args[1:]):
            return False
        return len(resume_args) == 1 or resume_args[1].startswith("-")
    return False
