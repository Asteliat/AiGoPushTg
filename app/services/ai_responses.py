from dataclasses import dataclass
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.ai.provider import generate_reply
from app.storage.models import Account, AIAgent, AppSetting, Example, Persona

@dataclass(slots=True)
class GeneratedResponse:
    proposed_text: str
    chat_label: str = ""


def resolve_runtime_override(account_override: int | None, agent_override: int | None, global_value: int) -> int:
    """Resolve account-specific settings such as cooldowns before agent/global defaults."""
    if account_override is not None:
        return int(account_override)
    return int(agent_override) if agent_override is not None else int(global_value)


def resolve_agent_runtime_override(agent_override: int | None, global_value: int) -> int:
    """Resolve reply probabilities from the assigned AI agent or global default only."""
    return int(agent_override) if agent_override is not None else int(global_value)


def build_reply_style_instructions(
    *,
    persona_style: str,
    account_style_override: str,
    chat_learning_prompt: str,
    additional_instructions: str = "",
) -> str:
    """Build style instructions for ordinary Chats conversations only."""
    style = persona_style or ""
    if account_style_override.strip():
        style += "\n\nИндивидуальные правила аккаунта:\n" + account_style_override
    if chat_learning_prompt.strip():
        style += "\n\nОбобщённый контекст выбранных чатов (не цитировать и не раскрывать личные данные):\n" + chat_learning_prompt
    if additional_instructions.strip():
        style += "\n\nДополнительные инструкции для этого диалога:\n" + additional_instructions[:6000]
    return style

async def global_ai_enabled(session: AsyncSession) -> bool:
    row = await session.get(AppSetting, "ai_global_enabled")
    return bool(row and row.value.lower() == "true")

async def generate_response(session: AsyncSession, *, incoming_text: str, persona_id: int | None = None, account_id: int | None = None, chat_label: str = "Ответ ИИ", agent_id: int | None = None, proactive: bool = False, conversation_context: str = "", additional_instructions: str = "", trigger_mode: bool = False) -> GeneratedResponse:
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
    style_instructions = build_reply_style_instructions(
        persona_style=persona.style_instructions if persona else "",
        account_style_override=account.persona_style_override if account else "",
        chat_learning_prompt=account.learning_prompt if account else "",
        additional_instructions=additional_instructions,
    )
    proposed = await generate_reply(agent=agent, incoming_text=incoming_text, style_instructions=style_instructions, examples=[(e.incoming_text, e.preferred_reply) for e in examples], proactive=proactive, conversation_context=conversation_context, trigger_mode=trigger_mode)
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
