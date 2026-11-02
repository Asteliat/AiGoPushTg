from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import parse_qsl

from aiogram import Bot
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.settings import get_settings
from app.storage.db import SessionFactory, get_session
from app.storage.models import (
    Account, AIAgent, AppSetting, AuditEvent, ChannelBinding, ChatBinding,
    Example, Persona, TriggerRule, ChannelAnalysis, ContentDraft,
)

router = APIRouter(prefix="/v1/miniapp", tags=["miniapp"])


def _telegram_user_id(init_data: str | None) -> int:
    cfg = get_settings()
    if not cfg.bot_token or not init_data:
        raise HTTPException(status_code=401, detail="Откройте Mini App через Telegram.")
    try:
        raw_pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        if len(raw_pairs) != len({key for key, _ in raw_pairs}):
            raise ValueError("Duplicate Telegram initData fields.")
        pairs = dict(raw_pairs)
        received_hash = pairs.pop("hash", "")
        auth_date = int(pairs.get("auth_date", "0"))
        user = json.loads(pairs.get("user", "{}"))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Некорректные данные Telegram.") from None
    if not received_hash or auth_date > time.time() + 60 or time.time() - auth_date > 86400:
        raise HTTPException(status_code=401, detail="Срок авторизации истёк. Откройте Mini App заново.")
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", cfg.bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(status_code=401, detail="Не удалось проверить подпись Telegram.")
    try:
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Telegram не передал пользователя.") from None
    if user_id not in cfg.admin_id_set:
        raise HTTPException(status_code=403, detail="Нет доступа к панели управления.")
    return user_id


async def require_admin(x_telegram_init_data: str | None = Header(default=None)) -> int:
    return _telegram_user_id(x_telegram_init_data)


def _snapshot_agent(agent: AIAgent) -> SimpleNamespace:
    """Detach provider settings from ORM state before any network request."""
    return SimpleNamespace(
        enabled=bool(agent.enabled),
        api_key=str(agent.api_key),
        base_url=str(agent.base_url),
        model=str(agent.model),
        timeout_seconds=int(agent.timeout_seconds),
    )


def _format_inbox_context(messages: list[Any]) -> str:
    """Format chronological Telegram messages with real line breaks."""
    return "\n".join(
        ("Аккаунт: " if item.out else "Собеседник: ")
        + ((item.message or "[медиа/вложение]")[:1200])
        for item in messages
    )


async def _audit(db: AsyncSession, actor: int, action: str, kind: str, object_id: Any = "") -> None:
    db.add(AuditEvent(
        actor_telegram_id=actor, action=action[:80],
        object_type=kind[:80], object_id=str(object_id)[:80],
    ))


def _account(row: Account) -> dict:
    return {
        "id": row.id, "telegram_user_id": row.telegram_user_id,
        "username": row.username, "display_name": row.display_name,
        "enabled": row.enabled, "manual_pause": row.manual_pause,
        "connection_status": row.connection_status, "ai_enabled": row.ai_enabled,
        "ai_agent_id": row.ai_agent_id, "persona_id": row.persona_id,
        "reply_mode": row.reply_mode, "channel_reply_mode": row.channel_reply_mode,
        "channel_chat_replies_enabled": row.channel_chat_replies_enabled,
        "channel_reply_probability": row.channel_reply_probability,
        "channel_reply_cooldown_seconds": row.channel_reply_cooldown_seconds,
        "channel_post_instructions": row.channel_post_instructions,
        "channel_comments_instructions": row.channel_comments_instructions,
        "channel_restrictions_instructions": row.channel_restrictions_instructions,
        "learning_status": row.learning_status,
        "learning_completed_at": row.learning_completed_at.isoformat() if row.learning_completed_at else None,
        "learning_limit": row.learning_limit,
    }


def _agent(row: AIAgent) -> dict:
    return {
        "id": row.id, "name": row.name, "base_url": row.base_url,
        "model": row.model, "timeout_seconds": row.timeout_seconds,
        "enabled": row.enabled, "is_default": row.is_default,
        "has_api_key": bool(row.api_key),
        "mention_reply_probability_override": row.mention_reply_probability_override,
        "dm_reply_probability_override": row.dm_reply_probability_override,
        "proactive_reply_probability_override": row.proactive_reply_probability_override,
        "reply_cooldown_seconds_override": row.reply_cooldown_seconds_override,
        "channel_reply_probability_override": row.channel_reply_probability_override,
        "channel_reply_cooldown_seconds_override": row.channel_reply_cooldown_seconds_override,
    }


def _persona(row: Persona) -> dict:
    return {"id": row.id, "name": row.name, "description": row.description,
            "style_instructions": row.style_instructions}


def _example(row: Example) -> dict:
    return {"id": row.id, "account_id": row.account_id, "persona_id": row.persona_id,
            "title": row.title, "incoming_text": row.incoming_text,
            "preferred_reply": row.preferred_reply, "consent_confirmed": row.consent_confirmed}


def _trigger(row: TriggerRule) -> dict:
    return {"id": row.id, "account_id": row.account_id, "apply_mode": row.apply_mode,
            "terms": row.terms, "response_instructions": row.response_instructions,
            "cooldown_seconds": row.cooldown_seconds,
            "message_cooldown_seconds": row.message_cooldown_seconds, "enabled": row.enabled}


class AccountPatch(BaseModel):
    enabled: bool | None = None
    manual_pause: bool | None = None
    ai_enabled: bool | None = None
    ai_agent_id: int | None = None
    persona_id: int | None = None
    reply_mode: str | None = Field(default=None, pattern="^(manual|automatic)$")
    channel_reply_mode: str | None = Field(default=None, pattern="^(manual|automatic)$")
    channel_chat_replies_enabled: bool | None = None
    channel_reply_probability: int | None = Field(default=None, ge=0, le=100)
    channel_reply_cooldown_seconds: int | None = Field(default=None, ge=0, le=31536000)
    channel_post_instructions: str | None = Field(default=None, max_length=6000)
    channel_comments_instructions: str | None = Field(default=None, max_length=6000)
    channel_restrictions_instructions: str | None = Field(default=None, max_length=6000)

class AgentInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(default="https://api.openai.com/v1", max_length=500)
    model: str = Field(default="gpt-4o-mini", min_length=1, max_length=160)
    timeout_seconds: int = Field(default=45, ge=5, le=180)
    enabled: bool = True
    is_default: bool = False
    api_key: str = ""
    mention_reply_probability_override: int | None = Field(default=None, ge=0, le=100)
    dm_reply_probability_override: int | None = Field(default=None, ge=0, le=100)
    proactive_reply_probability_override: int | None = Field(default=None, ge=0, le=100)
    reply_cooldown_seconds_override: int | None = Field(default=None, ge=0, le=2147483647)
    channel_reply_probability_override: int | None = Field(default=None, ge=0, le=100)
    channel_reply_cooldown_seconds_override: int | None = Field(default=None, ge=0, le=2147483647)


class PersonaInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=2000)
    style_instructions: str = Field(default="", max_length=12000)


class ExampleInput(BaseModel):
    account_id: int | None = None
    persona_id: int | None = None
    title: str = Field(default="", max_length=160)
    incoming_text: str = Field(min_length=1, max_length=12000)
    preferred_reply: str = Field(min_length=1, max_length=12000)
    consent_confirmed: bool


class TriggerInput(BaseModel):
    account_id: int | None = None
    apply_mode: str = Field(default="empty_only", pattern="^(account|empty_only|all)$")
    terms: str = Field(min_length=1, max_length=4000)
    response_instructions: str = Field(default="", max_length=8000)
    cooldown_seconds: int = Field(default=900, ge=0, le=31536000)
    message_cooldown_seconds: int = Field(default=0, ge=0, le=31536000)
    enabled: bool = True


class SettingsInput(BaseModel):
    values: dict[str, Any]
    apply_to_agents: Literal["inherit", "all"] = "inherit"


class SearchInput(BaseModel):
    account_id: int
    query: str = Field(min_length=2, max_length=100)
    limit: int = Field(default=30, ge=5, le=80)


class BindingInput(BaseModel):
    account_id: int
    kind: str = Field(pattern="^(group|channel)$")
    peer_id: int
    title: str = Field(default="", max_length=255)


class LogDestinationInput(BaseModel):
    chat_id: int
    thread_id: int | None = Field(default=None, gt=0)


class ChannelAssistantPatch(BaseModel):
    enabled: bool | None = None
    reply_mode_override: Literal["manual", "automatic"] | None = None
    chat_replies_enabled_override: bool | None = None
    post_instructions_override: str | None = Field(default=None, max_length=6000)
    comments_instructions_override: str | None = Field(default=None, max_length=6000)
    restrictions_instructions_override: str | None = Field(default=None, max_length=6000)
    reply_probability_override: int | None = Field(default=None, ge=0, le=100)
    reply_cooldown_seconds_override: int | None = Field(default=None, ge=0, le=31536000)


class ContentGenerateInput(BaseModel):
    account_id: int | None = Field(default=None, ge=1)
    agent_id: int | None = Field(default=None, ge=1)
    persona_id: int | None = Field(default=None, ge=1)
    channel_binding_id: int | None = Field(default=None, ge=1)
    title: str = Field(default="", max_length=160)
    brief: str = Field(min_length=5, max_length=6000)




class InboxPeerInput(BaseModel):
    account_id: int = Field(ge=1)
    peer_id: int
    peer_ref: str = Field(default="", max_length=255)


class InboxReplyInput(InboxPeerInput):
    text: str = Field(min_length=1, max_length=4096)
    reply_to_message_id: int = Field(gt=0)


class ContentDraftPatch(BaseModel):
    title: str | None = Field(default=None, max_length=160)
    body: str | None = Field(default=None, min_length=1, max_length=12000)
    status: Literal["draft", "archived"] | None = None


@router.get("/overview")
async def overview(_: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    accounts = list((await db.scalars(select(Account).order_by(Account.id))).all())
    agents = list((await db.scalars(select(AIAgent).order_by(AIAgent.id))).all())
    personas = list((await db.scalars(select(Persona).order_by(Persona.id))).all())
    examples_count = await db.scalar(select(func.count()).select_from(Example))
    triggers = list((await db.scalars(select(TriggerRule).order_by(TriggerRule.id))).all())
    settings = await _read_settings(db)
    return {
        "accounts": len(accounts), "active_accounts": sum(a.enabled and not a.manual_pause for a in accounts),
        "paused_accounts": sum(a.manual_pause for a in accounts),
        "agents": len(agents), "personas": len(personas),
        "examples": int(examples_count or 0),
        "triggers": len(triggers), "enabled_triggers": sum(t.enabled for t in triggers),
        "global_ai_enabled": settings.get("ai_global_enabled", False),
    }


@router.get("/accounts")
async def accounts(_: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    rows = (await db.scalars(select(Account).order_by(Account.id))).all()
    return [_account(row) for row in rows]


@router.patch("/accounts/{account_id}")
async def update_account(account_id: int, payload: AccountPatch, actor: int = Depends(require_admin),
                         db: AsyncSession = Depends(get_session)):
    row = await db.get(Account, account_id)
    if not row:
        raise HTTPException(404, "Аккаунт не найден.")
    values = payload.model_dump(exclude_unset=True)
    if values.get("reply_mode") == "automatic" and row.reply_mode == "manual" and row.learning_status in {"training", "stopping"}:
        raise HTTPException(409, "Нельзя включить автоматические ответы во время обучения аккаунта.")
    if "ai_agent_id" in values and values["ai_agent_id"] is not None:
        if not await db.get(AIAgent, values["ai_agent_id"]):
            raise HTTPException(400, "ИИ-агент не найден.")
    if "persona_id" in values and values["persona_id"] is not None:
        if not await db.get(Persona, values["persona_id"]):
            raise HTTPException(400, "Персона не найдена.")
    for key, value in values.items():
        setattr(row, key, value)
    await _audit(db, actor, "account.update", "account", account_id)
    await db.commit()
    return _account(row)


@router.get("/agents")
async def agents(_: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    rows = (await db.scalars(select(AIAgent).order_by(AIAgent.id))).all()
    return [_agent(row) for row in rows]


@router.post("/agents")
async def create_agent(payload: AgentInput, actor: int = Depends(require_admin),
                       db: AsyncSession = Depends(get_session)):
    if await db.scalar(select(AIAgent.id).where(AIAgent.name == payload.name)):
        raise HTTPException(409, "Агент с таким названием уже существует.")
    values = payload.model_dump()
    row = AIAgent(**values)
    if row.is_default:
        for item in (await db.scalars(select(AIAgent))).all():
            item.is_default = False
    db.add(row)
    await db.flush()
    await _audit(db, actor, "agent.create", "agent", row.id)
    await db.commit()
    return _agent(row)


@router.patch("/agents/{agent_id}")
async def update_agent(agent_id: int, payload: AgentInput, actor: int = Depends(require_admin),
                       db: AsyncSession = Depends(get_session)):
    row = await db.get(AIAgent, agent_id)
    if not row:
        raise HTTPException(404, "ИИ-агент не найден.")
    values = payload.model_dump()
    duplicate = await db.scalar(select(AIAgent.id).where(AIAgent.name == payload.name, AIAgent.id != agent_id))
    if duplicate:
        raise HTTPException(409, "ИИ-агент с таким названием уже существует.")
    if not values["api_key"]:
        values.pop("api_key")
    if row.is_default or values["is_default"]:
        for item in (await db.scalars(select(AIAgent).where(AIAgent.id != agent_id))).all():
            item.is_default = False
    for key, value in values.items():
        setattr(row, key, value)
    await _audit(db, actor, "agent.update", "agent", agent_id)
    await db.commit()
    return _agent(row)


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: int, actor: int = Depends(require_admin),
                       db: AsyncSession = Depends(get_session)):
    row = await db.get(AIAgent, agent_id)
    if not row:
        raise HTTPException(404, "ИИ-агент не найден.")
    await db.delete(row)
    await _audit(db, actor, "agent.delete", "agent", agent_id)
    await db.commit()
    return {"ok": True}


@router.get("/personas")
async def personas(_: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    rows = (await db.scalars(select(Persona).order_by(Persona.id))).all()
    return [_persona(row) for row in rows]


@router.post("/personas")
async def create_persona(payload: PersonaInput, actor: int = Depends(require_admin),
                         db: AsyncSession = Depends(get_session)):
    if await db.scalar(select(Persona.id).where(Persona.name == payload.name)):
        raise HTTPException(409, "Персона с таким названием уже существует.")
    row = Persona(**payload.model_dump())
    db.add(row)
    await db.flush()
    await _audit(db, actor, "persona.create", "persona", row.id)
    await db.commit()
    return _persona(row)


@router.patch("/personas/{persona_id}")
async def update_persona(persona_id: int, payload: PersonaInput, actor: int = Depends(require_admin),
                         db: AsyncSession = Depends(get_session)):
    row = await db.get(Persona, persona_id)
    if not row:
        raise HTTPException(404, "Персона не найдена.")
    duplicate = await db.scalar(select(Persona.id).where(Persona.name == payload.name, Persona.id != persona_id))
    if duplicate:
        raise HTTPException(409, "Персона с таким названием уже существует.")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    await _audit(db, actor, "persona.update", "persona", persona_id)
    await db.commit()
    return _persona(row)


@router.delete("/personas/{persona_id}")
async def delete_persona(persona_id: int, actor: int = Depends(require_admin),
                         db: AsyncSession = Depends(get_session)):
    row = await db.get(Persona, persona_id)
    if not row:
        raise HTTPException(404, "Персона не найдена.")
    await db.delete(row)
    await _audit(db, actor, "persona.delete", "persona", persona_id)
    await db.commit()
    return {"ok": True}


@router.get("/examples")
async def examples(_: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    rows = (await db.scalars(select(Example).order_by(desc(Example.id)).limit(300))).all()
    return [_example(row) for row in rows]


@router.post("/examples")
async def create_example(payload: ExampleInput, actor: int = Depends(require_admin),
                         db: AsyncSession = Depends(get_session)):
    if not payload.consent_confirmed:
        raise HTTPException(400, "Подтвердите право использовать этот пример.")
    if payload.account_id is not None and not await db.get(Account, payload.account_id):
        raise HTTPException(400, "Аккаунт не найден.")
    if payload.persona_id is not None and not await db.get(Persona, payload.persona_id):
        raise HTTPException(400, "Персона не найдена.")
    row = Example(**payload.model_dump())
    db.add(row)
    await db.flush()
    await _audit(db, actor, "example.create", "example", row.id)
    await db.commit()
    return _example(row)


@router.delete("/examples/{example_id}")
async def delete_example(example_id: int, actor: int = Depends(require_admin),
                         db: AsyncSession = Depends(get_session)):
    row = await db.get(Example, example_id)
    if not row:
        raise HTTPException(404, "Пример не найден.")
    await db.delete(row)
    await _audit(db, actor, "example.delete", "example", example_id)
    await db.commit()
    return {"ok": True}


@router.get("/triggers")
async def triggers(_: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    rows = (await db.scalars(select(TriggerRule).order_by(TriggerRule.account_id, TriggerRule.id))).all()
    return [_trigger(row) for row in rows]


@router.post("/triggers")
async def create_trigger(payload: TriggerInput, actor: int = Depends(require_admin),
                         db: AsyncSession = Depends(get_session)):
    if payload.account_id is not None and not await db.get(Account, payload.account_id):
        raise HTTPException(400, "Аккаунт не найден.")
    from app.services.triggers import parse_trigger_terms
    parsed_terms = parse_trigger_terms(payload.terms)
    if not parsed_terms or len(parsed_terms) > 30:
        raise HTTPException(400, "Добавьте от 1 до 30 слов или фраз для триггера.")
    values = payload.model_dump()
    values["terms"] = "\n".join(parsed_terms)
    if values["account_id"] is not None:
        values["apply_mode"] = "account"
    row = TriggerRule(**values)
    db.add(row)
    await db.flush()
    await _audit(db, actor, "trigger.create", "trigger", row.id)
    await db.commit()
    return _trigger(row)


@router.patch("/triggers/{trigger_id}")
async def update_trigger(trigger_id: int, payload: TriggerInput, actor: int = Depends(require_admin),
                         db: AsyncSession = Depends(get_session)):
    row = await db.get(TriggerRule, trigger_id)
    if not row:
        raise HTTPException(404, "Триггер не найден.")
    if payload.account_id is not None and not await db.get(Account, payload.account_id):
        raise HTTPException(400, "Аккаунт не найден.")
    from app.services.triggers import parse_trigger_terms
    parsed_terms = parse_trigger_terms(payload.terms)
    if not parsed_terms or len(parsed_terms) > 30:
        raise HTTPException(400, "Добавьте от 1 до 30 слов или фраз для триггера.")
    values = payload.model_dump()
    values["terms"] = "\n".join(parsed_terms)
    if values["account_id"] is not None:
        values["apply_mode"] = "account"
    for key, value in values.items():
        setattr(row, key, value)
    await _audit(db, actor, "trigger.update", "trigger", trigger_id)
    await db.commit()
    return _trigger(row)


@router.delete("/triggers/{trigger_id}")
async def delete_trigger(trigger_id: int, actor: int = Depends(require_admin),
                         db: AsyncSession = Depends(get_session)):
    row = await db.get(TriggerRule, trigger_id)
    if not row:
        raise HTTPException(404, "Триггер не найден.")
    await db.delete(row)
    await _audit(db, actor, "trigger.delete", "trigger", trigger_id)
    await db.commit()
    return {"ok": True}


async def _read_settings(db: AsyncSession) -> dict[str, Any]:
    rows = (await db.scalars(select(AppSetting))).all()
    result: dict[str, Any] = {}
    for row in rows:
        if row.key == "ai_global_enabled":
            result[row.key] = row.value.lower() == "true"
        elif row.key in {"mention_reply_probability", "dm_reply_probability",
                         "proactive_reply_probability", "reply_cooldown_seconds"}:
            try:
                result[row.key] = int(row.value)
            except (TypeError, ValueError):
                result[row.key] = 0
    return result


@router.get("/settings")
async def get_global_settings(_: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    return await _read_settings(db)


@router.put("/settings")
async def update_global_settings(payload: SettingsInput, actor: int = Depends(require_admin),
                                 db: AsyncSession = Depends(get_session)):
    allowed = {"ai_global_enabled", "mention_reply_probability", "dm_reply_probability",
               "proactive_reply_probability", "reply_cooldown_seconds"}
    for key, value in payload.values.items():
        if key not in allowed:
            raise HTTPException(400, f"Настройка {key} не поддерживается.")
        if key == "ai_global_enabled":
            if not isinstance(value, bool):
                raise HTTPException(400, "Глобальный ИИ должен быть логическим значением.")
        elif not isinstance(value, int) or isinstance(value, bool):
            raise HTTPException(400, f"Некорректное значение {key}.")
        elif "probability" in key and not 0 <= value <= 100:
            raise HTTPException(400, "Вероятность должна быть от 0 до 100%.")
        elif "cooldown" in key and not 0 <= value <= 2147483647:
            raise HTTPException(400, "Интервал превышает допустимое значение.")
        row = await db.get(AppSetting, key)
        stored = str(value).lower() if isinstance(value, bool) else str(value)
        if row:
            row.value = stored
        else:
            db.add(AppSetting(key=key, value=stored))
    if payload.apply_to_agents == "all":
        agents = (await db.scalars(select(AIAgent))).all()
        for key, value in payload.values.items():
            if key in {"mention_reply_probability", "dm_reply_probability",
                       "proactive_reply_probability", "reply_cooldown_seconds"}:
                for agent in agents:
                    setattr(agent, key + "_override", int(value))
    await _audit(db, actor, "settings.update", "settings", "global")
    await db.commit()
    return await _read_settings(db)



LOG_CATEGORIES = {
    "main": "Основные логи",
    "replies": "Ответы ИИ",
    "errors": "Ошибки",
    "learning": "Обучение",
    "channels": "Каналы и обсуждения",
}


def _parse_log_destinations(value: str | None) -> list[tuple[int, int | None]]:
    result: list[tuple[int, int | None]] = []
    for raw in (value or "").replace(";", ",").split(","):
        parts = raw.strip().split(":")
        try:
            chat_id = int(parts[0])
            thread_id = int(parts[1]) if len(parts) == 2 else None
        except (TypeError, ValueError):
            continue
        if len(parts) > 2 or chat_id == 0 or (thread_id is not None and thread_id <= 0):
            continue
        item = (chat_id, thread_id)
        if item not in result:
            result.append(item)
    return result


def _format_log_destinations(items: list[tuple[int, int | None]]) -> str:
    return ",".join(f"{chat_id}:{thread_id}" if thread_id is not None else str(chat_id)
                    for chat_id, thread_id in items)


@router.get("/logs/destinations")
async def get_log_destinations(_: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    result = {}
    for category, label in LOG_CATEGORIES.items():
        row = await db.get(AppSetting, f"log_chat_ids_{category}")
        destinations = _parse_log_destinations(row.value if row else "")
        if category == "main" and row is None and get_settings().log_chat_id:
            destinations = [(get_settings().log_chat_id, None)]
        result[category] = {
            "label": label,
            "destinations": [{"chat_id": chat_id, "thread_id": thread_id}
                             for chat_id, thread_id in destinations],
        }
    return result


@router.post("/logs/destinations/{category}")
async def add_log_destination(category: str, payload: LogDestinationInput,
                               actor: int = Depends(require_admin),
                               db: AsyncSession = Depends(get_session)):
    if category not in LOG_CATEGORIES:
        raise HTTPException(404, "Категория логов не найдена.")
    cfg = get_settings()
    if not cfg.bot_token:
        raise HTTPException(503, "BOT_TOKEN не настроен.")
    bot = Bot(token=cfg.bot_token)
    try:
        kwargs = {"message_thread_id": payload.thread_id} if payload.thread_id is not None else {}
        await bot.send_message(payload.chat_id,
                               "Проверка связи: этот получатель добавлен для логов AtcivPerson.",
                               **kwargs)
    except Exception as exc:
        raise HTTPException(400, f"Не удалось отправить тестовое сообщение: {type(exc).__name__}: {str(exc)[:180]}") from None
    finally:
        await bot.session.close()
    key = f"log_chat_ids_{category}"
    row = await db.get(AppSetting, key)
    destinations = _parse_log_destinations(row.value if row else "")
    if category == "main" and row is None and cfg.log_chat_id:
        destinations = [(cfg.log_chat_id, None)]
    destination = (payload.chat_id, payload.thread_id)
    if destination not in destinations:
        destinations.append(destination)
    value = _format_log_destinations(destinations)
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    await _audit(db, actor, "logs.destination.add", "logs", f"{category}:{payload.chat_id}:{payload.thread_id or 0}")
    await db.commit()
    return {"ok": True, "destinations": [{"chat_id": chat_id, "thread_id": thread_id}
                                          for chat_id, thread_id in destinations]}


@router.delete("/logs/destinations/{category}")
async def remove_log_destination(category: str, chat_id: int, thread_id: int | None = None,
                                 actor: int = Depends(require_admin),
                                 db: AsyncSession = Depends(get_session)):
    if category not in LOG_CATEGORIES:
        raise HTTPException(404, "Категория логов не найдена.")
    key = f"log_chat_ids_{category}"
    row = await db.get(AppSetting, key)
    destinations = _parse_log_destinations(row.value if row else "")
    destinations = [item for item in destinations if item != (chat_id, thread_id)]
    value = _format_log_destinations(destinations)
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    await _audit(db, actor, "logs.destination.remove", "logs", f"{category}:{chat_id}:{thread_id or 0}")
    await db.commit()
    return {"ok": True, "destinations": [{"chat_id": target, "thread_id": thread}
                                          for target, thread in destinations]}



@router.get("/logs")
async def logs(_: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    rows = (await db.scalars(select(AuditEvent).order_by(desc(AuditEvent.id)).limit(200))).all()
    return [{"id": r.id, "actor": r.actor_telegram_id, "action": r.action,
             "object_type": r.object_type, "object_id": r.object_id,
             "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]




@router.get("/bindings")
async def bindings(_: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    chats = (await db.scalars(select(ChatBinding).order_by(ChatBinding.account_id, ChatBinding.title))).all()
    channels = (await db.scalars(select(ChannelBinding).order_by(ChannelBinding.account_id, ChannelBinding.title))).all()
    return {
        "groups": [{"id": r.id, "account_id": r.account_id, "peer_id": r.chat_id,
                    "title": r.title, "enabled": r.enabled} for r in chats],
        "channels": [{"id": r.id, "account_id": r.account_id, "peer_id": r.channel_id,
                      "title": r.title, "enabled": r.enabled} for r in channels],
    }



@router.delete("/bindings/{kind}/{binding_id}")
async def delete_binding(kind: str, binding_id: int,
                         actor: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    if kind not in {"group", "channel"}:
        raise HTTPException(404, "Тип площадки не найден.")
    model = ChatBinding if kind == "group" else ChannelBinding
    row = await db.get(model, binding_id)
    if not row:
        raise HTTPException(404, "Сохранённая площадка не найдена.")
    await db.delete(row)
    await _audit(db, actor, "binding.delete", kind, binding_id)
    await db.commit()
    return {"ok": True, "left_chat": False}


@router.patch("/bindings/{kind}/{binding_id}")
async def update_binding(kind: str, binding_id: int, enabled: bool,
                         actor: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    model = ChatBinding if kind == "group" else ChannelBinding if kind == "channel" else None
    if model is None:
        raise HTTPException(400, "Неизвестный тип привязки.")
    row = await db.get(model, binding_id)
    if not row:
        raise HTTPException(404, "Привязка не найдена.")
    row.enabled = enabled
    await _audit(db, actor, f"{kind}.binding.update", kind, binding_id)
    await db.commit()
    return {"ok": True, "enabled": row.enabled}


@router.post("/bindings")
async def create_binding(payload: BindingInput, actor: int = Depends(require_admin),
                          db: AsyncSession = Depends(get_session)):
    if not await db.get(Account, payload.account_id):
        raise HTTPException(404, "Аккаунт не найден.")
    if payload.kind == "group":
        row = await db.scalar(select(ChatBinding).where(
            ChatBinding.account_id == payload.account_id, ChatBinding.chat_id == payload.peer_id
        ))
        if row:
            return {"ok": True, "existing": True, "id": row.id}
        row = ChatBinding(account_id=payload.account_id, chat_id=payload.peer_id,
                          title=payload.title, enabled=False)
    else:
        row = await db.scalar(select(ChannelBinding).where(
            ChannelBinding.account_id == payload.account_id, ChannelBinding.channel_id == payload.peer_id
        ))
        if row:
            return {"ok": True, "existing": True, "id": row.id}
        row = ChannelBinding(account_id=payload.account_id, channel_id=payload.peer_id,
                             title=payload.title, enabled=False)
    db.add(row)
    await db.flush()
    await _audit(db, actor, f"{payload.kind}.binding.create", payload.kind, row.id)
    await db.commit()
    return {"ok": True, "id": row.id, "enabled": False}






async def _resolve_inbox_entity(client, peer_id: int, peer_ref: str = ""):
    from telethon import utils

    entity = await client.get_entity(peer_ref if peer_ref else int(peer_id))
    if int(utils.get_peer_id(entity)) != int(peer_id):
        raise HTTPException(409, "Идентификатор диалога изменился. Обновите список диалогов.")
    return entity


async def _inbox_recent_messages(client, entity, limit: int = 30):
    messages = []
    async for message in client.iter_messages(entity, limit=limit):
        messages.append({
            "id": int(message.id),
            "text": (message.message or "")[:5000],
            "date": message.date.isoformat() if message.date else None,
            "out": bool(message.out),
            "sender_id": int(message.sender_id) if message.sender_id else None,
            "has_media": bool(message.media),
        })
    messages.reverse()
    return messages


@router.get("/inbox/{account_id}/dialogs")
async def inbox_dialogs(account_id: int, _: int = Depends(require_admin)):
    from app.telegram_gateway.management import isolated_account_client

    try:
        async with isolated_account_client(account_id) as (account, client):
            if not account.enabled:
                raise HTTPException(400, "Аккаунт отключён.")
            rows = []
            async for dialog in client.iter_dialogs(limit=80):
                entity = dialog.entity
                username = getattr(entity, "username", None)
                marked_id = int(dialog.id)
                kind = (
                    "channel" if getattr(entity, "broadcast", False) else
                    "group" if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False)
                    or entity.__class__.__name__ == "Chat" else "private"
                )
                last = dialog.message
                rows.append({
                    "peer_id": marked_id,
                    "peer_ref": ("@" + username) if username else "",
                    "title": str(dialog.name or "Без названия")[:160],
                    "username": username,
                    "kind": kind,
                    "unread_count": int(dialog.unread_count or 0),
                    "last_message_id": int(last.id) if last else None,
                    "last_message": ((last.message or "")[:240] if last else ""),
                    "last_message_out": bool(last.out) if last else False,
                    "last_message_at": last.date.isoformat() if last and last.date else None,
                })
            return {"account_id": account_id, "dialogs": rows}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Не удалось загрузить диалоги: {type(exc).__name__}: {str(exc)[:180]}") from None


@router.get("/inbox/{account_id}/messages/{peer_id}")
async def inbox_messages(account_id: int, peer_id: int, peer_ref: str = "",
                         _: int = Depends(require_admin)):
    from app.telegram_gateway.management import isolated_account_client

    try:
        async with isolated_account_client(account_id) as (_account, client):
            entity = await _resolve_inbox_entity(client, peer_id, peer_ref)
            if getattr(entity, "broadcast", False) and not getattr(entity, "megagroup", False):
                raise HTTPException(400, "Просмотр сообщений вещательного канала здесь не поддерживается.")
            messages = await _inbox_recent_messages(client, entity, limit=40)
            return {"account_id": account_id, "peer_id": peer_id, "messages": messages}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Не удалось загрузить переписку: {type(exc).__name__}: {str(exc)[:180]}") from None


@router.post("/inbox/draft")
async def inbox_ai_draft(payload: InboxPeerInput, _: int = Depends(require_admin),
                         db: AsyncSession = Depends(get_session)):
    from app.telegram_gateway.management import isolated_account_client
    from app.ai.provider import generate_reply
    from telethon.tl.types import User

    account = await db.get(Account, payload.account_id)
    if not account or not account.enabled:
        raise HTTPException(404, "Аккаунт не найден или отключён.")
    agent = await db.get(AIAgent, account.ai_agent_id) if account.ai_agent_id else None
    if not agent:
        agent = await db.scalar(select(AIAgent).where(
            AIAgent.enabled.is_(True), AIAgent.is_default.is_(True)
        ).order_by(AIAgent.id))
    if not agent or not agent.enabled or not agent.api_key.strip():
        raise HTTPException(400, "Назначьте активного ИИ-агента с API-ключом.")
    persona = await db.get(Persona, account.persona_id) if account.persona_id else None
    examples = list((await db.scalars(select(Example).where(
        Example.consent_confirmed.is_(True),
        or_(Example.account_id == account.id, Example.account_id.is_(None)),
        or_(Example.persona_id == account.persona_id, Example.persona_id.is_(None))
        if account.persona_id else Example.persona_id.is_(None),
    ).order_by(desc(Example.id)).limit(8))).all())
    style = persona.style_instructions if persona else ""
    if account.persona_style_override.strip():
        style += "\n\nИндивидуальные правила аккаунта:\n" + account.persona_style_override[:6000]
    if account.learning_prompt.strip():
        style += "\n\nКонтекст стиля аккаунта:\n" + account.learning_prompt[:6000]
    style += "\n\nЕсли собеседник спрашивает, является ли ответ автоматическим, отвечай честно."

    # Rollback releases the request's DB transaction before Telegram/AI network calls.
    # AsyncSession.rollback() expires ORM instances, so copy every value used below
    # into plain Python data before releasing the session.
    agent_config = _snapshot_agent(agent)
    approved_examples = [
        (str(item.incoming_text), str(item.preferred_reply))
        for item in examples
    ]
    await db.rollback()
    try:
        async with isolated_account_client(payload.account_id) as (_, client):
            entity = await _resolve_inbox_entity(client, payload.peer_id, payload.peer_ref)
            if getattr(entity, "broadcast", False) and not getattr(entity, "megagroup", False):
                raise HTTPException(400, "Для вещательных каналов нельзя создавать ответ в личном inbox.")
            if not isinstance(entity, User):
                async with SessionFactory() as check_db:
                    binding = await check_db.scalar(select(ChatBinding).where(
                        ChatBinding.account_id == payload.account_id,
                        ChatBinding.chat_id == payload.peer_id,
                        ChatBinding.enabled.is_(True),
                    ))
                if not binding:
                    raise HTTPException(403, "Для ответа в группе сначала сохраните и включите её в своей базе.")
            raw = []
            async for item in client.iter_messages(entity, limit=15):
                raw.append(item)
            raw.reverse()
            inbound = raw[-1] if raw else None
            if inbound is None or inbound.out:
                raise HTTPException(409, "Последнее сообщение уже отправлено аккаунтом. Обновите диалог после нового входящего сообщения.")
            context = _format_inbox_context(raw)
            draft = await generate_reply(
                agent=agent_config,
                incoming_text=(inbound.message or "Собеседник отправил медиа или вложение."),
                style_instructions=(
                    "Подготовь уместный ответ на последнее входящее сообщение в реальном диалоге. "
                    "Учитывай всю историю, не выдумывай факты и не отправляй сообщение самостоятельно. "
                    "Не используй навязчивые продажи. " + style
                ),
                examples=approved_examples,
                conversation_context=context,
            )
            return {"draft": draft, "reply_to_message_id": int(inbound.id)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Не удалось подготовить ответ: {type(exc).__name__}: {str(exc)[:180]}") from None


@router.post("/inbox/reply")
async def inbox_send_reply(payload: InboxReplyInput, actor: int = Depends(require_admin),
                           db: AsyncSession = Depends(get_session)):
    from app.telegram_gateway.management import isolated_account_client
    from telethon.errors import FloodWaitError
    from telethon.tl.types import User

    account = await db.get(Account, payload.account_id)
    if not account or not account.enabled:
        raise HTTPException(404, "Аккаунт не найден или отключён.")
    if account.manual_pause:
        raise HTTPException(409, "Аккаунт на паузе. Снимите паузу перед отправкой.")
    await db.rollback()
    try:
        async with isolated_account_client(payload.account_id) as (live_account, client):
            if not live_account.enabled or live_account.manual_pause:
                raise HTTPException(409, "Аккаунт отключён или находится на паузе.")
            entity = await _resolve_inbox_entity(client, payload.peer_id, payload.peer_ref)
            if getattr(entity, "broadcast", False) and not getattr(entity, "megagroup", False):
                raise HTTPException(400, "Нельзя отправлять ответы в вещательный канал.")
            if not isinstance(entity, User):
                async with SessionFactory() as check_db:
                    binding = await check_db.scalar(select(ChatBinding).where(
                        ChatBinding.account_id == payload.account_id,
                        ChatBinding.chat_id == payload.peer_id,
                        ChatBinding.enabled.is_(True),
                    ))
                if not binding:
                    raise HTTPException(403, "Ответ в группе разрешён только для включённой сохранённой группы.")
            recent = []
            async for item in client.iter_messages(entity, limit=30):
                recent.append(item)
            latest = recent[0] if recent else None
            target = next((item for item in recent if int(item.id) == payload.reply_to_message_id), None)
            if target is None or target.out or latest is None or int(latest.id) != payload.reply_to_message_id or latest.out:
                raise HTTPException(409, "Ответ можно отправить только на последнее входящее сообщение. Обновите переписку.")
            sent = await client.send_message(
                entity, payload.text.strip(), reply_to=payload.reply_to_message_id,
                link_preview=False,
            )
        async with SessionFactory() as audit_db:
            await _audit(audit_db, actor, "inbox.reply.send", "telegram_message", int(sent.id))
            await audit_db.commit()
        return {"ok": True, "message_id": int(sent.id), "reply_to_message_id": payload.reply_to_message_id}
    except FloodWaitError as exc:
        raise HTTPException(429, f"Telegram просит подождать {int(exc.seconds)} сек. Повтор не запускается автоматически.") from None
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Не удалось отправить ответ: {type(exc).__name__}: {str(exc)[:180]}") from None


@router.get("/channel-assistant")
async def get_channel_assistant(_: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    rows = list((await db.scalars(
        select(ChannelBinding).order_by(ChannelBinding.account_id, ChannelBinding.title)
    )).all())
    return [{
        "id": row.id, "account_id": row.account_id, "channel_id": row.channel_id,
        "title": row.title, "discussion_group_id": row.discussion_group_id,
        "enabled": row.enabled, "reply_mode_override": row.reply_mode_override,
        "chat_replies_enabled_override": row.chat_replies_enabled_override,
        "post_instructions_override": row.post_instructions_override or "",
        "comments_instructions_override": row.comments_instructions_override or "",
        "restrictions_instructions_override": row.restrictions_instructions_override or "",
        "reply_probability_override": row.reply_probability_override,
        "reply_cooldown_seconds_override": row.reply_cooldown_seconds_override,
        "learning_status": row.learning_status,
    } for row in rows]


@router.patch("/channel-assistant/{binding_id}")
async def update_channel_assistant(binding_id: int, payload: ChannelAssistantPatch,
                                   actor: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    row = await db.get(ChannelBinding, binding_id)
    if not row:
        raise HTTPException(404, "Канал не найден.")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    await _audit(db, actor, "channel.assistant.update", "channel", binding_id)
    await db.commit()
    return {"ok": True, "id": row.id, "enabled": row.enabled}


@router.get("/channel-activity")
async def get_channel_activity(_: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    rows = list((await db.scalars(
        select(ChannelAnalysis).order_by(desc(ChannelAnalysis.created_at)).limit(60)
    )).all())
    return [{
        "id": row.id, "account_id": row.account_id, "channel_id": row.channel_id,
        "channel_title": row.channel_title, "post_id": row.post_id,
        "post_text": row.post_text[:1200], "comments_summary": row.comments_summary[:1200],
        "suggested_response": row.suggested_response[:2000], "on_topic": row.on_topic,
        "publication_status": row.publication_status, "publication_error": row.publication_error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    } for row in rows]


@router.get("/content/drafts")
async def get_content_drafts(_: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    rows = list((await db.scalars(
        select(ContentDraft).order_by(desc(ContentDraft.updated_at)).limit(100)
    )).all())
    return [{
        "id": row.id, "account_id": row.account_id, "agent_id": row.agent_id,
        "persona_id": row.persona_id, "channel_binding_id": row.channel_binding_id,
        "title": row.title, "prompt": row.prompt, "body": row.body,
        "status": row.status, "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    } for row in rows]


@router.post("/content/generate")
async def generate_content_draft(payload: ContentGenerateInput, actor: int = Depends(require_admin),
                                 db: AsyncSession = Depends(get_session)):
    account = await db.get(Account, payload.account_id) if payload.account_id else None
    if payload.account_id and not account:
        raise HTTPException(404, "Аккаунт не найден.")
    channel = await db.get(ChannelBinding, payload.channel_binding_id) if payload.channel_binding_id else None
    if payload.channel_binding_id and not channel:
        raise HTTPException(404, "Канал не найден.")
    if channel and account and channel.account_id != account.id:
        raise HTTPException(400, "Выбранный канал не принадлежит выбранному аккаунту.")
    if channel and not account:
        account = await db.get(Account, channel.account_id)
    agent_id = payload.agent_id or (account.ai_agent_id if account else None)
    agent = await db.get(AIAgent, agent_id) if agent_id else None
    if agent_id and agent is None:
        raise HTTPException(404, "ИИ-агент не найден.")
    if agent is None:
        agent = await db.scalar(select(AIAgent).where(
            AIAgent.enabled.is_(True), AIAgent.is_default.is_(True)
        ).order_by(AIAgent.id))
    if agent is None:
        agent = await db.scalar(select(AIAgent).where(
            AIAgent.enabled.is_(True)
        ).order_by(AIAgent.id))
    if not agent or not agent.enabled or not agent.api_key.strip() or not agent.base_url.strip() or not agent.model.strip():
        raise HTTPException(400, "Выберите активного ИИ-агента и заполните его API-ключ, адрес и модель.")
    persona_id = payload.persona_id if payload.persona_id is not None else (account.persona_id if account else None)
    persona = await db.get(Persona, persona_id) if persona_id else None
    if persona_id and persona is None:
        raise HTTPException(404, "Персона не найдена.")
    query = select(Example).where(Example.consent_confirmed.is_(True))
    if account:
        query = query.where(or_(Example.account_id == account.id, Example.account_id.is_(None)))
    else:
        query = query.where(Example.account_id.is_(None))
    if persona_id:
        query = query.where(or_(Example.persona_id == persona_id, Example.persona_id.is_(None)))
    else:
        query = query.where(Example.persona_id.is_(None))
    examples = list((await db.scalars(query.order_by(desc(Example.id)).limit(8))).all())
    style = persona.style_instructions if persona else ""
    if account and account.persona_style_override.strip():
        style += "\n\nИндивидуальные правила аккаунта:\n" + account.persona_style_override[:6000]
    if account and account.learning_prompt.strip():
        style += "\n\nКонтекст стиля аккаунта:\n" + account.learning_prompt[:6000]
    # Snapshot everything needed for generation, then release the DB connection
    # before waiting on the external AI provider.
    account_id = int(account.id) if account else None
    channel_binding_id = int(channel.id) if channel else None
    selected_agent_id = int(agent.id)
    selected_persona_id = int(persona.id) if persona else None
    agent_config = _snapshot_agent(agent)
    approved_examples = [
        (str(item.incoming_text), str(item.preferred_reply))
        for item in examples
    ]
    await db.rollback()

    from app.ai.provider import generate_reply
    try:
        body = await generate_reply(
            agent=agent_config,
            incoming_text=payload.brief,
            style_instructions=(
                "Ты редактор контента для официального Telegram-канала. Создай качественный, "
                "понятный и естественный черновик публикации по заданию. Не выдумывай факты, "
                "цифры, ссылки, отзывы или обещания. Если данных недостаточно, формулируй осторожно. "
                "Не добавляй хештеги без необходимости. " + style
            ),
            examples=approved_examples,
        )
    except Exception as exc:
        raise HTTPException(502, f"Не удалось создать черновик: {type(exc).__name__}: {str(exc)[:220]}") from None
    row = ContentDraft(
        account_id=account_id, agent_id=selected_agent_id,
        persona_id=selected_persona_id,
        channel_binding_id=channel_binding_id,
        title=payload.title.strip(), prompt=payload.brief.strip(), body=body,
        status="draft",
    )
    db.add(row)
    await db.flush()
    await _audit(db, actor, "content.draft.generate", "content_draft", row.id)
    await db.commit()
    return {
        "id": row.id, "account_id": row.account_id, "agent_id": row.agent_id,
        "persona_id": row.persona_id, "channel_binding_id": row.channel_binding_id,
        "title": row.title, "prompt": row.prompt, "body": row.body, "status": row.status,
        "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat(),
    }


@router.patch("/content/drafts/{draft_id}")
async def update_content_draft(draft_id: int, payload: ContentDraftPatch,
                                actor: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    row = await db.get(ContentDraft, draft_id)
    if not row:
        raise HTTPException(404, "Черновик не найден.")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    await _audit(db, actor, "content.draft.update", "content_draft", draft_id)
    await db.commit()
    return {"ok": True, "id": row.id, "status": row.status}


@router.delete("/content/drafts/{draft_id}")
async def delete_content_draft(draft_id: int, actor: int = Depends(require_admin),
                               db: AsyncSession = Depends(get_session)):
    row = await db.get(ContentDraft, draft_id)
    if not row:
        raise HTTPException(404, "Черновик не найден.")
    await db.delete(row)
    await _audit(db, actor, "content.draft.delete", "content_draft", draft_id)
    await db.commit()
    return {"ok": True}


@router.post("/search/groups")
async def search_groups(payload: SearchInput, actor: int = Depends(require_admin)):
    from app.telegram_gateway.discovery import search_public_groups, search_web_groups, verify_group_candidates
    from app.telegram_gateway.management import isolated_account_client
    from telethon import utils
    async with isolated_account_client(payload.account_id) as (_account, client):
        telegram = await search_public_groups(client, payload.query, limit=payload.limit)
        web_candidates = await search_web_groups(payload.query, limit=min(payload.limit * 3, 120), pages_per_query=1)
        verified = await verify_group_candidates(client, web_candidates[:payload.limit * 2])
        merged = {str(item.get("link", "")).casefold(): item for item in [*telegram, *verified]}
        results = sorted(merged.values(), key=lambda item: (int(item.get("score") or 0), int(item.get("members") or 0)), reverse=True)
        from telethon.tl.types import PeerChannel, PeerChat
        for item in results:
            raw_id = item.get("id")
            if raw_id is None:
                item["id"] = None
            else:
                peer = PeerChannel(int(raw_id)) if item.get("kind") == "supergroup" else PeerChat(int(raw_id))
                item["id"] = int(utils.get_peer_id(peer))
    return {"results": results[:payload.limit], "count": len(results)}


@router.post("/search/channels")
async def search_channels(payload: SearchInput, actor: int = Depends(require_admin)):
    from app.telegram_gateway.discovery import search_public_channels, search_web_channels, verify_channel_candidates
    from app.telegram_gateway.management import isolated_account_client
    from telethon import utils
    async with isolated_account_client(payload.account_id) as (_account, client):
        telegram = await search_public_channels(client, payload.query, limit=payload.limit)
        web_candidates = await search_web_channels(payload.query, limit=min(payload.limit * 3, 120), pages_per_query=1)
        verified = await verify_channel_candidates(client, web_candidates[:payload.limit * 2])
        merged = {str(item.get("link", "")).casefold(): item for item in [*telegram, *verified]}
        results = sorted(merged.values(), key=lambda item: (int(item.get("score") or 0), int(item.get("members") or 0)), reverse=True)
        from telethon.tl.types import PeerChannel
        for item in results:
            raw_id = item.get("id")
            item["id"] = int(utils.get_peer_id(PeerChannel(int(raw_id)))) if raw_id is not None else None
    return {"results": results[:payload.limit], "count": len(results)}
