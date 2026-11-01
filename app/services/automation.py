"""Opt-in campaign queue with durable scheduling and Telegram FloodWait handling.

Only explicitly consented peers can be added to a campaign. The worker sends sequentially,
records each acknowledgement, and resumes safely after restarts. This module does not
scrape participants, add strangers to groups, or bypass Telegram limits.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, or_, select

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

ALLOWED_KINDS = {"broadcast", "invite", "group_broadcast"}
MAX_RECIPIENTS = 500
MAX_TEXT = 4096


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _validate_payload(kind: str, payload: dict) -> dict:
    if kind not in ALLOWED_KINDS:
        raise ValueError("Поддерживаются только рассылка и отправка приглашения.")
    normalized = {
        "text": str(payload.get("text") or "").strip(),
        "file_path": str(payload.get("file_path") or "").strip(),
        "caption": str(payload.get("caption") or "").strip(),
    }
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


async def grant_consent(
    account_id: int, peer_id: int, *, peer_ref: str = "", peer_label: str = "",
    scope: str = "broadcast", consent_source: str = "operator-confirmed",
) -> None:
    if scope not in {"broadcast", "invite", "group_publish"}:
        raise ValueError("Неизвестный тип согласия.")
    if int(peer_id) == 0:
        raise ValueError("Некорректный Telegram ID.")
    async with SessionFactory() as db:
        account = await db.get(Account, account_id)
        if not account:
            raise ValueError("Аккаунт не найден.")
        item = await db.scalar(select(AutomationConsent).where(
            AutomationConsent.account_id == account_id,
            AutomationConsent.peer_id == int(peer_id),
            AutomationConsent.scope == scope,
        ))
        if item:
            item.peer_ref = peer_ref[:255]
            item.peer_label = peer_label[:255]
            item.consent_source = consent_source[:255]
            item.granted_at = utcnow()
            item.revoked_at = None
        else:
            db.add(AutomationConsent(
                account_id=account_id, peer_id=int(peer_id), peer_ref=peer_ref[:255],
                peer_label=peer_label[:255], scope=scope,
                consent_source=consent_source[:255],
            ))
        await db.commit()


async def revoke_consent(account_id: int, peer_id: int, *, scope: str | None = None) -> int:
    async with SessionFactory() as db:
        query = select(AutomationConsent).where(
            AutomationConsent.account_id == account_id,
            AutomationConsent.peer_id == int(peer_id),
            AutomationConsent.revoked_at.is_(None),
        )
        if scope:
            query = query.where(AutomationConsent.scope == scope)
        rows = list((await db.scalars(query)).all())
        now = utcnow()
        for item in rows:
            item.revoked_at = now
        await db.commit()
        return len(rows)


async def list_consents(account_id: int, *, scope: str | None = None) -> list[dict]:
    async with SessionFactory() as db:
        query = select(AutomationConsent).where(
            AutomationConsent.account_id == account_id,
            AutomationConsent.revoked_at.is_(None),
        ).order_by(AutomationConsent.granted_at.desc())
        if scope:
            query = query.where(AutomationConsent.scope == scope)
        rows = list((await db.scalars(query.limit(MAX_RECIPIENTS))).all())
        return [{
            "peer_id": int(row.peer_id), "peer_ref": row.peer_ref, "peer_label": row.peer_label,
            "scope": row.scope, "granted_at": row.granted_at.isoformat(),
        } for row in rows]


async def create_campaign(
    account_id: int, *, kind: str, recipients: list[int],
    payload: dict, scheduled_at: datetime | None = None,
) -> int:
    normalized = _validate_payload(kind, payload)
    unique_ids = list(dict.fromkeys(int(peer_id) for peer_id in recipients))
    if not unique_ids or len(unique_ids) > MAX_RECIPIENTS:
        raise ValueError(f"Выберите от 1 до {MAX_RECIPIENTS} получателей.")
    due = _aware(scheduled_at or utcnow())
    if due < utcnow() - timedelta(minutes=1):
        raise ValueError("Время запуска уже прошло.")
    scope = "invite" if kind == "invite" else ("group_publish" if kind == "group_broadcast" else "broadcast")
    async with SessionFactory() as db:
        account = await db.get(Account, account_id)
        if not account or not account.enabled:
            raise ValueError("Аккаунт не найден или отключён.")
        consents = list((await db.scalars(select(AutomationConsent).where(
            AutomationConsent.account_id == account_id,
            AutomationConsent.peer_id.in_(unique_ids),
            AutomationConsent.scope == scope,
            AutomationConsent.revoked_at.is_(None),
        ))).all())
        by_id = {int(item.peer_id): item for item in consents}
        missing = [peer_id for peer_id in unique_ids if peer_id not in by_id]
        if missing:
            raise ValueError(
                f"Нет действующего согласия для {len(missing)} получателей. "
                "Без подтверждённого opt-in рассылка не создаётся."
            )
        task = AutomationTask(
            account_id=account_id, kind=kind,
            payload=json.dumps(normalized, ensure_ascii=False),
            status="queued", scheduled_at=due,
        )
        db.add(task)
        await db.flush()
        for peer_id in unique_ids:
            consent = by_id[peer_id]
            db.add(AutomationTaskRecipient(
                task_id=task.id, peer_id=peer_id, peer_ref=consent.peer_ref, peer_label=consent.peer_label,
            ))
        await db.commit()
        return int(task.id)


async def set_campaign_status(task_id: int, status: str) -> None:
    if status not in {"paused", "cancelled", "queued"}:
        raise ValueError("Недопустимый статус задачи.")
    async with SessionFactory() as db:
        task = await db.get(AutomationTask, task_id)
        if not task:
            raise ValueError("Задача не найдена.")
        if task.status in {"completed", "failed", "cancelled"}:
            raise ValueError("Задача уже завершена.")
        task.status = status
        task.updated_at = utcnow()
        await db.commit()


async def reset_account_limits(account_id: int) -> None:
    async with SessionFactory() as db:
        if not await db.get(Account, account_id):
            raise ValueError("Аккаунт не найден.")
        keys = [
            f"automation.{account_id}.interval_seconds",
            f"automation.{account_id}.daily_limit",
            f"automation.{account_id}.quiet_start_utc",
            f"automation.{account_id}.quiet_end_utc",
        ]
        rows = list((await db.scalars(select(AppSetting).where(AppSetting.key.in_(keys)))).all())
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
            AutomationTaskRecipient.status.in_(["failed", "sending"]),
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
        return {
            "id": task.id, "account_id": task.account_id, "kind": task.kind, "status": task.status,
            "scheduled_at": _aware(task.scheduled_at).isoformat(),
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
        async with SessionFactory() as db:
            task = await db.get(AutomationTask, task_id)
            if not task or task.status != "running":
                return
            task.started_at = utcnow()
            task.updated_at = utcnow()
            await db.commit()


async def process_due_campaigns(*, max_tasks: int = 1) -> int:
    """Process due opt-in campaigns. Called repeatedly by the dedicated worker loop."""
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
            interval, daily_limit, quiet_start, quiet_end = await _account_limits(account_id, "invite" if kind == "invite" else "broadcast")
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
                        # Consent is checked again immediately before each send.
                        scope = "invite" if kind == "invite" else ("group_publish" if kind == "group_broadcast" else "broadcast")
                        consent = await db.scalar(select(AutomationConsent).where(
                            AutomationConsent.account_id == account_id,
                            AutomationConsent.peer_id == recipient.peer_id,
                            AutomationConsent.scope == scope,
                            AutomationConsent.revoked_at.is_(None),
                        ))
                        if not consent:
                            recipient.status = "skipped"
                            recipient.last_error = "Consent revoked before delivery."
                            await db.commit()
                            continue
                        sent_query = select(func.count()).select_from(
                            AutomationTaskRecipient
                        ).join(AutomationTask).where(
                            AutomationTask.account_id == account_id,
                            AutomationTaskRecipient.status == "sent",
                            AutomationTaskRecipient.delivered_at >= now - timedelta(hours=24),
                        )
                        if kind == "invite":
                            sent_query = sent_query.where(AutomationTask.kind == "invite")
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
                        peer_ref = recipient.peer_ref
                        recipient.status = "sending"
                        recipient.attempts += 1
                        current.started_at = utcnow()
                        current.updated_at = utcnow()
                        await db.commit()

                    try:
                        entity = await client.get_entity(peer_ref or peer_id)
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
                                current.status = "queued"
                                current.scheduled_at = utcnow() + timedelta(seconds=seconds)
                                current.last_error = f"Telegram FloodWait: {seconds}s"
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
                            await db.commit()
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
            if final_task and (
                final_task.status == "cancelled"
                or (final_task.status in {"completed", "failed"} and int(unfinished_count or 0) == 0)
            ):
                try:
                    final_payload = json.loads(final_task.payload)
                    file_path = Path(final_payload.get("file_path") or "")
                    upload_root = (Path(get_settings().data_dir) / "automation_uploads").resolve()
                    if file_path.is_file() and upload_root in file_path.resolve().parents:
                        file_path.unlink(missing_ok=True)
                except (OSError, ValueError, TypeError):
                    pass
        processed += 1
    return processed


__all__ = [
    "campaign_status", "create_campaign", "grant_consent", "list_campaigns", "list_consents",
    "process_due_campaigns", "reset_account_limits", "retry_failed_campaign", "revoke_consent", "set_account_limits",
    "set_global_automation_limits", "set_operation_limits", "operation_limits",
    "create_schedule", "list_schedules", "set_schedule_status", "dispatch_due_schedules",
    "resolve_uncertain_recipient", "schedule_reaction", "list_scheduled_reactions",
    "cancel_scheduled_reaction", "process_due_reactions", "reserve_operation_slot",
    "OperationRateLimitError",
    "set_campaign_status",
]



async def set_operation_limits(
    operation: str, *, interval_seconds: int, daily_limit: int,
    account_id: int | None = None,
    quiet_start_utc: str | None = None, quiet_end_utc: str | None = None,
) -> None:
    """Set independent broadcast/invite cadence and daily caps, globally or per account."""
    if operation not in {"broadcast", "invite", "subscription", "reaction"}:
        raise ValueError("Операция должна быть broadcast, invite, subscription или reaction.")
    if not 1 <= int(interval_seconds) <= 3600:
        raise ValueError("Интервал должен быть от 1 до 3600 секунд.")
    if not 1 <= int(daily_limit) <= 500:
        raise ValueError("Дневной лимит должен быть от 1 до 500.")
    quiet_start, quiet_end = _normalize_quiet_hours(quiet_start_utc, quiet_end_utc)
    prefix = f"automation.{account_id}" if account_id is not None else "automation.default"
    async with SessionFactory() as db:
        if account_id is not None and not await db.get(Account, account_id):
            raise ValueError("Аккаунт не найден.")
        for suffix, value in (
            (f"{operation}.interval_seconds", str(int(interval_seconds))),
            (f"{operation}.daily_limit", str(int(daily_limit))),
            (f"{operation}.quiet_start_utc", quiet_start),
            (f"{operation}.quiet_end_utc", quiet_end),
        ):
            key = f"{prefix}.{suffix}"
            row = await db.get(AppSetting, key)
            if row:
                row.value = value
            else:
                db.add(AppSetting(key=key, value=value))
        await db.commit()


async def operation_limits(account_id: int, operation: str) -> dict:
    if operation not in {"broadcast", "invite", "subscription", "reaction"}:
        raise ValueError("Неизвестный тип операции.")
    interval, daily, quiet_start, quiet_end = await _account_limits(account_id, operation)
    return {
        "operation": operation, "interval_seconds": interval, "daily_limit": daily,
        "quiet_start_utc": quiet_start or "", "quiet_end_utc": quiet_end or "",
    }


async def create_schedule(
    account_id: int, *, kind: str, recipients: list[int], payload: dict,
    interval_seconds: int, starts_at: datetime | None = None,
    ends_at: datetime | None = None, max_runs: int = 30,
) -> int:
    """Create a recurring campaign; repeated sends require explicit opt-in and >=24h spacing."""
    normalized = _validate_payload(kind, payload)
    if not 86400 <= int(interval_seconds) <= 31536000:
        raise ValueError("Повторение должно быть от 24 часов до 365 дней.")
    if not 1 <= int(max_runs) <= 365:
        raise ValueError("Количество повторов должно быть от 1 до 365.")
    unique_ids = list(dict.fromkeys(int(peer_id) for peer_id in recipients))
    if not unique_ids or len(unique_ids) > MAX_RECIPIENTS:
        raise ValueError(f"Выберите от 1 до {MAX_RECIPIENTS} получателей.")
    due = _aware(starts_at or utcnow())
    end = _aware(ends_at) if ends_at else None
    if due < utcnow() - timedelta(minutes=1) or (end and end <= due):
        raise ValueError("Проверьте время начала и окончания расписания.")
    scope = "invite" if kind == "invite" else "broadcast"
    async with SessionFactory() as db:
        account = await db.get(Account, account_id)
        if not account or not account.enabled:
            raise ValueError("Аккаунт не найден или отключён.")
        consents = list((await db.scalars(select(AutomationConsent).where(
            AutomationConsent.account_id == account_id,
            AutomationConsent.peer_id.in_(unique_ids),
            AutomationConsent.scope == scope,
            AutomationConsent.revoked_at.is_(None),
        ))).all())
        if len(consents) != len(unique_ids):
            raise ValueError("Для каждого получателя требуется действующее согласие соответствующего типа.")
        schedule = AutomationSchedule(
            account_id=account_id, kind=kind,
            payload=json.dumps(normalized, ensure_ascii=False),
            recipients_json=json.dumps(unique_ids),
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
    async with SessionFactory() as db:
        row = await db.get(AutomationSchedule, schedule_id)
        if not row:
            raise ValueError("Расписание не найдено.")
        if row.status in {"completed", "cancelled"}:
            raise ValueError("Расписание уже завершено.")
        row.status = status
        row.updated_at = utcnow()
        await db.commit()


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
            try:
                ids = list(dict.fromkeys(int(x) for x in json.loads(row.recipients_json)))
                scope = "invite" if row.kind == "invite" else ("group_publish" if row.kind == "group_broadcast" else "broadcast")
                consents = list((await db.scalars(select(AutomationConsent).where(
                    AutomationConsent.account_id == row.account_id,
                    AutomationConsent.peer_id.in_(ids),
                    AutomationConsent.scope == scope,
                    AutomationConsent.revoked_at.is_(None),
                ))).all()) if ids else []
                by_id = {int(item.peer_id): item for item in consents}
                if len(by_id) != len(ids):
                    row.status = "paused"
                    row.last_error = "Расписание приостановлено: одно или несколько согласий отозваны."
                    row.updated_at = now
                    await db.commit()
                    continue
                task = AutomationTask(
                    account_id=row.account_id, kind=row.kind, payload=row.payload,
                    status="queued", scheduled_at=now,
                )
                db.add(task)
                await db.flush()
                for peer_id in ids:
                    consent = by_id[peer_id]
                    db.add(AutomationTaskRecipient(
                        task_id=task.id, peer_id=peer_id,
                        peer_ref=consent.peer_ref, peer_label=consent.peer_label,
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
        await db.commit()



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
    defaults = {"subscription": (5, 20), "reaction": (30, 50)}
    default_interval, default_daily = defaults[operation]
    async with SessionFactory() as db:
        account = await db.scalar(select(Account).where(Account.id == account_id).with_for_update())
        if not account or not account.enabled:
            raise ValueError("Аккаунт не найден или отключён.")
        keys = [
            f"automation.default.{operation}.interval_seconds",
            f"automation.default.{operation}.daily_limit",
            f"automation.{account_id}.{operation}.interval_seconds",
            f"automation.{account_id}.{operation}.daily_limit",
            f"automation.default.{operation}.quiet_start_utc",
            f"automation.default.{operation}.quiet_end_utc",
            f"automation.{account_id}.{operation}.quiet_start_utc",
            f"automation.{account_id}.{operation}.quiet_end_utc",
        ]
        values = {row.key: row.value for row in (await db.scalars(
            select(AppSetting).where(AppSetting.key.in_(keys))
        )).all()}
        interval = max(1, min(3600, int(values.get(
            f"automation.{account_id}.{operation}.interval_seconds",
            values.get(f"automation.default.{operation}.interval_seconds", str(default_interval)),
        ))))
        daily = max(1, min(500, int(values.get(
            f"automation.{account_id}.{operation}.daily_limit",
            values.get(f"automation.default.{operation}.daily_limit", str(default_daily)),
        ))))
        now = utcnow()
        quiet_start = values.get(
            f"automation.{account_id}.{operation}.quiet_start_utc",
            values.get(f"automation.default.{operation}.quiet_start_utc", ""),
        ) or None
        quiet_end = values.get(
            f"automation.{account_id}.{operation}.quiet_end_utc",
            values.get(f"automation.default.{operation}.quiet_end_utc", ""),
        ) or None
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
