from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from lark_bot.codex.app_server import (
    ServerRequest,
    command_approval_response,
    file_approval_response,
    permission_response,
    user_input_response,
)
from lark_bot.codex.models import InteractionKind


def resolution(
    kind: InteractionKind,
    request: ServerRequest,
    allow: bool | None,
    answers: Mapping[str, str] | None,
) -> tuple[object, str]:
    if kind is InteractionKind.USER_INPUT:
        if answers is None:
            raise ValueError("answers are required for user input")
        questions = request.params.get("questions")
        if not isinstance(questions, Sequence) or isinstance(questions, (str, bytes)):
            raise ValueError("questions must be an array")
        return user_input_response(questions, answers), "submitted"
    if allow is None:
        raise ValueError("allow is required for approvals")
    if kind is InteractionKind.EXEC_APPROVAL:
        return command_approval_response(allow), "approved" if allow else "denied"
    if kind is InteractionKind.FILE_CHANGE_APPROVAL:
        return file_approval_response(allow), "accept" if allow else "decline"
    if kind is InteractionKind.PERMISSION_REQUEST:
        return permission_response(request.params, allow), "granted" if allow else "denied"
    raise ValueError(f"unsupported interaction kind: {kind}")


def terminal_decision(
    kind: InteractionKind,
    request: ServerRequest,
    result: object,
) -> str:
    if not isinstance(result, Mapping):
        raise ValueError("terminal response result must be an object")
    if kind is InteractionKind.USER_INPUT:
        validate_terminal_answers(request, result)
        return "submitted"
    if kind is InteractionKind.PERMISSION_REQUEST:
        permissions = result.get("permissions")
        if not isinstance(permissions, Mapping):
            raise ValueError("terminal permission response permissions must be an object")
        scope = result.get("scope")
        if scope is not None and not isinstance(scope, str):
            raise ValueError("terminal permission response scope must be a string")
        strict = result.get("strictAutoReview")
        if strict is not None and not isinstance(strict, bool):
            raise ValueError(
                "terminal permission response strictAutoReview must be a boolean"
            )
        return "granted" if permissions else "denied"
    decision = result.get("decision")
    if not isinstance(decision, str):
        raise ValueError("terminal approval response decision must be a string")
    if decision in {"accept", "acceptForSession"}:
        return "approved" if kind is InteractionKind.EXEC_APPROVAL else "accept"
    if decision in {"decline", "cancel"}:
        return "denied" if kind is InteractionKind.EXEC_APPROVAL else "decline"
    raise ValueError("unsupported terminal approval decision")


def validate_terminal_answers(
    request: ServerRequest,
    result: Mapping[str, Any],
) -> None:
    questions = request.params.get("questions")
    if not isinstance(questions, Sequence) or isinstance(questions, (str, bytes)):
        raise ValueError("questions must be an array")
    expected: set[str] = set()
    for question in questions:
        if not isinstance(question, Mapping):
            raise ValueError("each question must be an object")
        question_id = question.get("id")
        if not isinstance(question_id, str) or not question_id:
            raise ValueError("each question must have a non-empty string id")
        if question_id in expected:
            raise ValueError(f"duplicate question id: {question_id}")
        expected.add(question_id)
    answers = result.get("answers")
    if not isinstance(answers, Mapping):
        raise ValueError("terminal user input answers must be an object")
    if set(answers) != expected:
        raise ValueError("terminal user input answers do not match questions")
    for answer in answers.values():
        if not isinstance(answer, Mapping):
            raise ValueError("each terminal answers entry must be an object")
        values = answer.get("answers")
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise ValueError("each terminal answer answers must be an array")
        if any(not isinstance(value, str) for value in values):
            raise ValueError("terminal answer values must be strings")


def denial_response(kind: InteractionKind, params: Mapping[str, Any]) -> object:
    if kind is InteractionKind.EXEC_APPROVAL:
        return command_approval_response(False)
    if kind is InteractionKind.FILE_CHANGE_APPROVAL:
        return file_approval_response(False)
    if kind is InteractionKind.PERMISSION_REQUEST:
        return permission_response(params, False)
    raise ValueError("user input does not have a denial response")


def denial_decision(kind: InteractionKind) -> str:
    if kind is InteractionKind.EXEC_APPROVAL:
        return "denied"
    if kind is InteractionKind.FILE_CHANGE_APPROVAL:
        return "decline"
    if kind is InteractionKind.PERMISSION_REQUEST:
        return "denied"
    raise ValueError("user input has no denial decision")


def canonical_request_id(value: int | str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
