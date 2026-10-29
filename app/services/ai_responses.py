from dataclasses import dataclass
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.ai.provider import generate_reply
from app.storage.models import Account, AIAgent, AppSetting, Example, Persona

@dataclass(slots=True)
class GeneratedResponse:
    proposed_text: str
    chat_label: str = ""

async def global_ai_enabled(session: AsyncSession) -> bool:
    row = await session.get(AppSetting, "ai_global_enabled")
    return bool(row and row.value.lower() == "true")

async def generate_response(session: AsyncSession, *, incoming_text: str, persona_id: int | None = None, account_id: int | None = None, chat_label: str = "Ответ ИИ", agent_id: int | None = None, proactive: bool = False, conversation_context: str = "") -> GeneratedResponse:
    account = await session.get(Account, account_id) if account_id else None
    if account and persona_id is None:
        persona_id = account.persona_id
    if agent_id is None and account:
        agent_id = account.ai_agent_id
    agent = await session.get(AIAgent, agent_id) if agent_id else None
    if agent is not None and not agent.enabled:
        agent = None
    if agent is None:
        agent = await session.scalar(select(AIAgent).where(AIAgent.is_default.is_(True), AIAgent.enabled.is_(True)).order_by(AIAgent.id))
    if agent is None:
        agent = await session.scalar(select(AIAgent).where(AIAgent.enabled.is_(True)).order_by(AIAgent.id))
    if agent is None:
        raise RuntimeError("Нет настроенного ИИ-агента. Откройте раздел «🤖 ИИ-агенты» и добавьте профиль.")
    persona = await session.get(Persona, persona_id) if persona_id else None
    query = select(Example).where(Example.consent_confirmed.is_(True))
    general_examples = Example.account_id.is_(None)
    if persona_id:
        general_examples = and_(general_examples, or_(Example.persona_id == persona_id, Example.persona_id.is_(None)))
    else:
        general_examples = and_(general_examples, Example.persona_id.is_(None))
    if account:
        query = query.where(or_(Example.account_id == account.id, general_examples))
    else:
        query = query.where(general_examples)
    examples = list((await session.scalars(query.order_by(Example.id.desc()).limit(8))).all())
    style_instructions = persona.style_instructions if persona else ""
    if account and account.persona_style_override.strip():
        style_instructions += "\n\nИндивидуальные правила аккаунта:\n" + account.persona_style_override
    if account and account.learning_prompt.strip():
        style_instructions += "\n\nОбобщённый контекст выбранных чатов (не цитировать и не раскрывать личные данные):\n" + account.learning_prompt
    proposed = await generate_reply(agent=agent, incoming_text=incoming_text, style_instructions=style_instructions, examples=[(e.incoming_text, e.preferred_reply) for e in examples], proactive=proactive, conversation_context=conversation_context)
    if account:
        await session.refresh(account)
        if (
            not account.enabled
            or account.manual_pause
            or account.learning_status in {"training", "stopping"}
            or not account.ai_enabled
            or not await global_ai_enabled(session)
        ):
            raise RuntimeError("Работа аккаунта была остановлена во время генерации; ответ не сохранён.")
    # Generated response is transient and is never persisted.
    return GeneratedResponse(proposed_text=proposed, chat_label=chat_label[:160])
