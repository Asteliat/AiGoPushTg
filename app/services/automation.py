"""Durable Telegram campaign queue with scheduling and FloodWait handling.

Recipients are selected explicitly by the operator. Telegram privacy, paid-message
requirements, account restrictions and permissions remain authoritative.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, or_, select
from telethon import utils

from app.settings import get_settings
from app.storage.db import SessionFactory
from app.storage.models import (
    Account,
    AppSetting,
    AutomationConsent,
    AutomationSchedule,
    ScheduledReaction,
    AutomationOperationEvent,
    AutomationTask,
    AutomationTaskRecipient,
    utcnow,
)

ALLOWED_KINDS = {"broadcast", "invite", "direct_invite", "group_broadcast"}
MAX_RECIPIENTS = 500
MAX_TEXT = 4096


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _has_invite_permission(permissions) -> bool:
    """Check Telegram's invite_users right, not merely the admin role."""
    if getattr(permissions, "is_creator", False):
        return True
    if not getattr(permissions, "is_admin", False):
        return False
    invite_users = getattr(permissions, "invite_users", None)
    if invite_users is not None:
        return bool(invite_users)
    admin_rights = getattr(permissions, "admin_rights", None)
    return bool(getattr(admin_rights, "invite_users", False)) if admin_rights is not None else False


async def _resolve_campaign_peer(client, peer_id: int, peer_ref: str = ""):
    """Resolve a selected peer and verify its username still maps to the saved ID.

    The temporary session snapshot may not retain entities discovered by an earlier
    temporary client. Resolving the saved username keeps access hashes available, while
    the ID comparison prevents a reassigned username from receiving the campaign.
    """
    if peer_ref:
        entity = await client.get_entity(peer_ref)
        if int(utils.get_peer_id(entity)) != int(peer_id):
            raise ValueError(
                "Username получателя изменился и теперь указывает на другой аккаунт. "
                "Повторно выберите пользователя по актуальному username."
            )
        return entity
    return await client.get_entity(int(peer_id))


def _validate_payload(kind: str, payload: dict) -> dict:
    if kind not in ALLOWED_KINDS:
        raise ValueError("Неизвестный тип кампании.")
    normalized = {
        "text": str(payload.get("text") or "").strip(),
        "file_path": str(payload.get("file_path") or "").strip(),
        "caption": str(payload.get("caption") or "").strip(),
    }
    if kind == "direct_invite":
        target_group_id = int(payload.get("target_group_id") or 0)
        if not target_group_id:
            raise ValueError("Для прямого приглашения выберите целевую супергруппу.")
        normalized.update({
            "target_group_id": target_group_id,
            "target_group_ref": str(payload.get("target_group_ref") or "").strip(),
            "target_group_label": str(payload.get("target_group_label") or "").strip(),
        })
        return normalized
    if len(normalized["text"]) > MAX_TEXT or len(normalized["caption"]) > MAX_TEXT:
        raise ValueError("Текст сообщения не должен превышать 4096 символов.")
    if not normalized["text"] and not normalized["file_path"]:
        raise ValueError("Добавьте текст или файл.")
    if kind == "invite" and not re.search(r"(?:https?://)?t\.me/", normalized["text"] or normalized["caption"], re.I):
        raise ValueError("Для приглашения укажите текст и ссылку-приглашение t.me.")
    if normalized["file_path"]:
        root = (Path(get_settings().data_dir) / "automation_uploads").resolve()
        candidate = Path(normalized["file_path"]).resolve()
        if root not in candidate.parents or not candidate.is_file():
            raise ValueError("Файл должен находиться в /data/automation_uploads.")
        normalized["file_path"] = str(candidate)
    return normalized


async def create_subscription_task(
    account_id: int, links: list[str], *, action: str,
    scheduled_at: datetime | None = None,
) -> int:
    """Create a durable one-shot join/leave queue from explicitly supplied links."""
    if action not in {"join", "leave"}:
        raise ValueError("Действие должно быть join или leave.")
    normalized = list(dict.fromkeys(str(value).strip() for value in links if str(value).strip()))
    if not normalized or len(normalized) > MAX_RECIPIENTS:
        raise ValueError(f"Укажите от 1 до {MAX_RECIPIENTS} ссылок.")
    due = _aware(scheduled_at or utcnow())
    if due < utcnow() - timedelta(minutes=1):
        raise ValueError("Время запуска уже прошло.")
    policy = await module_policy("subscription")
    if len(normalized) > policy["per_account_limit"]:
        raise ValueError(
            f"Глобальный лимит подписок: не более {policy['per_account_limit']} ссылок "
            "на один аккаунт в одной задаче."
        )
    async with SessionFactory() as db:
        account = await db.get(Account, account_id)
        if not account or not account.enabled:
            raise ValueError("Аккаунт не найден или отключён.")
        task = AutomationTask(
            account_id=account_id,
            kind="subscription_join" if action == "join" else "subscription_leave",
            payload=json.dumps({"action": action}, ensure_ascii=False),
            status="queued", scheduled_at=due,
        )
        db.add(task)
        await db.flush()
        for link in normalized:
            peer_id = int.from_bytes(hashlib.sha256(link.casefold().encode("utf-8")).digest()[:8], "big")
            peer_id &= (1 << 63) - 1
            db.add(AutomationTaskRecipient(
                task_id=task.id, peer_id=peer_id or 1,
                peer_ref=link, peer_label=link,
            ))
        await db.commit()
        return int(task.id)


async def create_campaign(
    account_id: int, *, kind: str, recipients: list[int],
    payload: dict, scheduled_at: datetime | None = None,
    recipient_refs: dict[int, str] | None = None,
    recipient_labels: dict[int, str] | None = None,
) -> int:
    normalized = _validate_payload(kind, payload)
    unique_ids = list(dict.fromkeys(int(peer_id) for peer_id in recipients))
    if not unique_ids or len(unique_ids) > MAX_RECIPIENTS:
        raise ValueError(f"Выберите от 1 до {MAX_RECIPIENTS} получателей.")
    module = _module_name_for_kind(kind)
    policy = await module_policy(module)
    if len(unique_ids) > policy["per_account_limit"]:
        raise ValueError(
            f"Глобальный лимит модуля «{module}»: не более {policy['per_account_limit']} "
            "получателей на один аккаунт в одной задаче."
        )
    due = _aware(scheduled_at or utcnow())
    if due < utcnow() - timedelta(minutes=1):
        raise ValueError("Время запуска уже прошло.")
    async with SessionFactory() as db:
        account = await db.get(Account, account_id)
        if not account or not account.enabled:
            raise ValueError("Аккаунт не найден или отключён.")
        refs, labels = recipient_refs or {}, recipient_labels or {}
        by_id = {
            peer_id: {
                "peer_ref": str(refs.get(peer_id) or "").strip(),
                "peer_label": str(labels.get(peer_id) or refs.get(peer_id) or peer_id).strip(),
            }
            for peer_id in unique_ids
        }
        if kind != "group_broadcast":
            missing_refs = [peer_id for peer_id, item in by_id.items() if not item["peer_ref"]]
            if missing_refs:
                raise ValueError(
                    f"Для {len(missing_refs)} получателей не сохранён username. "
                    "Повторно выберите пользователей по @username."
                )
        task = AutomationTask(
            account_id=account_id, kind=kind,
            payload=json.dumps(normalized, ensure_ascii=False),
            status="queued", scheduled_at=due,
        )
        db.add(task)
        await db.flush()
        for peer_id in unique_ids:
            recipient = by_id[peer_id]
            db.add(AutomationTaskRecipient(
                task_id=task.id, peer_id=peer_id,
                peer_ref=recipient["peer_ref"],
                peer_label=recipient["peer_label"],
            ))
        await db.commit()
        return int(task.id)


async def _cleanup_campaign_upload(account_id: int, payload: dict) -> None:
    """Delete an uploaded campaign file only after no queued task/schedule can reuse it."""
    raw_path = str(payload.get("file_path") or "").strip()
    if not raw_path:
        return
    try:
        candidate = Path(raw_path).resolve()
        upload_root = (Path(get_settings().data_dir) / "automation_uploads").resolve()
        if upload_root not in candidate.parents or not candidate.is_file():
            return
        async with SessionFactory() as db:
            active_schedules = await db.scalar(select(func.count()).select_from(
                AutomationSchedule
            ).where(
                AutomationSchedule.status.in_(["active", "paused"]),
                AutomationSchedule.payload.contains(str(candidate)),
            ))
            active_tasks = await db.scalar(select(func.count(func.distinct(
                AutomationTask.id
            ))).select_from(AutomationTask).join(
                AutomationTaskRecipient,
                AutomationTaskRecipient.task_id == AutomationTask.id,
            ).where(
                AutomationTask.payload.contains(str(candidate)),
                or_(
                    AutomationTask.status.in_(["queued", "running", "paused", "needs_review"]),
                    (AutomationTask.status == "failed") & AutomationTaskRecipient.status.in_(
                        ["pending", "failed", "uncertain"]
                    ),
                    (AutomationTask.status == "cancelled") & (AutomationTaskRecipient.status == "sending"),
                ),
            ))
        if not int(active_schedules or 0) and not int(active_tasks or 0):
            candidate.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        logging.exception("Could not safely clean automation upload %s", raw_path)


async def set_campaign_status(task_id: int, status: str) -> None:
    if status not in {"paused", "cancelled", "queued"}:
        raise ValueError("Недопустимый статус задачи.")
    payload = None
    account_id = None
    async with SessionFactory() as db:
        task = await db.get(AutomationTask, task_id)
        if not task:
            raise ValueError("Задача не найдена.")
        if task.status in {"completed", "failed", "cancelled"}:
            raise ValueError("Задача уже завершена.")
        task.status = status
        task.updated_at = utcnow()
        if status == "cancelled":
            payload = json.loads(task.payload)
            account_id = task.account_id
        await db.commit()
    if payload is not None and account_id is not None:
        await _cleanup_campaign_upload(account_id, payload)


async def reset_account_limits(account_id: int) -> None:
    async with SessionFactory() as db:
        if not await db.get(Account, account_id):
            raise ValueError("Аккаунт не найден.")
        rows = list((await db.scalars(select(AppSetting).where(
            AppSetting.key.startswith(f"automation.{account_id}.")
        ))).all())
        for row in rows:
            await db.delete(row)
        await db.commit()


async def retry_failed_campaign(task_id: int) -> int:
    async with SessionFactory() as db:
        task = await db.get(AutomationTask, task_id)
        if not task or task.status in {"cancelled", "paused", "running"}:
            raise ValueError("Задачу нельзя повторить в текущем статусе.")
        retryable = list((await db.scalars(select(AutomationTaskRecipient).where(
            AutomationTaskRecipient.task_id == task_id,
            AutomationTaskRecipient.status == "failed",
        ))).all())
        pending_count = await db.scalar(select(func.count()).select_from(
            AutomationTaskRecipient
        ).where(
            AutomationTaskRecipient.task_id == task_id,
            AutomationTaskRecipient.status == "pending",
        ))
        if not retryable and not int(pending_count or 0):
            raise ValueError("В задаче нет неотправленных получателей для повтора.")
        for recipient in retryable:
            recipient.status = "pending"
            recipient.last_error = ""
        task.status = "queued"
        task.scheduled_at = utcnow()
        task.completed_at = None
        task.last_error = ""
        task.updated_at = utcnow()
        await db.commit()
        return len(retryable) + int(pending_count or 0)


async def campaign_status(task_id: int) -> dict:
    async with SessionFactory() as db:
        task = await db.get(AutomationTask, task_id)
        if not task:
            raise ValueError("Задача не найдена.")
        counts = dict((await db.execute(
            select(AutomationTaskRecipient.status, func.count())
            .where(AutomationTaskRecipient.task_id == task_id)
            .group_by(AutomationTaskRecipient.status)
        )).all())
        delivery_rows = list((await db.scalars(
            select(AutomationTaskRecipient)
            .where(AutomationTaskRecipient.task_id == task_id)
            .order_by(AutomationTaskRecipient.id)
            .limit(100)
        )).all())
        payload = json.loads(task.payload)
        return {
            "id": task.id, "account_id": task.account_id, "kind": task.kind, "status": task.status,
            "scheduled_at": _aware(task.scheduled_at).isoformat(),
            "target_group_label": str(payload.get("target_group_label") or ""),
            "last_error": task.last_error, "recipients": counts,
            "delivery_log": [{
                "peer_id": int(row.peer_id),
                "peer_label": row.peer_label or row.peer_ref or str(row.peer_id),
                "peer_ref": row.peer_ref, "status": row.status,
                "attempts": row.attempts, "last_error": row.last_error,
                "delivered_at": _aware(row.delivered_at).isoformat() if row.delivered_at else "",
            } for row in delivery_rows],
        }


async def list_campaigns(account_id: int, *, limit: int = 10) -> list[dict]:
    async with SessionFactory() as db:
        rows = list((await db.scalars(
            select(AutomationTask).where(
                AutomationTask.account_id == account_id
            ).order_by(AutomationTask.id.desc()).limit(max(1, min(limit, 25)))
        )).all())
        return [{
            "id": row.id, "kind": row.kind, "status": row.status,
            "scheduled_at": _aware(row.scheduled_at).isoformat(),
            "last_error": row.last_error,
        } for row in rows]


async def _account_limits(account_id: int, operation: str = "broadcast") -> tuple[int, int, str | None, str | None]:
    async with SessionFactory() as db:
        rows = list((await db.scalars(select(AppSetting).where(
            AppSetting.key.in_([
                "automation.default.interval_seconds",
                "automation.default.daily_limit",
                "automation.default.quiet_start_utc",
                "automation.default.quiet_end_utc",
                f"automation.{account_id}.interval_seconds",
                f"automation.{account_id}.daily_limit",
                f"automation.{account_id}.quiet_start_utc",
                f"automation.{account_id}.quiet_end_utc",
                f"automation.default.{operation}.interval_seconds",
                f"automation.default.{operation}.daily_limit",
                f"automation.{account_id}.{operation}.interval_seconds",
                f"automation.{account_id}.{operation}.daily_limit",
                f"automation.default.{operation}.quiet_start_utc",
                f"automation.default.{operation}.quiet_end_utc",
                f"automation.{account_id}.{operation}.quiet_start_utc",
                f"automation.{account_id}.{operation}.quiet_end_utc",
            ])
        ))).all())
    values = {row.key: row.value for row in rows}
    default_interval = max(1, min(3600, int(values.get("automation.default.interval_seconds", "30"))))
    default_daily = max(1, min(500, int(values.get("automation.default.daily_limit", "50"))))
    operation_interval = max(1, min(3600, int(values.get(f"automation.default.{operation}.interval_seconds", str(default_interval)))))
    operation_daily = max(1, min(500, int(values.get(f"automation.default.{operation}.daily_limit", str(default_daily)))))
    interval = max(1, min(3600, int(values.get(f"automation.{account_id}.{operation}.interval_seconds", values.get(f"automation.{account_id}.interval_seconds", str(operation_interval))))))
    daily = max(1, min(500, int(values.get(f"automation.{account_id}.{operation}.daily_limit", values.get(f"automation.{account_id}.daily_limit", str(operation_daily))))))
    quiet_start = values.get(
        f"automation.{account_id}.{operation}.quiet_start_utc",
        values.get(f"automation.{account_id}.quiet_start_utc",
                   values.get(f"automation.default.{operation}.quiet_start_utc",
                              values.get("automation.default.quiet_start_utc", ""))),
    ) or None
    quiet_end = values.get(
        f"automation.{account_id}.{operation}.quiet_end_utc",
        values.get(f"automation.{account_id}.quiet_end_utc",
                   values.get(f"automation.default.{operation}.quiet_end_utc",
                              values.get("automation.default.quiet_end_utc", ""))),
    ) or None
    return interval, daily, quiet_start, quiet_end


def _normalize_quiet_hours(start: str | None, end: str | None) -> tuple[str, str]:
    start_value = (start or "").strip()
    end_value = (end or "").strip()
    if start_value in {"", "-"} and end_value in {"", "-"}:
        return "", ""
    try:
        time.fromisoformat(start_value)
        time.fromisoformat(end_value)
    except ValueError as exc:
        raise ValueError("Тихие часы укажите как HH:MM UTC или поставьте - в обоих полях.") from exc
    if len(start_value) != 5 or len(end_value) != 5:
        raise ValueError("Тихие часы укажите в формате HH:MM UTC.")
    return start_value, end_value


def _quiet_until(now: datetime, start: str | None, end: str | None) -> datetime | None:
    if not start or not end:
        return None
    start_time = time.fromisoformat(start)
    end_time = time.fromisoformat(end)
    current_time = now.time().replace(tzinfo=None)
    if start_time == end_time:
        return None
    active = (
        start_time <= current_time < end_time
        if start_time < end_time
        else current_time >= start_time or current_time < end_time
    )
    if not active:
        return None
    target = now.replace(hour=end_time.hour, minute=end_time.minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


async def set_global_automation_limits(
    *, interval_seconds: int, daily_limit: int,
    quiet_start_utc: str | None = None, quiet_end_utc: str | None = None,
) -> None:
    if not 1 <= int(interval_seconds) <= 3600:
        raise ValueError("Интервал должен быть от 1 до 3600 секунд.")
    if not 1 <= int(daily_limit) <= 500:
        raise ValueError("Дневной лимит должен быть от 1 до 500 сообщений.")
    quiet_start, quiet_end = _normalize_quiet_hours(quiet_start_utc, quiet_end_utc)
    async with SessionFactory() as db:
        for key, value in (
            ("automation.default.interval_seconds", str(int(interval_seconds))),
            ("automation.default.daily_limit", str(int(daily_limit))),
            ("automation.default.quiet_start_utc", quiet_start),
            ("automation.default.quiet_end_utc", quiet_end),
        ):
            row = await db.get(AppSetting, key)
            if row:
                row.value = value
            else:
                db.add(AppSetting(key=key, value=value))
        await db.commit()


async def set_account_limits(
    account_id: int, *, interval_seconds: int, daily_limit: int,
    quiet_start_utc: str | None = None, quiet_end_utc: str | None = None,
) -> None:
    if not 1 <= int(interval_seconds) <= 3600:
        raise ValueError("Интервал должен быть от 1 до 3600 секунд.")
    if not 1 <= int(daily_limit) <= 500:
        raise ValueError("Дневной лимит должен быть от 1 до 500 сообщений.")
    quiet_start, quiet_end = _normalize_quiet_hours(quiet_start_utc, quiet_end_utc)
    async with SessionFactory() as db:
        if not await db.get(Account, account_id):
            raise ValueError("Аккаунт не найден.")
        for key, value in (
            (f"automation.{account_id}.interval_seconds", str(int(interval_seconds))),
            (f"automation.{account_id}.daily_limit", str(int(daily_limit))),
            (f"automation.{account_id}.quiet_start_utc", quiet_start),
            (f"automation.{account_id}.quiet_end_utc", quiet_end),
        ):
            row = await db.get(AppSetting, key)
            if row:
                row.value = value
            else:
                db.add(AppSetting(key=key, value=value))
        await db.commit()


async def _sleep_with_task_heartbeat(task_id: int, seconds: int) -> None:
    remaining = max(0, int(seconds))
    while remaining:
        step = min(15, remaining)
        await asyncio.sleep(step)
        remaining -= step
        async with SessionFactory() as db:
            task = await db.get(AutomationTask, task_id)
            if not task or task.status != "running":
                return
            task.started_at = utcnow()
            task.updated_at = utcnow()
            await db.commit()


async def _maintain_campaign_lease(task_id: int) -> None:
    """Refresh the durable task lease while a Telegram request is in flight."""
    while True:
        await asyncio.sleep(20)
        try:
            async with SessionFactory() as db:
                task = await db.get(AutomationTask, task_id)
                if not task or task.status != "running":
                    return
                task.started_at = utcnow()
                task.updated_at = utcnow()
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Could not refresh automation task lease: task=%s", task_id)


async def process_due_campaigns(*, max_tasks: int = 1) -> int:
    """Process due campaigns. Called repeatedly by the dedicated worker loop."""
    from telethon.errors import FloodWaitError
    from app.telegram_gateway.management import isolated_account_client

    processed = 0
    for _ in range(max(1, min(max_tasks, 5))):
        async with SessionFactory() as db:
            now = utcnow()
            task = await db.scalar(
                select(AutomationTask)
                .where(
                    or_(
                        (AutomationTask.status == "queued") & (AutomationTask.scheduled_at <= now),
                        (AutomationTask.status == "running") & (
                            AutomationTask.started_at.is_(None)
                            | (AutomationTask.started_at <= now - timedelta(minutes=10))
                        ),
                    ),
                )
                .order_by(AutomationTask.scheduled_at, AutomationTask.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not task:
                break
            task.status = "running"
            task.started_at = now
            task.updated_at = now
            await db.commit()
            task_id, account_id, kind, payload_raw = task.id, task.account_id, task.kind, task.payload
            stale_sending = list((await db.scalars(select(AutomationTaskRecipient).where(
                AutomationTaskRecipient.task_id == task_id,
                AutomationTaskRecipient.status == "sending",
            ))).all())
            for stale_item in stale_sending:
                # Telegram may have accepted the message before the process lost its ACK.
                # Never retry an ambiguous delivery automatically.
                stale_item.status = "uncertain"
                stale_item.last_error = "Результат неизвестен после перезапуска: проверьте Telegram перед ручным повтором."
            await db.commit()

        heartbeat_task = asyncio.create_task(_maintain_campaign_lease(task_id))
        try:
            payload = json.loads(payload_raw)
            module = _module_name_for_kind(kind)
            policy = await module_policy(module)
            interval, daily_limit = policy["interval_seconds"], policy["daily_account_limit"]
            quiet_keys = [
                f"automation.default.{module}.quiet_start_utc",
                f"automation.default.{module}.quiet_end_utc",
            ]
            async with SessionFactory() as policy_db:
                quiet_values = {row.key: row.value for row in (await policy_db.scalars(
                    select(AppSetting).where(AppSetting.key.in_(quiet_keys))
                )).all()}
            quiet_start = quiet_values.get(quiet_keys[0], "") or None
            quiet_end = quiet_values.get(quiet_keys[1], "") or None
            quarantine = await module_quarantine_status(account_id, module)
            if quarantine["quarantined"]:
                async with SessionFactory() as db:
                    current = await db.get(AutomationTask, task_id)
                    if current and current.status == "running":
                        current.status = "paused"
                        current.last_error = f"Карантин модуля {module}: {quarantine['reason']}"
                        current.updated_at = utcnow()
                        await db.commit()
                continue
            async with isolated_account_client(account_id) as (_account, client):
                while True:
                    async with SessionFactory() as db:
                        current = await db.get(AutomationTask, task_id)
                        if not current or current.status != "running":
                            break
                        recipient = await db.scalar(
                            select(AutomationTaskRecipient)
                            .where(
                                AutomationTaskRecipient.task_id == task_id,
                                AutomationTaskRecipient.status == "pending",
                            )
                            .order_by(AutomationTaskRecipient.id)
                            .with_for_update(skip_locked=True)
                            .limit(1)
                        )
                        if not recipient:
                            uncertain_count = await db.scalar(select(func.count()).select_from(AutomationTaskRecipient).where(
                                AutomationTaskRecipient.task_id == task_id,
                                AutomationTaskRecipient.status == "uncertain",
                            ))
                            failed_count = await db.scalar(select(func.count()).select_from(AutomationTaskRecipient).where(
                                AutomationTaskRecipient.task_id == task_id,
                                AutomationTaskRecipient.status == "failed",
                            ))
                            current.status = (
                                "needs_review" if int(uncertain_count or 0)
                                else ("failed" if int(failed_count or 0) else "completed")
                            )
                            current.completed_at = utcnow()
                            current.updated_at = utcnow()
                            await db.commit()
                            break
                        quiet_until = _quiet_until(utcnow(), quiet_start, quiet_end)
                        if quiet_until:
                            current.status = "queued"
                            current.scheduled_at = quiet_until
                            current.last_error = f"Quiet hours until {quiet_until.isoformat()} UTC."
                            recipient.status = "pending"
                            await db.commit()
                            break
                        sent_query = select(func.count()).select_from(
                            AutomationTaskRecipient
                        ).join(AutomationTask).where(
                            AutomationTask.account_id == account_id,
                            AutomationTaskRecipient.status == "sent",
                            AutomationTaskRecipient.delivered_at >= now - timedelta(hours=24),
                        )
                        if kind in {"invite", "direct_invite"}:
                            sent_query = sent_query.where(AutomationTask.kind.in_(["invite", "direct_invite"]))
                        else:
                            sent_query = sent_query.where(AutomationTask.kind.in_(["broadcast", "group_broadcast"]))
                        sent_today = await db.scalar(sent_query)
                        if int(sent_today or 0) >= daily_limit:
                            current.status = "queued"
                            current.scheduled_at = now + timedelta(hours=24)
                            current.last_error = "Account daily automation limit reached."
                            recipient.status = "pending"
                            await db.commit()
                            break
                        peer_id = int(recipient.peer_id)
                        peer_ref = str(recipient.peer_ref or "")
                        recipient.status = "sending"
                        recipient.attempts += 1
                        current.started_at = utcnow()
                        current.updated_at = utcnow()
                        await db.commit()

                    try:
                        if kind == "direct_invite":
                            from telethon.errors import UserAlreadyParticipantError
                            from telethon.tl.functions.channels import InviteToChannelRequest

                            target_group = await _resolve_campaign_peer(
                                client, int(payload["target_group_id"]),
                                str(payload.get("target_group_ref") or ""),
                            )
                            if not getattr(target_group, "megagroup", False):
                                raise ValueError("Прямое приглашение поддерживается только в супергруппы.")
                            permissions = await client.get_permissions(target_group, "me")
                            if not _has_invite_permission(permissions):
                                raise PermissionError(
                                    "У аккаунта нет права приглашать участников (invite_users) в целевую группу."
                                )
                            user = await _resolve_campaign_peer(client, peer_id, peer_ref)
                            try:
                                await client(InviteToChannelRequest(channel=target_group, users=[user]))
                            except UserAlreadyParticipantError:
                                pass
                        else:
                            entity = await _resolve_campaign_peer(client, peer_id, recipient.peer_ref)
                            if kind == "group_broadcast":
                                permissions = await client.get_permissions(entity, "me")
                                if not (getattr(permissions, "is_admin", False) or getattr(permissions, "is_creator", False)):
                                    raise PermissionError("Аккаунт больше не является администратором этой группы.")
                            if payload.get("file_path"):
                                await client.send_file(
                                    entity, payload["file_path"],
                                    caption=payload.get("caption") or payload.get("text") or None,
                                )
                            else:
                                await client.send_message(entity, payload["text"])
                    except FloodWaitError as exc:
                        seconds = max(1, int(exc.seconds))
                        flood_state = await record_module_floodwait(account_id, module, seconds)
                        async with SessionFactory() as db:
                            current = await db.get(AutomationTask, task_id)
                            item = await db.scalar(select(AutomationTaskRecipient).where(
                                AutomationTaskRecipient.task_id == task_id,
                                AutomationTaskRecipient.peer_id == peer_id,
                            ))
                            if item:
                                item.status = "pending"
                                item.last_error = f"FloodWait: {seconds}s"
                            if current and current.status == "running":
                                current.status = "paused" if flood_state["quarantined"] else "queued"
                                current.scheduled_at = utcnow() + timedelta(seconds=seconds)
                                current.last_error = (
                                    f"Карантин {module}: FloodWait {seconds}s "
                                    f"({flood_state['count']}/{flood_state['threshold']})"
                                    if flood_state["quarantined"] else f"Telegram FloodWait: {seconds}s"
                                )
                            await db.commit()
                        break
                    except Exception as exc:
                        async with SessionFactory() as db:
                            item = await db.scalar(select(AutomationTaskRecipient).where(
                                AutomationTaskRecipient.task_id == task_id,
                                AutomationTaskRecipient.peer_id == peer_id,
                            ))
                            if item:
                                # Transport errors may occur after Telegram accepted a message.
                                uncertain = isinstance(exc, (TimeoutError, ConnectionError, OSError))
                                item.status = "uncertain" if uncertain else "failed"
                                item.last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
                            stop_direct_invite = False
                            if isinstance(exc, PermissionError):
                                try:
                                    schedule_id = int(json.loads(payload_raw).get("_schedule_id") or 0)
                                except (ValueError, TypeError):
                                    schedule_id = 0
                                if schedule_id:
                                    schedule = await db.get(AutomationSchedule, schedule_id)
                                    if schedule and schedule.status == "active":
                                        schedule.status = "paused"
                                        schedule.last_error = f"Расписание приостановлено: {str(exc)[:500]}"
                                        schedule.updated_at = utcnow()
                                if kind == "direct_invite":
                                    current = await db.get(AutomationTask, task_id)
                                    if current and current.status == "running":
                                        current.status = "failed"
                                        current.completed_at = utcnow()
                                        current.last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
                                    stop_direct_invite = True
                            await db.commit()
                        if stop_direct_invite:
                            break
                    else:
                        async with SessionFactory() as db:
                            item = await db.scalar(select(AutomationTaskRecipient).where(
                                AutomationTaskRecipient.task_id == task_id,
                                AutomationTaskRecipient.peer_id == peer_id,
                            ))
                            if item:
                                item.status = "sent"
                                item.delivered_at = utcnow()
                                item.last_error = ""
                            await db.commit()
                    await _sleep_with_task_heartbeat(task_id, interval)
        except Exception as exc:
            async with SessionFactory() as db:
                current = await db.get(AutomationTask, task_id)
                if current and current.status == "running":
                    current.status = "failed"
                    current.last_error = f"{type(exc).__name__}: {str(exc)[:1000]}"
                    current.updated_at = utcnow()
                    await db.commit()
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

        async with SessionFactory() as db:
            final_task = await db.get(AutomationTask, task_id)
            unfinished_count = await db.scalar(select(func.count()).select_from(
                AutomationTaskRecipient
            ).where(
                AutomationTaskRecipient.task_id == task_id,
                AutomationTaskRecipient.status.in_(["pending", "sending", "failed"]),
            ))
            cleanup_payload = None
            cleanup_account_id = None
            if final_task and (
                final_task.status == "cancelled"
                or (final_task.status in {"completed", "failed"} and int(unfinished_count or 0) == 0)
            ):
                cleanup_payload = json.loads(final_task.payload)
                cleanup_account_id = final_task.account_id
        if cleanup_payload is not None and cleanup_account_id is not None:
            await _cleanup_campaign_upload(cleanup_account_id, cleanup_payload)
        processed += 1
    return processed


__all__ = [
    "campaign_status", "create_campaign", "create_subscription_task", "list_campaigns",
    "process_due_campaigns", "reset_account_limits", "retry_failed_campaign", "set_account_limits",
    "set_global_automation_limits", "set_operation_limits", "operation_limits",
    "create_schedule", "list_schedules", "set_schedule_status", "dispatch_due_schedules",
    "resolve_uncertain_recipient", "schedule_reaction", "list_scheduled_reactions",
    "cancel_scheduled_reaction", "resolve_scheduled_reaction", "process_due_reactions", "reserve_operation_slot",
    "OperationRateLimitError", "module_policy", "set_module_policy", "record_module_floodwait",
    "module_quarantine_status", "clear_module_quarantine",
    "set_campaign_status",
]



MODULE_POLICY_DEFAULTS = {
    "broadcast": {"interval_seconds": 30, "daily_limit": 50, "batch_size": 10, "per_account_limit": 50, "daily_account_limit": 50, "floodwait_wait_seconds": 9999, "floodwait_threshold": 10},
    "invite": {"interval_seconds": 120, "daily_limit": 25, "batch_size": 1, "per_account_limit": 99, "daily_account_limit": 25, "floodwait_wait_seconds": 9999, "floodwait_threshold": 10},
    "subscription": {"interval_seconds": 10, "daily_limit": 20, "batch_size": 1, "per_account_limit": 20, "daily_account_limit": 20, "floodwait_wait_seconds": 9999, "floodwait_threshold": 10},
    "reaction": {"interval_seconds": 30, "daily_limit": 50, "batch_size": 1, "per_account_limit": 50, "daily_account_limit": 50, "floodwait_wait_seconds": 9999, "floodwait_threshold": 10},
}


def _module_name_for_kind(kind: str) -> str:
    if kind in {"invite", "direct_invite"}:
        return "invite"
    if kind in {"broadcast", "group_broadcast"}:
        return "broadcast"
    if kind in {"subscription", "join", "leave", "subscription_join", "subscription_leave"}:
        return "subscription"
    if kind == "reaction":
        return "reaction"
    raise ValueError("Неизвестный модуль автоматизации.")


async def module_policy(module: str) -> dict:
    if module not in MODULE_POLICY_DEFAULTS:
        raise ValueError("Неизвестный модуль.")
    defaults = MODULE_POLICY_DEFAULTS[module]
    keys = [f"automation.policy.{module}.{name}" for name in defaults]
    async with SessionFactory() as db:
        rows = (await db.scalars(select(AppSetting).where(AppSetting.key.in_(keys)))).all()
    values = {row.key.rsplit(".", 1)[-1]: row.value for row in rows}
    result = dict(defaults)
    for name, default in defaults.items():
        try:
            result[name] = int(values.get(name, default))
        except (TypeError, ValueError):
            result[name] = default
    for name in ("interval_seconds", "daily_limit", "batch_size", "per_account_limit", "daily_account_limit", "floodwait_wait_seconds", "floodwait_threshold"):
        result[name] = max(1, min(9999 if name != "floodwait_threshold" else 100, result[name]))
    return result


async def set_module_policy(module: str, **values: int) -> dict:
    if module not in MODULE_POLICY_DEFAULTS:
        raise ValueError("Неизвестный модуль.")
    allowed = set(MODULE_POLICY_DEFAULTS[module])
    if not values or set(values) - allowed:
        raise ValueError("Переданы неизвестные параметры настройки.")
    current = await module_policy(module)
    for name, raw in values.items():
        value = int(raw)
        upper = 100 if name == "floodwait_threshold" else 9999
        if not 1 <= value <= upper:
            raise ValueError(f"{name}: значение должно быть от 1 до {upper}.")
        current[name] = value
    async with SessionFactory() as db:
        for name in values:
            key = f"automation.policy.{module}.{name}"
            row = await db.get(AppSetting, key)
            if row:
                row.value = str(current[name])
            else:
                db.add(AppSetting(key=key, value=str(current[name])))
        await db.commit()
    return current


async def module_quarantine_status(account_id: int, module: str) -> dict:
    async with SessionFactory() as db:
        row = await db.get(AppSetting, f"automation.quarantine.{account_id}.{module}")
        count = await db.get(AppSetting, f"automation.floodwait_count.{account_id}.{module}")
    return {
        "quarantined": bool(row and row.value),
        "reason": row.value if row else "",
        "floodwait_count": int(count.value) if count and count.value.isdigit() else 0,
    }


async def record_module_floodwait(account_id: int, module: str, seconds: int) -> dict:
    policy = await module_policy(module)
    async with SessionFactory() as db:
        account = await db.scalar(select(Account).where(Account.id == account_id).with_for_update())
        if not account:
            raise ValueError("Аккаунт не найден.")
        count_key = f"automation.floodwait_count.{account_id}.{module}"
        quarantine_key = f"automation.quarantine.{account_id}.{module}"
        row = await db.get(AppSetting, count_key)
        count = int(row.value) if row and row.value.isdigit() else 0
        count += 1
        if row:
            row.value = str(count)
        else:
            db.add(AppSetting(key=count_key, value=str(count)))
        quarantined = count >= policy["floodwait_threshold"] or int(seconds) > policy["floodwait_wait_seconds"]
        if quarantined:
            reason = f"FloodWait {int(seconds)} сек.; срабатываний {count}. Карантин до ручного снятия."
            quarantine = await db.get(AppSetting, quarantine_key)
            if quarantine:
                quarantine.value = reason
            else:
                db.add(AppSetting(key=quarantine_key, value=reason))
        await db.commit()
    return {"count": count, "threshold": policy["floodwait_threshold"], "quarantined": quarantined}


async def clear_module_quarantine(account_id: int, module: str) -> None:
    async with SessionFactory() as db:
        for key in (f"automation.quarantine.{account_id}.{module}", f"automation.floodwait_count.{account_id}.{module}"):
            row = await db.get(AppSetting, key)
            if row:
                await db.delete(row)
        await db.commit()


async def set_operation_limits(
    operation: str, *, interval_seconds: int, daily_limit: int,
    account_id: int | None = None,
    quiet_start_utc: str | None = None, quiet_end_utc: str | None = None,
) -> None:
    """Compatibility API; all module limits are global and shared by selected accounts."""
    if account_id is not None:
        raise ValueError("Индивидуальные лимиты отключены. Настройки задаются глобально.")
    await set_module_policy(operation, interval_seconds=interval_seconds, daily_limit=daily_limit)
    quiet_start, quiet_end = _normalize_quiet_hours(quiet_start_utc, quiet_end_utc)
    async with SessionFactory() as db:
        for suffix, value in (("quiet_start_utc", quiet_start), ("quiet_end_utc", quiet_end)):
            key = f"automation.default.{operation}.{suffix}"
            row = await db.get(AppSetting, key)
            if row:
                row.value = value
            else:
                db.add(AppSetting(key=key, value=value))
        await db.commit()


async def operation_limits(account_id: int, operation: str) -> dict:
    policy = await module_policy(operation)
    keys = [f"automation.default.{operation}.quiet_start_utc", f"automation.default.{operation}.quiet_end_utc"]
    async with SessionFactory() as db:
        rows = (await db.scalars(select(AppSetting).where(AppSetting.key.in_(keys)))).all()
    values = {row.key: row.value for row in rows}
    return {
        "operation": operation, **policy,
        "quiet_start_utc": values.get(keys[0], ""),
        "quiet_end_utc": values.get(keys[1], ""),
    }



def _validate_schedule_interval(interval_seconds: int) -> int:
    seconds = int(interval_seconds)
    if not 3600 <= seconds <= 31536000:
        raise ValueError("Интервал повторения должен быть от 1 часа до 365 дней.")
    return seconds


async def create_schedule(
    account_id: int, *, kind: str, recipients: list[int], payload: dict,
    interval_seconds: int, starts_at: datetime | None = None,
    ends_at: datetime | None = None, max_runs: int = 30,
    recipient_refs: dict[int, str] | None = None,
    recipient_labels: dict[int, str] | None = None,
) -> int:
    """Create a recurring campaign for explicitly selected Telegram peers."""
    if kind == "direct_invite":
        raise ValueError("Прямое добавление можно запланировать однократно, но не повторять автоматически.")
    normalized = _validate_payload(kind, payload)
    interval_seconds = _validate_schedule_interval(interval_seconds)
    if not 1 <= int(max_runs) <= 365:
        raise ValueError("Количество повторов должно быть от 1 до 365.")
    unique_ids = list(dict.fromkeys(int(peer_id) for peer_id in recipients))
    if not unique_ids or len(unique_ids) > MAX_RECIPIENTS:
        raise ValueError(f"Выберите от 1 до {MAX_RECIPIENTS} получателей.")
    module = _module_name_for_kind(kind)
    policy = await module_policy(module)
    if len(unique_ids) > policy["per_account_limit"]:
        raise ValueError(
            f"Глобальный лимит модуля «{module}»: не более {policy['per_account_limit']} "
            "получателей на один аккаунт в одном расписании."
        )
    due = _aware(starts_at or utcnow())
    end = _aware(ends_at) if ends_at else None
    if due < utcnow() - timedelta(minutes=1) or (end and end <= due):
        raise ValueError("Проверьте время начала и окончания расписания.")
    refs, labels = recipient_refs or {}, recipient_labels or {}
    schedule_recipients = [
        {
            "peer_id": peer_id,
            "peer_ref": str(refs.get(peer_id) or "").strip(),
            "peer_label": str(labels.get(peer_id) or refs.get(peer_id) or peer_id).strip(),
        }
        for peer_id in unique_ids
    ]
    if kind != "group_broadcast" and any(not item["peer_ref"] for item in schedule_recipients):
        raise ValueError("Для каждого получателя сохраните username, повторно выбрав его по @username.")
    async with SessionFactory() as db:
        account = await db.get(Account, account_id)
        if not account or not account.enabled:
            raise ValueError("Аккаунт не найден или отключён.")
        schedule = AutomationSchedule(
            account_id=account_id, kind=kind,
            payload=json.dumps(normalized, ensure_ascii=False),
            recipients_json=json.dumps(schedule_recipients, ensure_ascii=False),
            interval_seconds=int(interval_seconds), next_run_at=due,
            ends_at=end, max_runs=int(max_runs), status="active",
        )
        db.add(schedule)
        await db.commit()
        return int(schedule.id)


async def list_schedules(account_id: int, *, limit: int = 20) -> list[dict]:
    async with SessionFactory() as db:
        rows = list((await db.scalars(select(AutomationSchedule).where(
            AutomationSchedule.account_id == account_id
        ).order_by(AutomationSchedule.next_run_at, AutomationSchedule.id).limit(max(1, min(limit, 50))))).all())
        return [{
            "id": row.id, "kind": row.kind, "status": row.status,
            "next_run_at": _aware(row.next_run_at).isoformat(),
            "interval_seconds": row.interval_seconds, "runs_count": row.runs_count,
            "max_runs": row.max_runs, "last_error": row.last_error,
            "last_task_id": row.last_task_id,
        } for row in rows]


async def set_schedule_status(schedule_id: int, status: str) -> None:
    if status not in {"active", "paused", "cancelled"}:
        raise ValueError("Недопустимый статус расписания.")
    payload = None
    account_id = None
    async with SessionFactory() as db:
        row = await db.get(AutomationSchedule, schedule_id)
        if not row:
            raise ValueError("Расписание не найдено.")
        if row.status in {"completed", "cancelled"}:
            raise ValueError("Расписание уже завершено.")
        row.status = status
        row.updated_at = utcnow()
        if status == "cancelled":
            payload = json.loads(row.payload)
            account_id = row.account_id
        await db.commit()
    if payload is not None and account_id is not None:
        await _cleanup_campaign_upload(account_id, payload)


async def dispatch_due_schedules(*, max_schedules: int = 10) -> int:
    """Atomically materialize due recurring schedules into ordinary durable campaigns."""
    created = 0
    for _ in range(max(1, min(int(max_schedules), 50))):
        async with SessionFactory() as db:
            now = utcnow()
            row = await db.scalar(select(AutomationSchedule).where(
                AutomationSchedule.status == "active",
                AutomationSchedule.next_run_at <= now,
            ).order_by(AutomationSchedule.next_run_at, AutomationSchedule.id)
             .with_for_update(skip_locked=True).limit(1))
            if not row:
                break
            if row.last_task_id:
                previous_task = await db.get(AutomationTask, row.last_task_id)
                if previous_task and previous_task.status in {"failed", "needs_review"}:
                    row.status = "paused"
                    row.last_error = (
                        f"Расписание приостановлено: предыдущая задача #{previous_task.id} "
                        f"имеет статус {previous_task.status}; сначала разберите её."
                    )
                    row.updated_at = now
                    await db.commit()
                    continue
                if previous_task and previous_task.status in {"queued", "running", "paused"}:
                    row.next_run_at = now + timedelta(seconds=60)
                    row.last_error = f"Ожидание завершения предыдущей задачи #{previous_task.id}."
                    row.updated_at = now
                    await db.commit()
                    continue
            try:
                raw_recipients = json.loads(row.recipients_json)
                if raw_recipients and isinstance(raw_recipients[0], dict):
                    schedule_recipients = {
                        int(item["peer_id"]): item for item in raw_recipients
                    }
                else:
                    # Legacy schedules stored only peer IDs. Reuse old saved references
                    # for compatibility; these rows are not checked as permission gates.
                    legacy_ids = [int(peer_id) for peer_id in raw_recipients]
                    legacy_rows = list((await db.scalars(select(AutomationConsent).where(
                        AutomationConsent.account_id == row.account_id,
                        AutomationConsent.peer_id.in_(legacy_ids),
                    ).order_by(AutomationConsent.granted_at.desc()))).all()) if legacy_ids else []
                    legacy_refs = {}
                    for legacy in legacy_rows:
                        legacy_refs.setdefault(int(legacy.peer_id), legacy)
                    schedule_recipients = {
                        peer_id: {
                            "peer_id": peer_id,
                            "peer_ref": str(legacy_refs[peer_id].peer_ref) if peer_id in legacy_refs else "",
                            "peer_label": str(legacy_refs[peer_id].peer_label) if peer_id in legacy_refs else str(peer_id),
                        }
                        for peer_id in legacy_ids
                    }
                ids = list(schedule_recipients)
                task_payload = json.loads(row.payload)
                task_payload["_schedule_id"] = int(row.id)
                task = AutomationTask(
                    account_id=row.account_id, kind=row.kind,
                    payload=json.dumps(task_payload, ensure_ascii=False),
                    status="queued", scheduled_at=now,
                )
                db.add(task)
                await db.flush()
                for peer_id in ids:
                    recipient = schedule_recipients[peer_id]
                    db.add(AutomationTaskRecipient(
                        task_id=task.id, peer_id=peer_id,
                        peer_ref=str(recipient.get("peer_ref") or ""),
                        peer_label=str(recipient.get("peer_label") or peer_id),
                    ))
                row.last_task_id = task.id
                row.runs_count += 1
                row.last_error = ""
                next_run = _aware(row.next_run_at) + timedelta(seconds=row.interval_seconds)
                if row.runs_count >= row.max_runs or (row.ends_at and next_run > _aware(row.ends_at)):
                    row.status = "completed"
                else:
                    row.next_run_at = next_run
                row.updated_at = now
                await db.commit()
                created += 1
            except Exception:
                await db.rollback()
                raise
    return created


async def resolve_uncertain_recipient(task_id: int, peer_id: int, *, delivered: bool) -> None:
    """Operator reconciliation for a delivery whose acknowledgement was lost."""
    cleanup_payload = None
    cleanup_account_id = None
    async with SessionFactory() as db:
        item = await db.scalar(select(AutomationTaskRecipient).where(
            AutomationTaskRecipient.task_id == task_id,
            AutomationTaskRecipient.peer_id == int(peer_id),
            AutomationTaskRecipient.status == "uncertain",
        ).with_for_update())
        if not item:
            raise ValueError("Не найдена доставка со статусом «результат неизвестен».")
        if delivered:
            item.status = "sent"
            item.delivered_at = utcnow()
            item.last_error = "Подтверждено оператором после проверки Telegram."
        else:
            item.status = "pending"
            item.last_error = "Оператор проверил Telegram: сообщение не доставлено; разрешён повтор."
        task = await db.get(AutomationTask, task_id)
        if task and task.status == "needs_review":
            remaining_uncertain = await db.scalar(select(func.count()).select_from(AutomationTaskRecipient).where(
                AutomationTaskRecipient.task_id == task_id,
                AutomationTaskRecipient.status == "uncertain",
            ))
            remaining_pending = await db.scalar(select(func.count()).select_from(AutomationTaskRecipient).where(
                AutomationTaskRecipient.task_id == task_id,
                AutomationTaskRecipient.status.in_(["pending", "sending"]),
            ))
            remaining_failed = await db.scalar(select(func.count()).select_from(AutomationTaskRecipient).where(
                AutomationTaskRecipient.task_id == task_id,
                AutomationTaskRecipient.status == "failed",
            ))
            if not int(remaining_uncertain or 0):
                if int(remaining_pending or 0):
                    task.status = "queued"
                    task.completed_at = None
                else:
                    task.status = "failed" if int(remaining_failed or 0) else "completed"
                    task.completed_at = utcnow()
        if task and task.status == "completed":
            cleanup_payload = json.loads(task.payload)
            cleanup_account_id = task.account_id
        await db.commit()
    if cleanup_payload is not None and cleanup_account_id is not None:
        await _cleanup_campaign_upload(cleanup_account_id, cleanup_payload)



ALLOWED_REACTIONS = {"👍", "👎", "❤️", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🎉", "💩", "🙏"}


async def schedule_reaction(
    account_id: int, *, chat_ref: str, message_id: int, emoji: str,
    run_at: datetime | None = None,
) -> int:
    chat = str(chat_ref or "").strip()
    if not chat or len(chat) > 255:
        raise ValueError("Укажите @username или ссылку на чат.")
    if int(message_id) <= 0:
        raise ValueError("ID публикации должен быть положительным числом.")
    if emoji not in ALLOWED_REACTIONS:
        raise ValueError("Реакция не поддерживается.")
    due = _aware(run_at or utcnow())
    if due < utcnow() - timedelta(minutes=1):
        raise ValueError("Время запуска уже прошло.")
    async with SessionFactory() as db:
        account = await db.get(Account, account_id)
        if not account or not account.enabled:
            raise ValueError("Аккаунт не найден или отключён.")
        row = ScheduledReaction(
            account_id=account_id, chat_ref=chat, message_id=int(message_id),
            emoji=emoji, run_at=due, status="queued",
        )
        db.add(row)
        await db.commit()
        return int(row.id)


async def list_scheduled_reactions(account_id: int, *, limit: int = 20) -> list[dict]:
    async with SessionFactory() as db:
        rows = list((await db.scalars(select(ScheduledReaction).where(
            ScheduledReaction.account_id == account_id
        ).order_by(ScheduledReaction.run_at, ScheduledReaction.id).limit(max(1, min(limit, 50))))).all())
        return [{
            "id": row.id, "chat_ref": row.chat_ref, "message_id": row.message_id,
            "emoji": row.emoji, "run_at": _aware(row.run_at).isoformat(),
            "status": row.status, "last_error": row.last_error,
        } for row in rows]


async def cancel_scheduled_reaction(reaction_id: int) -> None:
    async with SessionFactory() as db:
        row = await db.get(ScheduledReaction, reaction_id)
        if not row:
            raise ValueError("Запланированная реакция не найдена.")
        if row.status != "queued":
            raise ValueError("Можно отменить только ожидающую реакцию.")
        row.status = "cancelled"
        row.updated_at = utcnow()
        await db.commit()


async def resolve_scheduled_reaction(reaction_id: int, *, delivered: bool) -> None:
    """Reconcile an uncertain reaction after the operator checks the target message."""
    async with SessionFactory() as db:
        row = await db.scalar(select(ScheduledReaction).where(
            ScheduledReaction.id == reaction_id,
            ScheduledReaction.status == "uncertain",
        ).with_for_update())
        if not row:
            raise ValueError("Не найдена реакция с неопределённым результатом.")
        if delivered:
            row.status = "sent"
            row.last_error = "Оператор подтвердил реакцию после проверки Telegram."
        else:
            row.status = "queued"
            row.run_at = utcnow()
            row.last_error = "Оператор проверил публикацию: реакции нет; разрешён повтор."
        row.updated_at = utcnow()
        await db.commit()


async def process_due_reactions(*, max_jobs: int = 5) -> int:
    """Execute one-shot reactions; stale in-flight jobs become uncertain, never duplicated."""
    from app.telegram_gateway.management import react_to_message

    processed = 0
    for _ in range(max(1, min(int(max_jobs), 20))):
        async with SessionFactory() as db:
            now = utcnow()
            row = await db.scalar(select(ScheduledReaction).where(
                or_(
                    (ScheduledReaction.status == "queued") & (ScheduledReaction.run_at <= now),
                    (ScheduledReaction.status == "running") & (
                        ScheduledReaction.started_at.is_(None)
                        | (ScheduledReaction.started_at <= now - timedelta(minutes=10))
                    ),
                )
            ).order_by(ScheduledReaction.run_at, ScheduledReaction.id)
             .with_for_update(skip_locked=True).limit(1))
            if not row:
                break
            if row.status == "running":
                row.status = "uncertain"
                row.last_error = "Результат неизвестен после перезапуска. Проверьте публикацию вручную."
                row.updated_at = now
                await db.commit()
                processed += 1
                continue
            row.status = "running"
            row.started_at = now
            row.updated_at = now
            snapshot = (row.id, row.account_id, row.chat_ref, row.message_id, row.emoji)
            await db.commit()
        reaction_id, account_id, chat_ref, message_id, emoji = snapshot
        try:
            await react_to_message(account_id, chat=chat_ref, message_id=message_id, emoji=emoji)
        except Exception as exc:
            from telethon.errors import FloodWaitError
            async with SessionFactory() as db:
                row = await db.get(ScheduledReaction, reaction_id)
                if row and row.status == "running":
                    if isinstance(exc, FloodWaitError):
                        row.status = "queued"
                        row.run_at = utcnow() + timedelta(seconds=max(1, int(exc.seconds)))
                    elif isinstance(exc, OperationRateLimitError):
                        row.status = "queued"
                        row.run_at = utcnow() + timedelta(seconds=exc.retry_after)
                    elif isinstance(exc, (TimeoutError, ConnectionError, OSError)):
                        row.status = "uncertain"
                    else:
                        row.status = "failed"
                    row.last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
                    row.updated_at = utcnow()
                    await db.commit()
        else:
            async with SessionFactory() as db:
                row = await db.get(ScheduledReaction, reaction_id)
                if row and row.status == "running":
                    row.status = "sent"
                    row.last_error = ""
                    row.updated_at = utcnow()
                    await db.commit()
        processed += 1
    return processed



class OperationRateLimitError(RuntimeError):
    def __init__(self, operation: str, retry_after: int, reason: str):
        self.operation = operation
        self.retry_after = max(1, int(retry_after))
        super().__init__(f"{reason} Повторите через {self.retry_after} сек.")


async def reserve_operation_slot(account_id: int, operation: str) -> None:
    """Atomically reserve one local operation slot before touching Telegram."""
    if operation not in {"subscription", "reaction"}:
        raise ValueError("Для прямого резервирования поддерживаются subscription и reaction.")
    quarantine = await module_quarantine_status(account_id, operation)
    if quarantine["quarantined"]:
        raise OperationRateLimitError(operation, 86400, f"Аккаунт в карантине модуля: {quarantine['reason']}")
    policy = await module_policy(operation)
    async with SessionFactory() as db:
        account = await db.scalar(select(Account).where(Account.id == account_id).with_for_update())
        if not account or not account.enabled:
            raise ValueError("Аккаунт не найден или отключён.")
        keys = [
            f"automation.default.{operation}.quiet_start_utc",
            f"automation.default.{operation}.quiet_end_utc",
        ]
        values = {row.key: row.value for row in (await db.scalars(
            select(AppSetting).where(AppSetting.key.in_(keys))
        )).all()}
        interval = policy["interval_seconds"]
        daily = policy["daily_account_limit"]
        now = utcnow()
        await db.execute(delete(AutomationOperationEvent).where(
            AutomationOperationEvent.account_id == account_id,
            AutomationOperationEvent.occurred_at < now - timedelta(days=7),
        ))
        quiet_start = values.get(f"automation.default.{operation}.quiet_start_utc", "") or None
        quiet_end = values.get(f"automation.default.{operation}.quiet_end_utc", "") or None
        quiet_until = _quiet_until(now, quiet_start, quiet_end)
        if quiet_until:
            wait_seconds = int((quiet_until - now).total_seconds())
            raise OperationRateLimitError(operation, max(1, wait_seconds), "Сейчас действуют тихие часы.")
        latest = await db.scalar(select(AutomationOperationEvent).where(
            AutomationOperationEvent.account_id == account_id,
            AutomationOperationEvent.operation == operation,
        ).order_by(AutomationOperationEvent.occurred_at.desc()).limit(1))
        if latest:
            elapsed = int((now - _aware(latest.occurred_at)).total_seconds())
            if elapsed < interval:
                raise OperationRateLimitError(operation, interval - elapsed, "Сработал локальный интервал операции.")
        window_start = now - timedelta(hours=24)
        events = list((await db.scalars(select(AutomationOperationEvent).where(
            AutomationOperationEvent.account_id == account_id,
            AutomationOperationEvent.operation == operation,
            AutomationOperationEvent.occurred_at >= window_start,
        ).order_by(AutomationOperationEvent.occurred_at))).all())
        if len(events) >= daily:
            retry_after = int((_aware(events[0].occurred_at) + timedelta(hours=24) - now).total_seconds())
            raise OperationRateLimitError(operation, max(1, retry_after), "Достигнут локальный лимит за 24 часа.")
        db.add(AutomationOperationEvent(account_id=account_id, operation=operation, occurred_at=now))
        await db.commit()
