from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.ai.provider import generate_reply
from app.storage.models import Draft, Example, Persona

async def create_draft(session: AsyncSession, *, incoming_text: str, persona_id: int | None = None, account_id: int | None = None, chat_label: str = "Личный черновик") -> Draft:
    persona = await session.get(Persona, persona_id) if persona_id else None
    query = select(Example).where(Example.consent_confirmed.is_(True))
    if persona_id:
        query = query.where((Example.persona_id == persona_id) | (Example.persona_id.is_(None)))
    examples = list((await session.scalars(query.order_by(Example.id.desc()).limit(8))).all())
    proposed = await generate_reply(incoming_text=incoming_text, style_instructions=persona.style_instructions if persona else "", examples=[(e.incoming_text, e.preferred_reply) for e in examples])
    draft = Draft(incoming_text=incoming_text, proposed_text=proposed, persona_id=persona_id, account_id=account_id, chat_label=chat_label[:160])
    session.add(draft)
    await session.commit()
    await session.refresh(draft)
    return draft
