"""Opt-in campaign queue with durable scheduling and Telegram FloodWait handling.

Only explicitly consented peers can be added to a campaign. The worker sends sequentially,
records each acknowledgement, and resumes safely after restarts. This module does not
scrape participants, add strangers to groups, or bypass Telegram limits.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, or_, select

from app.settings import get_settings
from app.storage.db import SessionFactory
from app.storage.models import (
    Account,
    AppSetting,
    AutomationConsent,
    AutomationTask,
    AutomationTaskRecipient,
    utcnow,
)

ALLOWED_KINDS = {"broadcast", "invite"}
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
    if kind == "invite" and not re.search(r"(?:https?://)?t\\.me/", normalized["text"] or normalized["caption"], re.I):
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
    if scope not in {"broadcast", "invite"}:
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
        return {
            "id": task.id, "account_id": task.account_id, "kind": task.kind, "status": task.status,
            "scheduled_at": _aware(task.scheduled_at).isoformat(),
            "last_error": task.last_error, "recipients": counts,
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


async def _account_limits(account_id: int) -> tuple[int, int]:
    async with SessionFactory() as db:
        rows = list((await db.scalars(select(AppSetting).where(
            AppSetting.key.in_([
                f"automation.{account_id}.interval_seconds",
                f"automation.{account_id}.daily_limit",
            ])
        ))).all())
    values = {row.key: row.value for row in rows}
    interval = max(1, min(3600, int(values.get(f"automation.{account_id}.interval_seconds", "30"))))
    daily = max(1, min(500, int(values.get(f"automation.{account_id}.daily_limit", "50"))))
    return interval, daily


async def set_account_limits(account_id: int, *, interval_seconds: int, daily_limit: int) -> None:
    if not 1 <= int(interval_seconds) <= 3600:
        raise ValueError("Интервал должен быть от 1 до 3600 секунд.")
    if not 1 <= int(daily_limit) <= 500:
        raise ValueError("Дневной лимит должен быть от 1 до 500 сообщений.")
    async with SessionFactory() as db:
        if not await db.get(Account, account_id):
            raise ValueError("Аккаунт не найден.")
        for key, value in (
            (f"automation.{account_id}.interval_seconds", str(int(interval_seconds))),
            (f"automation.{account_id}.daily_limit", str(int(daily_limit))),
        ):
            row = await db.get(AppSetting, key)
            if row:
                row.value = value
            else:
                db.add(AppSetting(key=key, value=value))
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
                stale_item.status = "pending"
                stale_item.last_error = "Recovered after worker restart; delivery may be retried."
            await db.commit()

        try:
            payload = json.loads(payload_raw)
            interval, daily_limit = await _account_limits(account_id)
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
                            current.status = "completed"
                            current.completed_at = utcnow()
                            current.updated_at = utcnow()
                            await db.commit()
                            break
                        # Consent is checked again immediately before each send.
                        scope = "invite" if kind == "invite" else "broadcast"
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
                        sent_today = await db.scalar(select(func.count()).select_from(
                            AutomationTaskRecipient
                        ).join(AutomationTask).where(
                            AutomationTask.account_id == account_id,
                            AutomationTaskRecipient.status == "sent",
                            AutomationTaskRecipient.delivered_at >= now - timedelta(hours=24),
                        ))
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
                        await db.commit()

                    try:
                        entity = await client.get_entity(peer_ref or peer_id)
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
                                item.status = "failed"
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
                    await asyncio.sleep(interval)
        except Exception as exc:
            async with SessionFactory() as db:
                current = await db.get(AutomationTask, task_id)
                if current and current.status == "running":
                    current.status = "failed"
                    current.last_error = f"{type(exc).__name__}: {str(exc)[:1000]}"
                    current.updated_at = utcnow()
                    await db.commit()
        processed += 1
    return processed


__all__ = [
    "campaign_status", "create_campaign", "grant_consent", "list_campaigns", "list_consents",
    "process_due_campaigns", "revoke_consent", "set_account_limits",
    "set_campaign_status",
]
