from __future__ import annotations

import os
import secrets
from pathlib import Path


def ensure_daemon_token(path: str | Path) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise RuntimeError("daemon token file must not be a symlink")
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(secrets.token_urlsafe(32))
    try:
        os.chmod(target, 0o600)
        if os.name != "nt" and target.stat().st_mode & 0o077:
            raise RuntimeError("daemon token file permissions are insecure")
        token = target.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError("daemon token file is unreadable") from error
    if len(token) < 32:
        raise RuntimeError("daemon token file is empty or insecure")
    return token
