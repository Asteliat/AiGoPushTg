from datetime import datetime, timezone
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from app.ai.provider import AIUnavailableError
from app.services.drafts import create_draft
from app.settings import get_settings
from app.storage.db import SessionFactory
from app.storage.models import Account, Draft, DraftStatus, Example, Persona

router = Router()
settings = get_settings()

def allowed(user_id: int | None) -> bool:
    return user_id is not None and user_id in settings.admin_id_set

async def denied(message: Message) -> None:
    await message.answer("Доступ закрыт. Добавьте свой Telegram user ID в ADMIN_IDS в .env.")

@router.message(CommandStart())
async def start(message: Message) -> None:
    if not allowed(message.from_user.id if message.from_user else None):
        await denied(message); return
    await message.answer("AtcivPerson — управление своими аккаунтами и ИИ-черновиками.\n\n/accounts — аккаунты\n/personas — персоны\n/persona_add НАЗВАНИЕ | СТИЛЬ — создать персону\n/examples — число примеров\n/example_add ID | ВХОДЯЩЕЕ || ОТВЕТ — добавить пример\n/drafts — ожидающие черновики\n/draft ТЕКСТ — создать черновик\n/help — помощь\n\nАвтоматической отправки сообщений в чаты нет.")

@router.message(Command("help"))
async def help_cmd(message: Message) -> None:
    if not allowed(message.from_user.id if message.from_user else None):
        await denied(message); return
    await message.answer("Подключение пользовательского аккаунта выполняется владельцем в доверенном терминале: python -m app.telegram_gateway.enroll. Никогда не отправляйте боту коды входа, пароль 2FA или session-файлы. ИИ только предлагает черновики; публикация выполняется вручную вне этого приложения.\n/persona_add НАЗВАНИЕ | СТИЛЬ — создать персону\n/example_add ID | ВХОДЯЩЕЕ || ОТВЕТ — добавить пример (0 — общая подборка)")

@router.message(Command("accounts"))
async def accounts(message: Message) -> None:
    if not allowed(message.from_user.id if message.from_user else None):
        await denied(message); return
    async with SessionFactory() as session:
        rows = (await session.scalars(select(Account).order_by(Account.id))).all()
    await message.answer("\n".join(f"#{r.id} · {r.display_name} · @{r.username or 'нет username'} · {'включён' if r.enabled else 'выключен'}" for r in rows) or "Аккаунтов нет. Подключите аккаунт через CLI.")

@router.message(Command("personas"))
async def personas(message: Message) -> None:
    if not allowed(message.from_user.id if message.from_user else None):
        await denied(message); return
    async with SessionFactory() as session:
        rows = (await session.scalars(select(Persona).order_by(Persona.id))).all()
    await message.answer("\n\n".join(f"#{p.id} · {p.name}\n{p.description}" for p in rows) or "Персон пока нет.")

@router.message(Command("examples"))
async def examples(message: Message) -> None:
    if not allowed(message.from_user.id if message.from_user else None):
        await denied(message); return
    async with SessionFactory() as session:
        rows = (await session.scalars(select(Example))).all()
    await message.answer(f"Примеров сохранено: {len(rows)}. В генерации участвуют только примеры с подтверждённым разрешением.")

@router.message(Command("drafts"))
async def drafts(message: Message) -> None:
    if not allowed(message.from_user.id if message.from_user else None):
        await denied(message); return
    async with SessionFactory() as session:
        rows = list((await session.scalars(select(Draft).where(Draft.status == DraftStatus.PENDING.value).order_by(Draft.id.desc()).limit(10))).all())
    if not rows:
        await message.answer("Ожидающих черновиков нет."); return
    for d in rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✓ Принять для ручного использования", callback_data=f"draft:approve:{d.id}"), InlineKeyboardButton(text="Отклонить", callback_data=f"draft:reject:{d.id}")]])
        await message.answer(f"Черновик #{d.id}\nВходящее: {d.incoming_text[:1200]}\n\nПредложение: {d.proposed_text[:1800]}\n\nНичего не отправлено.", reply_markup=kb)

@router.message(Command("draft"))
async def draft_command(message: Message) -> None:
    if not allowed(message.from_user.id if message.from_user else None):
        await denied(message); return
    incoming = (message.text or "").partition(" ")[2].strip()
    if not incoming:
        await message.answer("Использование: /draft текст сообщения"); return
    if len(incoming) > 12000:
        await message.answer("Максимальная длина — 12000 символов."); return
    async with SessionFactory() as session:
        try:
            d = await create_draft(session, incoming_text=incoming)
        except AIUnavailableError as exc:
            await message.answer(str(exc)); return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✓ Принять для ручного использования", callback_data=f"draft:approve:{d.id}"), InlineKeyboardButton(text="Отклонить", callback_data=f"draft:reject:{d.id}")]])
    await message.answer(f"Черновик #{d.id}:\n\n{d.proposed_text}\n\nСообщение не отправлено.", reply_markup=kb)


@router.message(Command("persona_add"))
async def persona_add(message: Message) -> None:
    if not allowed(message.from_user.id if message.from_user else None):
        await denied(message); return
    raw = (message.text or "").partition(" ")[2]
    name, sep, style = raw.partition("|")
    name, style = name.strip(), style.strip()
    if not sep or not name or not style:
        await message.answer("Формат: /persona_add название | инструкции стиля")
        return
    if len(name) > 80 or len(style) > 5000:
        await message.answer("Название: до 80 символов; инструкции: до 5000 символов.")
        return
    async with SessionFactory() as session:
        existing = await session.scalar(select(Persona).where(Persona.name == name))
        if existing:
            await message.answer("Персона с таким названием уже существует.")
            return
        item = Persona(name=name, description="Создана через управляющего бота", style_instructions=style)
        session.add(item)
        await session.commit()
        await session.refresh(item)
    await message.answer(f"Персона #{item.id} «{item.name}» создана.")

@router.message(Command("example_add"))
async def example_add(message: Message) -> None:
    if not allowed(message.from_user.id if message.from_user else None):
        await denied(message); return
    raw = (message.text or "").partition(" ")[2]
    left, sep, reply = raw.partition("||")
    persona_raw, bar, incoming = left.partition("|")
    if not sep or not bar or not incoming.strip() or not reply.strip():
        await message.answer("Формат: /example_add ID_ПЕРСОНЫ | входящее сообщение || предпочтительный ответ\nДля общей подборки укажите 0 вместо ID.")
        return
    try:
        persona_id = int(persona_raw.strip())
    except ValueError:
        await message.answer("ID персоны должен быть числом (0 — общая подборка).")
        return
    incoming, reply = incoming.strip(), reply.strip()
    if max(len(incoming), len(reply)) > 6000:
        await message.answer("Каждая часть должна быть не длиннее 6000 символов.")
        return
    async with SessionFactory() as session:
        if persona_id and not await session.get(Persona, persona_id):
            await message.answer("Персона с таким ID не найдена.")
            return
        item = Example(persona_id=persona_id or None, title="Добавлено оператором", incoming_text=incoming, preferred_reply=reply, consent_confirmed=True)
        session.add(item)
        await session.commit()
        await session.refresh(item)
    await message.answer(f"Пример #{item.id} сохранён. Сохраняйте только текст, который вы вправе использовать, предварительно удаляя личные данные.")

@router.callback_query(F.data.startswith("draft:"))
async def review_draft(callback: CallbackQuery) -> None:
    if not allowed(callback.from_user.id):
        await callback.answer("Доступ закрыт", show_alert=True); return
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[1] not in {"approve", "reject"} or not parts[2].isdigit():
        await callback.answer("Некорректная команда", show_alert=True); return
    async with SessionFactory() as session:
        d = await session.get(Draft, int(parts[2]))
        if not d:
            await callback.answer("Черновик не найден", show_alert=True); return
        if d.status != DraftStatus.PENDING.value:
            await callback.answer("Уже обработан", show_alert=True); return
        d.status = DraftStatus.APPROVED.value if parts[1] == "approve" else DraftStatus.REJECTED.value
        d.reviewed_at = datetime.now(timezone.utc)
        await session.commit()
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("Решение сохранено. Отправки в чат не было.")
    await callback.answer("Сохранено")
