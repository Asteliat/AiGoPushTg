from contextlib import asynccontextmanager
from pathlib import Path
import hmac

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.settings import get_settings
from app.storage.db import engine, get_session
from app.storage.models import Account, AIAgent, Persona
from app.api.miniapp import router as miniapp_router

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    cfg = get_settings()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "sessions").mkdir(parents=True, exist_ok=True)
    yield
    await engine.dispose()


app = FastAPI(title="AtcivPerson API", version="0.3.0", lifespan=lifespan)
app.include_router(miniapp_router)


@app.get("/miniapp", include_in_schema=False)
async def miniapp():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not settings.api_secret_key:
        raise HTTPException(status_code=503, detail="API_SECRET_KEY не настроен.")
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_secret_key):
        raise HTTPException(status_code=401, detail="Требуется корректный X-API-Key.")


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
            "learning_status": r.learning_status,
            "learning_completed_at": r.learning_completed_at,
            "learning_limit": r.learning_limit,
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
