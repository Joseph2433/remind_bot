"""Response payload builders for Codex app-server requests."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any


def command_approval_response(allow: bool) -> dict[str, str]:
    return {"decision": "accept" if allow else "decline"}


def file_approval_response(allow: bool) -> dict[str, str]:
    return {"decision": "accept" if allow else "decline"}


def permission_response(params: Mapping[str, Any], allow: bool) -> dict[str, Any]:
    requested = params.get("permissions", {})
    if not isinstance(requested, Mapping):
        raise ValueError("permissions must be an object")
    return {
        "permissions": copy.deepcopy(dict(requested)) if allow else {},
        "scope": "turn",
        "strictAutoReview": False,
    }


def user_input_response(
    questions: Sequence[Mapping[str, Any]], answers: Mapping[str, str]
) -> dict[str, dict[str, dict[str, list[str]]]]:
    question_ids: list[str] = []
    for question in questions:
        question_id = question.get("id")
        if not isinstance(question_id, str) or not question_id:
            raise ValueError("each question must have a non-empty string id")
        if question_id in question_ids:
            raise ValueError(f"duplicate question id: {question_id}")
        question_ids.append(question_id)

    expected = set(question_ids)
    provided = set(answers)
    missing = expected - provided
    unknown = provided - expected
    if missing:
        raise ValueError(f"missing answers for question ids: {sorted(missing)}")
    if unknown:
        raise ValueError(f"unknown question ids: {sorted(unknown)}")
    if any(not isinstance(value, str) for value in answers.values()):
        raise ValueError("answers must be strings")

    return {
        "answers": {
            question_id: {"answers": [answers[question_id]]}
            for question_id in question_ids
        }
    }
