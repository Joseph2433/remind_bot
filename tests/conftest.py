from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def workspace_tmp_path() -> Iterator[Path]:
    root = (Path(__file__).resolve().parents[1] / "test-runtime").resolve()
    path = (root / f"pytest-{uuid.uuid4().hex}").resolve()
    if root not in path.parents:
        raise RuntimeError("test path escaped workspace test-runtime directory")
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        if path.exists() and root in path.resolve().parents:
            shutil.rmtree(path)
        try:
            root.rmdir()
        except OSError:
            pass
