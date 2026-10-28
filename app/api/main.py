from contextlib import asynccontextmanager
import hmac

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.provider import AIUnavailableError
from app.services.drafts import create_draft, global_ai_enabled
from app.settings import get_settings
from app.storage.db import engine, get_session, init_db
from app.storage.models import Account, AIAgent, Draft, Persona

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    cfg = get_settings()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "sessions").mkdir(parents=True, exist_ok=True)
    await init_db()
    yield
    await engine.dispose()


app = FastAPI(title="AtcivPerson API", version="0.2.0", lifespan=lifespan)


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not settings.api_secret_key:
        raise HTTPException(status_code=503, detail="API_SECRET_KEY не настроен.")
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_secret_key):
        raise HTTPException(status_code=401, detail="Требуется корректный X-API-Key.")


class DraftRequest(BaseModel):
    incoming_text: str = Field(min_length=1, max_length=12000)
    persona_id: int | None = None
    account_id: int | None = None
    chat_label: str = Field(default="Личный черновик", max_length=160)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "atcivperson-api"}


@app.get("/v1/accounts", dependencies=[Depends(require_api_key)])
async def list_accounts(session: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (await session.scalars(select(Account).order_by(Account.id))).all()
    return [
        {
            "id": r.id,
            "telegram_user_id": r.telegram_user_id,
            "username": r.username,
            "display_name": r.display_name,
            "enabled": r.enabled,
            "manual_pause": r.manual_pause,
            "connection_status": r.connection_status,
            "ai_enabled": r.ai_enabled,
            "ai_agent_id": r.ai_agent_id,
            "persona_id": r.persona_id,
            "reply_mode": r.reply_mode,
        }
        for r in rows
    ]


@app.get("/v1/agents", dependencies=[Depends(require_api_key)])
async def list_agents(session: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (await session.scalars(select(AIAgent).order_by(AIAgent.id))).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "base_url": r.base_url,
            "model": r.model,
            "timeout_seconds": r.timeout_seconds,
            "enabled": r.enabled,
            "is_default": r.is_default,
        }
        for r in rows
    ]


@app.get("/v1/personas", dependencies=[Depends(require_api_key)])
async def list_personas(session: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (await session.scalars(select(Persona).order_by(Persona.id))).all()
    return [
        {"id": r.id, "name": r.name, "description": r.description, "style_instructions": r.style_instructions}
        for r in rows
    ]


@app.post("/v1/drafts", status_code=201, dependencies=[Depends(require_api_key)])
async def make_draft(payload: DraftRequest, session: AsyncSession = Depends(get_session)) -> dict:
    if not await global_ai_enabled(session):
        raise HTTPException(status_code=503, detail="Глобальный ИИ выключен.")
    try:
        draft = await create_draft(
            session,
            incoming_text=payload.incoming_text,
            persona_id=payload.persona_id,
            account_id=payload.account_id,
            chat_label=payload.chat_label,
        )
    except AIUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail="Не удалось создать черновик; проверьте настройки агента и входные данные.",
        ) from exc
    return {"id": draft.id, "status": draft.status, "proposed_text": draft.proposed_text}


@app.get("/v1/drafts", dependencies=[Depends(require_api_key)])
async def list_drafts(session: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (await session.scalars(select(Draft).order_by(Draft.id.desc()).limit(100))).all()
    return [
        {
            "id": r.id,
            "status": r.status,
            "chat_label": r.chat_label,
            "incoming_text": r.incoming_text,
            "proposed_text": r.proposed_text,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]
