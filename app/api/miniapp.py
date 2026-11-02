from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Literal
from urllib.parse import parse_qsl

from aiogram import Bot
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.settings import get_settings
from app.storage.db import SessionFactory, get_session
from app.storage.models import (
    Account, AIAgent, AppSetting, AuditEvent, ChannelBinding, ChatBinding,
    Example, Persona, TriggerRule, AutomationTask, AutomationTaskRecipient, AutomationSchedule,
)
from app.services import automation as telegram_automation

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


class ModulePolicyInput(BaseModel):
    values: dict[str, int]


class ParserSettingsInput(BaseModel):
    values: dict[str, Any]


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



class CampaignStatusInput(BaseModel):
    status: str = Field(pattern="^(paused|cancelled|queued)$")


class ScheduleStatusInput(BaseModel):
    status: str = Field(pattern="^(active|paused|cancelled)$")


@router.get("/campaigns")
async def get_campaigns(account_id: int | None = None, _: int = Depends(require_admin),
                        db: AsyncSession = Depends(get_session)):
    query = select(AutomationTask).order_by(desc(AutomationTask.id)).limit(100)
    if account_id is not None:
        if not await db.get(Account, account_id):
            raise HTTPException(404, "Аккаунт не найден.")
        query = select(AutomationTask).where(AutomationTask.account_id == account_id).order_by(desc(AutomationTask.id)).limit(100)
    tasks = list((await db.scalars(query)).all())
    task_ids = [row.id for row in tasks]
    counts = {}
    if task_ids:
        rows = (await db.execute(
            select(AutomationTaskRecipient.task_id, AutomationTaskRecipient.status, func.count())
            .where(AutomationTaskRecipient.task_id.in_(task_ids))
            .group_by(AutomationTaskRecipient.task_id, AutomationTaskRecipient.status)
        )).all()
        for task_id, status, count in rows:
            counts.setdefault(int(task_id), {})[str(status)] = int(count)
    return [{
        "id": row.id, "account_id": row.account_id, "kind": row.kind, "status": row.status,
        "scheduled_at": row.scheduled_at.isoformat() if row.scheduled_at else None,
        "last_error": row.last_error, "recipients": counts.get(row.id, {}),
    } for row in tasks]


@router.get("/campaigns/{task_id}")
async def get_campaign(task_id: int, _: int = Depends(require_admin)):
    try:
        return await telegram_automation.campaign_status(task_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None


@router.patch("/campaigns/{task_id}")
async def update_campaign(task_id: int, payload: CampaignStatusInput,
                          actor: int = Depends(require_admin)):
    try:
        await telegram_automation.set_campaign_status(task_id, payload.status)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    async with SessionFactory() as db:
        await _audit(db, actor, "campaign.status", "campaign", f"{task_id}:{payload.status}")
        await db.commit()
    return {"ok": True}


@router.post("/campaigns/{task_id}/retry")
async def retry_campaign(task_id: int, actor: int = Depends(require_admin)):
    try:
        count = await telegram_automation.retry_failed_campaign(task_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    async with SessionFactory() as db:
        await _audit(db, actor, "campaign.retry", "campaign", task_id)
        await db.commit()
    return {"ok": True, "recipients": count}


@router.get("/schedules")
async def get_schedules(account_id: int | None = None, _: int = Depends(require_admin),
                        db: AsyncSession = Depends(get_session)):
    query = select(AutomationSchedule).order_by(desc(AutomationSchedule.id)).limit(100)
    if account_id is not None:
        if not await db.get(Account, account_id):
            raise HTTPException(404, "Аккаунт не найден.")
        query = select(AutomationSchedule).where(AutomationSchedule.account_id == account_id).order_by(desc(AutomationSchedule.id)).limit(100)
    rows = list((await db.scalars(query)).all())
    return [{
        "id": row.id, "account_id": row.account_id, "kind": row.kind, "status": row.status,
        "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
        "interval_seconds": row.interval_seconds, "runs_count": row.runs_count,
        "max_runs": row.max_runs, "last_error": row.last_error, "last_task_id": row.last_task_id,
    } for row in rows]


@router.patch("/schedules/{schedule_id}")
async def update_schedule(schedule_id: int, payload: ScheduleStatusInput,
                          actor: int = Depends(require_admin)):
    try:
        await telegram_automation.set_schedule_status(schedule_id, payload.status)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    async with SessionFactory() as db:
        await _audit(db, actor, "schedule.status", "schedule", f"{schedule_id}:{payload.status}")
        await db.commit()
    return {"ok": True}


@router.get("/logs")
async def logs(_: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    rows = (await db.scalars(select(AuditEvent).order_by(desc(AuditEvent.id)).limit(200))).all()
    return [{"id": r.id, "actor": r.actor_telegram_id, "action": r.action,
             "object_type": r.object_type, "object_id": r.object_id,
             "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]




PARSER_SETTING_DEFAULTS = {
    "participants": {
        "limit": 200, "exclude_bots": False, "exclude_deleted": False,
        "exclude_scam": False, "exclude_blocked": False, "username_only": False,
        "photo_only": False, "premium_only": False, "active_days": 0,
    },
    "messages": {
        "limit": 100, "days": 0, "keywords": "",
        "message_delay_seconds": 0, "chat_delay_seconds": 0,
        "exclude_bots": False, "exclude_deleted": False,
        "exclude_scam": False, "exclude_blocked": False, "username_only": False,
        "photo_only": False, "premium_only": False, "active_days": 0,
    },
    "comments": {
        "post_limit": 100, "comment_limit": 100, "days": 0,
        "keywords": "", "comment_delay_seconds": 0,
        "exclude_bots": False, "exclude_deleted": False,
        "exclude_scam": False, "exclude_blocked": False, "username_only": False,
        "photo_only": False, "premium_only": False, "active_days": 0,
    },
}
PARSER_FILTERS = {
    "exclude_bots", "exclude_deleted", "exclude_scam", "exclude_blocked",
    "username_only", "photo_only", "premium_only",
}
PARSER_NUMERIC_LIMITS = {
    "limit": (1, 500), "post_limit": (1, 500), "comment_limit": (1, 500),
    "days": (0, 365), "message_delay_seconds": (0, 9999),
    "chat_delay_seconds": (0, 9999), "comment_delay_seconds": (0, 9999),
}


async def _parser_settings(db: AsyncSession, account_id: int, kind: str) -> dict[str, Any]:
    defaults = PARSER_SETTING_DEFAULTS[kind]
    prefix = f"parser.settings.{account_id}.{kind}."
    rows = (await db.scalars(select(AppSetting).where(AppSetting.key.startswith(prefix)))).all()
    raw = {row.key[len(prefix):]: row.value for row in rows}
    values = dict(defaults)
    for key, default in defaults.items():
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(default, bool):
            values[key] = value.lower() in {"1", "true", "yes", "on", "да"}
        elif isinstance(default, int):
            try:
                values[key] = int(value)
            except (TypeError, ValueError):
                pass
        else:
            values[key] = value
    return values


@router.get("/parser-settings/{account_id}/{kind}")
async def get_parser_settings(account_id: int, kind: str,
                              _: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    if kind not in PARSER_SETTING_DEFAULTS:
        raise HTTPException(404, "Раздел парсера не найден.")
    if not await db.get(Account, account_id):
        raise HTTPException(404, "Аккаунт не найден.")
    return await _parser_settings(db, account_id, kind)


@router.put("/parser-settings/{account_id}/{kind}")
async def update_parser_settings(account_id: int, kind: str, payload: ParserSettingsInput,
                                 actor: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    if kind not in PARSER_SETTING_DEFAULTS:
        raise HTTPException(404, "Раздел парсера не найден.")
    if not await db.get(Account, account_id):
        raise HTTPException(404, "Аккаунт не найден.")
    defaults = PARSER_SETTING_DEFAULTS[kind]
    if not payload.values or set(payload.values) - set(defaults):
        raise HTTPException(400, "Переданы неизвестные параметры парсера.")
    for key, value in payload.values.items():
        if key in PARSER_FILTERS:
            if not isinstance(value, bool):
                raise HTTPException(400, f"Параметр {key} должен быть логическим.")
        elif key == "keywords":
            if not isinstance(value, str) or len(value) > 1000:
                raise HTTPException(400, "Ключевые слова должны быть строкой до 1000 символов.")
        elif key == "active_days":
            if not isinstance(value, int) or isinstance(value, bool) or value not in {0, 3, 7, 30}:
                raise HTTPException(400, "Период активности: 0, 3, 7 или 30 дней.")
        else:
            bounds = PARSER_NUMERIC_LIMITS.get(key)
            if not bounds or not isinstance(value, int) or isinstance(value, bool) or not bounds[0] <= value <= bounds[1]:
                raise HTTPException(400, f"Некорректное значение параметра {key}.")
        setting_key = f"parser.settings.{account_id}.{kind}.{key}"
        row = await db.get(AppSetting, setting_key)
        stored = str(value).lower() if isinstance(value, bool) else str(value)
        if row:
            row.value = stored
        else:
            db.add(AppSetting(key=setting_key, value=stored))
    await _audit(db, actor, "parser.settings.update", "parser", f"{account_id}:{kind}")
    await db.commit()
    return await _parser_settings(db, account_id, kind)



class ParserRunInput(BaseModel):
    target: str = Field(min_length=2, max_length=255)


@router.post("/parser/run/{account_id}/{kind}")
async def run_parser(account_id: int, kind: str, payload: ParserRunInput,
                     actor: int = Depends(require_admin), db: AsyncSession = Depends(get_session)):
    if kind not in PARSER_SETTING_DEFAULTS:
        raise HTTPException(404, "Раздел парсера не найден.")
    if not await db.get(Account, account_id):
        raise HTTPException(404, "Аккаунт не найден.")
    settings = await _parser_settings(db, account_id, kind)
    import re
    keywords = [part.strip() for part in re.split(r"[,;\\n]", settings.get("keywords", "")) if part.strip()]
    filters = {key: settings[key] for key in (
        "exclude_bots", "exclude_deleted", "exclude_scam", "exclude_blocked",
        "username_only", "photo_only", "premium_only", "active_days",
    )}
    from app.telegram_gateway import management as telegram_management
    target = payload.target.strip()
    try:
        if kind == "participants":
            result = await telegram_management.parse_chat_participants(
                account_id, target, limit=settings["limit"], **filters,
            )
            items = result.pop("participants", [])
            summary = {
                "title": result["title"], "scanned": result["scanned_count"],
                "matched": result["visible_count"], "activity_unknown": result["activity_unknown_count"],
            }
        elif kind == "messages":
            if target.casefold() in {"all", "все", "все чаты"}:
                result = await telegram_management.parse_account_history(
                    account_id, limit_per_chat=settings["limit"], days=settings["days"],
                    keywords=keywords, message_delay_seconds=settings["message_delay_seconds"],
                    chat_delay_seconds=settings["chat_delay_seconds"], max_chats=100,
                    **filters,
                )
                items = [
                    {"chat": item.get("title", ""), "chat_id": item.get("channel_id"),
                     **post}
                    for item in result["results"] for post in item.get("posts", [])
                ]
                summary = {
                    "title": "История аккаунта", "scanned": result["messages_count"],
                    "matched": len(items), "chats_scanned": result["chats_scanned"],
                    "chats_parsed": result["chats_parsed"], "errors": len(result["errors"]),
                }
            else:
                result = await telegram_management.parse_chat_history(
                    account_id, target, limit=settings["limit"], days=settings["days"],
                    keywords=keywords, message_delay_seconds=settings["message_delay_seconds"],
                    **filters,
                )
                items = result["posts"]
                summary = {"title": result["title"], "scanned": result["scanned"], "matched": len(items)}
        else:
            result = await telegram_management.parse_chat_comments(
                account_id, target, post_limit=settings["post_limit"],
                comment_limit=settings["comment_limit"], days=settings["days"],
                keywords=keywords, comment_delay_seconds=settings["comment_delay_seconds"],
                **filters,
            )
            items = result["comments"]
            summary = {
                "title": result["title"], "scanned_posts": result["scanned_posts"],
                "scanned_comments": result["scanned_comments"], "matched": result["visible_count"],
            }
    except Exception as exc:
        raise HTTPException(400, f"Парсер не смог завершить задачу: {type(exc).__name__}: {str(exc)[:220]}") from None
    await _audit(db, actor, "parser.run", "parser", f"{account_id}:{kind}")
    await db.commit()
    return {"account_id": account_id, "kind": kind, "summary": summary, "items": items[:500]}


MODULE_LABELS = {
    "invite": "Инвайтинг",
    "broadcast": "Рассылки",
    "subscription": "Подписка и отписка",
    "reaction": "Реакции",
}
MODULE_FIELD_LABELS = {
    "interval_seconds": "Интервал между действиями, сек.",
    "daily_limit": "Лимит в сутки",
    "batch_size": "Действий за пакет",
    "batch_pause_seconds": "Пауза между пакетами, сек.",
    "per_account_limit": "Лимит на аккаунт",
    "daily_account_limit": "Лимит в сутки на аккаунт",
    "floodwait_wait_seconds": "Ждать при FloodWait, сек.",
    "floodwait_threshold": "FloodWait до карантина",
}


@router.get("/module-policies")
async def get_module_policies(_: int = Depends(require_admin)):
    result = {}
    for module, label in MODULE_LABELS.items():
        result[module] = {"label": label, "values": await telegram_automation.module_policy(module)}
    return result


@router.put("/module-policies/{module}")
async def update_module_policy(module: str, payload: ModulePolicyInput,
                               actor: int = Depends(require_admin)):
    if module not in MODULE_LABELS:
        raise HTTPException(404, "Модуль не найден.")
    try:
        values = await telegram_automation.set_module_policy(module, **payload.values)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from None
    async with SessionFactory() as db:
        await _audit(db, actor, "module.policy.update", "module", module)
        await db.commit()
    return {"label": MODULE_LABELS[module], "values": values}


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
