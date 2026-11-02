from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Literal
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.settings import get_settings
from app.storage.db import get_session
from app.storage.models import (
    Account, AIAgent, AppSetting, AuditEvent, ChannelBinding, ChatBinding,
    Example, Persona, TriggerRule,
)

router = APIRouter(prefix="/v1/miniapp", tags=["miniapp"])


def _telegram_user_id(init_data: str | None) -> int:
    cfg = get_settings()
    if not cfg.bot_token or not init_data:
        raise HTTPException(status_code=401, detail="Откройте Mini App через Telegram.")
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
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
        "reply_mode": row.reply_mode, "learning_status": row.learning_status,
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
    channel_reply_cooldown_seconds_override: int | None = Field(default=None, ge=0, le=604800)


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
    values["terms"] = "\\n".join(parsed_terms)
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
    values["terms"] = "\\n".join(parsed_terms)
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
        for item in results:
            if item.get("username"):
                entity = await client.get_entity(item["username"])
                item["id"] = int(utils.get_peer_id(entity))
            else:
                item["id"] = None
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
        for item in results:
            if item.get("username"):
                entity = await client.get_entity(item["username"])
                item["id"] = int(utils.get_peer_id(entity))
            else:
                item["id"] = None
    return {"results": results[:payload.limit], "count": len(results)}
