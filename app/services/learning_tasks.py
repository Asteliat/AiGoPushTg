"""Strict parser and runner for administrator-authored AtcivPerson learning tasks.

Only messages with the explicit ATCIV-LESSON/1 marker in an enabled, allowlisted
learning group are considered. No generic CAPTCHA/challenge recognition is used.
"""
from __future__ import annotations

import ast
import json
import operator
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.storage.db import SessionFactory
from app.storage.models import LearningEnrollment, LearningGroup, LearningResult

PREFIX = "ATCIV-LESSON/1"
_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


class LessonFormatError(ValueError):
    pass


def _math_value(expression: str) -> int | float:
    if len(expression) > 120:
        raise LessonFormatError("math expression too long")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise LessonFormatError("invalid math expression") from exc

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            if abs(node.value) > 10**12:
                raise LessonFormatError("number out of range")
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 8:
                raise LessonFormatError("exponent out of range")
            value = _ALLOWED_BINOPS[type(node.op)](left, right)
            if abs(value) > 10**12:
                raise LessonFormatError("result out of range")
            return value
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
            return _ALLOWED_UNARY[type(node.op)](visit(node.operand))
        raise LessonFormatError("unsupported math expression")

    try:
        result = visit(tree)
    except (ArithmeticError, OverflowError) as exc:
        raise LessonFormatError("math operation failed") from exc
    if isinstance(result, float) and result.is_integer():
        return int(result)
    return result


def _spoiler_contains(message, needle: str) -> bool:
    if not needle:
        return False
    text = message.message or ""
    # Telegram entity offsets/lengths are UTF-16 code units.
    encoded = text.encode("utf-16-le")
    for entity in getattr(message, "entities", None) or ():
        if type(entity).__name__ != "MessageEntitySpoiler":
            continue
        start = int(entity.offset) * 2
        end = (int(entity.offset) + int(entity.length)) * 2
        try:
            hidden = encoded[start:end].decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        if needle in hidden:
            return True
    return False


def parse_lesson(message) -> dict | None:
    body = (getattr(message, "message", None) or "").strip()
    if not body.startswith(PREFIX + "\n"):
        return None
    raw = body[len(PREFIX):].strip()
    if len(raw) > 4000:
        raise LessonFormatError("task payload too large")
    try:
        task = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise LessonFormatError("task payload must be JSON") from exc
    if not isinstance(task, dict):
        raise LessonFormatError("task payload must be an object")
    task_id = str(task.get("id", "")).strip()
    question = str(task.get("question", "")).strip()
    kind = str(task.get("type", "")).strip().lower()
    if not task_id or len(task_id) > 80 or not question or len(question) > 1000:
        raise LessonFormatError("id and question are required")
    if kind == "math":
        answer = _math_value(question)
    elif kind == "emoji":
        answer = str(task.get("answer", "")).strip()
        options = task.get("options")
        if not isinstance(options, list) or not 2 <= len(options) <= 12:
            raise LessonFormatError("emoji task requires 2-12 options")
        options = [str(item).strip() for item in options]
        if not answer or answer not in options or not _spoiler_contains(message, answer):
            raise LessonFormatError("emoji answer must be an option inside a Telegram spoiler")
    else:
        raise LessonFormatError("supported task types: math, emoji")
    return {"id": task_id, "type": kind, "question": question, "answer": str(answer)}


async def handle_lesson_message(event, account_id: int) -> bool:
    """Execute one explicitly marked task for an enrolled account; return if handled."""
    message = getattr(event, "message", None)
    if message is None:
        return False
    try:
        task = parse_lesson(message)
    except LessonFormatError:
        return False
    if task is None:
        return False

    group_id = int(event.chat_id or 0)
    message_id = int(message.id)
    started = datetime.now(timezone.utc)
    async with SessionFactory() as db:
        group = await db.scalar(select(LearningGroup).where(
            LearningGroup.chat_id == group_id, LearningGroup.enabled.is_(True)
        ))
        if group is None:
            return False
        enrollment = await db.scalar(select(LearningEnrollment).where(
            LearningEnrollment.group_id == group.id,
            LearningEnrollment.account_id == int(account_id),
            LearningEnrollment.enabled.is_(True),
        ))
        if enrollment is None:
            return False
        existing = await db.scalar(select(LearningResult.id).where(
            LearningResult.account_id == int(account_id),
            LearningResult.group_chat_id == group_id,
            LearningResult.message_id == message_id,
        ))
        if existing:
            return True
        row = LearningResult(
            account_id=int(account_id), group_chat_id=group_id,
            message_id=message_id, task_id=task["id"], task_type=task["type"],
            question=task["question"], answer=task["answer"], status="answering",
            started_at=started,
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return True
        result_id = row.id

    status, error = "answered", ""
    try:
        await message.reply(task["answer"])
    except Exception as exc:
        status, error = "failed", f"{type(exc).__name__}: {str(exc)[:300]}"
    finished = datetime.now(timezone.utc)
    async with SessionFactory() as db:
        row = await db.get(LearningResult, result_id)
        if row:
            row.status = status
            row.error = error
            row.completed_at = finished
            row.duration_ms = max(0, int((finished - started).total_seconds() * 1000))
            await db.commit()
    return True
