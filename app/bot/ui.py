from __future__ import annotations
import asyncio, io, os, secrets
from datetime import datetime, timezone
from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup, BufferedInputFile
from sqlalchemy import select
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from app.ai.provider import AIUnavailableError
from app.services.drafts import create_draft, global_ai_enabled
from app.settings import get_settings
from app.storage.db import SessionFactory
from app.storage.models import Account, AIAgent, AppSetting, ChatBinding, Draft, DraftStatus, Example, Persona

router = Router()
cfg = get_settings()
LOGIN_TASKS: dict[int, asyncio.Task] = {}

MAIN = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="👤 Аккаунты"), KeyboardButton(text="💬 Чаты")],
    [KeyboardButton(text="🤖 ИИ-агенты"), KeyboardButton(text="🎭 Персоны")],
    [KeyboardButton(text="🧩 Примеры"), KeyboardButton(text="📝 Черновики")],
    [KeyboardButton(text="✨ Создать черновик"), KeyboardButton(text="ℹ️ Помощь")],
], resize_keyboard=True)
CANCEL = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)

class Flow(StatesGroup):
    agent_name=State(); agent_url=State(); agent_key=State(); agent_model=State(); agent_timeout=State()
    agent_edit=State(); persona_name=State(); persona_style=State()
    example_incoming=State(); example_reply=State(); draft_text=State()

def admin(uid): return uid is not None and uid in cfg.admin_id_set
def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t,d in row] for row in rows])
async def check_msg(m):
    if not admin(m.from_user.id if m.from_user else None):
        await m.answer("Доступ закрыт. Проверьте ADMIN_IDS."); return False
    return True
async def check_cb(c):
    if not admin(c.from_user.id):
        await c.answer("Доступ закрыт", show_alert=True); return False
    return True
async def private(m):
    if m.chat.type != "private":
        await m.answer("Это действие доступно только в личном чате с ботом."); return False
    return True
async def delete_msg(m):
    try: await m.delete()
    except Exception: pass
async def show_home(m):
    async with SessionFactory() as db:
        nacc=len((await db.scalars(select(Account))).all())
        nagent=len((await db.scalars(select(AIAgent))).all())
        on=await global_ai_enabled(db)
    await m.answer(f"AtcivPerson · управление\nАккаунтов: {nacc}\nИИ-агентов: {nagent}\nГлобальный ИИ: {'включён' if on else 'выключен'}", reply_markup=MAIN)
async def edit(c, text, keyboard):
    if c.message:
        try: await c.message.edit_text(text, reply_markup=keyboard)
        except Exception: await c.message.answer(text, reply_markup=keyboard)
    await c.answer()
def back_home(): return kb([[("⬅️ Главное меню","nav:home")]])

@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    if await check_msg(m):
        await state.clear(); await show_home(m)
@router.message(F.text.in_({"❌ Отмена","Отмена"}))
async def cancel(m: Message, state: FSMContext):
    if await check_msg(m):
        await state.clear()
        task=LOGIN_TASKS.pop(m.from_user.id, None)
        if task: task.cancel()
        await m.answer("Действие отменено.", reply_markup=MAIN)
@router.message(F.text == "ℹ️ Помощь")
async def help_menu(m: Message):
    if await check_msg(m):
        await m.answer("Управление аккаунтами, чатами, персонами, примерами и ИИ-агентами доступно кнопками. Подключение аккаунта использует QR-вход Telegram. Если Telegram требует пароль 2FA, бот не принимает его и предлагает подключить аккаунт через доверенный терминал. Используйте только собственные аккаунты.", reply_markup=MAIN)

@router.callback_query(F.data=="nav:home")
async def home_cb(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    await state.clear()
    if c.message: await c.message.answer("Главное меню:", reply_markup=MAIN)
    await c.answer()

# Account menu and QR login
@router.message(F.text=="👤 Аккаунты")
async def accounts(m: Message):
    if not await check_msg(m): return
    await accounts_screen(m)
async def accounts_screen(m):
    async with SessionFactory() as db: rows=list((await db.scalars(select(Account).order_by(Account.id))).all())
    buttons=[[(f"{'🟢' if a.enabled else '⚪'} #{a.id} · {a.display_name[:28]}",f"acc:view:{a.id}")] for a in rows]
    buttons += [[("➕ Добавить аккаунт через QR","acc:add")],[("⬅️ Главное меню","nav:home")]]
    await m.answer("👤 Аккаунты\nВыберите аккаунт или подключите новый." if rows else "Аккаунтов пока нет. Подключите первый аккаунт кнопкой ниже.",reply_markup=kb(buttons))
@router.callback_query(F.data=="acc:list")
async def acc_list(c):
    if not await check_cb(c): return
    async with SessionFactory() as db: rows=list((await db.scalars(select(Account).order_by(Account.id))).all())
    buttons=[[(f"{'🟢' if a.enabled else '⚪'} #{a.id} · {a.display_name[:28]}",f"acc:view:{a.id}")] for a in rows]
    buttons += [[("➕ Добавить аккаунт через QR","acc:add")],[("⬅️ Главное меню","nav:home")]]
    await edit(c,"👤 Аккаунты",kb(buttons))
@router.callback_query(F.data=="acc:add")
async def acc_add(c, state: FSMContext):
    if not await check_cb(c): return
    if not c.message or c.message.chat.type!="private":
        await c.answer("Откройте личный чат с ботом.",show_alert=True); return
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.",show_alert=True); return
    uid=c.from_user.id
    if uid in LOGIN_TASKS and not LOGIN_TASKS[uid].done():
        await c.answer("Вход уже запущен. Отмените текущий процесс перед новым.",show_alert=True); return
    await state.clear()
    task=asyncio.create_task(qr_login(c.message.bot,uid))
    LOGIN_TASKS[uid]=task
    await c.answer("Готовлю QR-вход…")
async def qr_login(bot, uid):
    folder=cfg.data_dir/"sessions"; folder.mkdir(parents=True,exist_ok=True); os.chmod(folder,0o700)
    session_path=folder/("account_"+secrets.token_hex(8))
    client=TelegramClient(str(session_path),cfg.telegram_api_id,cfg.telegram_api_hash)
    success = False
    try:
        await client.connect()
        qr=await client.qr_login()
        import qrcode
        img=qrcode.make(qr.url); stream=io.BytesIO(); img.save(stream,format="PNG")
        await bot.send_photo(uid,BufferedInputFile(stream.getvalue(),filename="telegram-login.png"),
            caption="Отсканируйте QR в Telegram: Настройки → Устройства → Подключить устройство. Не пересылайте QR другим людям. Ожидаю подтверждение…")
        try:
            await qr.wait(timeout=120)
        except SessionPasswordNeededError:
            await bot.send_message(uid,"Для этого аккаунта Telegram запросил дополнительную проверку 2FA. Бот не принимает пароль 2FA. Подключите такой аккаунт через защищённый терминал или используйте отдельный безопасный способ авторизации.")
            return
        me=await client.get_me()
        if not me: raise RuntimeError("Telegram не вернул профиль.")
        name=" ".join(x for x in [me.first_name,me.last_name] if x) or str(me.id)
        async with SessionFactory() as db:
            row=await db.scalar(select(Account).where(Account.telegram_user_id==me.id))
            if row: row.username,row.display_name,row.session_path,row.enabled=me.username,name,str(session_path),True
            else: db.add(Account(telegram_user_id=me.id,username=me.username,display_name=name,session_path=str(session_path),enabled=True,reply_mode="manual",ai_enabled=False))
            await db.commit()
        success = True
        try: os.chmod(str(session_path)+".session",0o600)
        except OSError: pass
        await bot.send_message(uid,f"Аккаунт «{name}» подключён. ИИ выключен до отдельного включения в настройках.",reply_markup=MAIN)
    except asyncio.CancelledError:
        await bot.send_message(uid,"Подключение аккаунта отменено.",reply_markup=MAIN)
        raise
    except Exception as exc:
        await bot.send_message(uid,f"Не удалось подключить аккаунт: {type(exc).__name__}: {str(exc)[:220]}",reply_markup=MAIN)
    finally:
        LOGIN_TASKS.pop(uid,None)
        try:
            await client.disconnect()
        finally:
            if not success:
                for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
                    try:
                        os.unlink(str(session_path) + suffix)
                    except FileNotFoundError:
                        pass
@router.callback_query(F.data.startswith("acc:view:"))
async def acc_view(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(Account,aid)
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        agent=await db.get(AIAgent,a.ai_agent_id) if a.ai_agent_id else None
        persona=await db.get(Persona,a.persona_id) if a.persona_id else None
        chats=len((await db.scalars(select(ChatBinding).where(ChatBinding.account_id==aid,ChatBinding.enabled.is_(True)))).all())
    txt=f"👤 {a.display_name} (@{a.username or 'нет username'})\nID: #{aid}\nАккаунт: {'включён' if a.enabled else 'выключен'}\nИИ для аккаунта: {'включён' if a.ai_enabled else 'выключен'}\nГлобальный ИИ должен быть включён.\nРежим: {'автоматический' if a.reply_mode=='automatic' else 'ручной'}\nАгент: {agent.name if agent else 'не назначен'}\nПерсона: {persona.name if persona else 'не назначена'}\nАктивных чатов: {chats}"
    buttons=[
        [(("⏸ Выключить" if a.enabled else "▶️ Включить"),f"acc:toggle:{aid}")],
        [(("🤖 Выключить ИИ" if a.ai_enabled else "🤖 Включить ИИ"),f"acc:ai:{aid}")],
        [(f"Режим: {'Автомат' if a.reply_mode=='automatic' else 'Ручной'} · изменить",f"acc:mode:{aid}")],
        [("🧠 Назначить ИИ-агента",f"acc:agents:{aid}")],
        [("🎭 Назначить персону",f"acc:personas:{aid}")],
        [("💬 Выбрать чаты",f"chat:account:{aid}")],
        [("⬅️ К аккаунтам","acc:list")]]
    await edit(c,txt,kb(buttons))
@router.callback_query(F.data.startswith("acc:toggle:"))
async def acc_toggle(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(Account,aid)
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        a.enabled=not a.enabled; await db.commit()
    await acc_view(c)
@router.callback_query(F.data.startswith("acc:ai:"))
async def acc_ai(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(Account,aid)
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        a.ai_enabled=not a.ai_enabled; await db.commit()
    await acc_view(c)
@router.callback_query(F.data.startswith("acc:mode:"))
async def acc_mode(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(Account,aid)
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        a.reply_mode="manual" if a.reply_mode=="automatic" else "automatic"; await db.commit()
    await acc_view(c)
@router.callback_query(F.data.startswith("acc:agents:"))
async def acc_agents(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db: agents=list((await db.scalars(select(AIAgent).order_by(AIAgent.id))).all())
    buttons=[[(f"{'⭐ ' if a.is_default else ''}{a.name} · {'вкл' if a.enabled else 'выкл'}",f"acc:setagent:x:{aid}:{a.id}")] for a in agents]
    buttons += [[("Без агента",f"acc:setagent:x:{aid}:0")],[("⬅️ Назад",f"acc:view:{aid}")]]
    await edit(c,"Выберите ИИ-агента:",kb(buttons))
@router.callback_query(F.data.startswith("acc:setagent:"))
async def acc_set_agent(c):
    if not await check_cb(c): return
    _,_,_,aid,agent= c.data.split(":")
    async with SessionFactory() as db:
        a=await db.get(Account,int(aid)); ag=await db.get(AIAgent,int(agent)) if int(agent) else None
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        a.ai_agent_id=ag.id if ag else None; await db.commit()
    await acc_view(c)
@router.callback_query(F.data.startswith("acc:personas:"))
async def acc_personas(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db: ps=list((await db.scalars(select(Persona).order_by(Persona.id))).all())
    buttons=[[(p.name,f"acc:setpersona:x:{aid}:{p.id}")] for p in ps]
    buttons += [[("Без персоны",f"acc:setpersona:x:{aid}:0")],[("⬅️ Назад",f"acc:view:{aid}")]]
    await edit(c,"Выберите персону:",kb(buttons))
@router.callback_query(F.data.startswith("acc:setpersona:"))
async def acc_set_persona(c):
    if not await check_cb(c): return
    _,_,_,aid,pid=c.data.split(":")
    async with SessionFactory() as db:
        a=await db.get(Account,int(aid))
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        a.persona_id=int(pid) or None; await db.commit()
    await acc_view(c)

# AI agent management
@router.message(F.text=="🤖 ИИ-агенты")
async def agents(m):
    if not await check_msg(m): return
    await agents_screen(m)
async def agents_screen(m):
    async with SessionFactory() as db:
        aa=list((await db.scalars(select(AIAgent).order_by(AIAgent.id))).all()); on=await global_ai_enabled(db)
        enabled_count=len((await db.scalars(select(Account).where(Account.ai_enabled.is_(True)))).all())
    buttons=[[(f"{'⭐ ' if a.is_default else ''}{'🟢' if a.enabled else '⚪'} {a.name}",f"agent:view:{a.id}")] for a in aa]
    buttons += [[(("🟢 Выключить ИИ для всех" if on else "🤖 Включить ИИ для всех"),"agent:global")],
                [("🔀 Назначить агент всем аккаунтам","agent:assignall")],[("➕ Добавить ИИ-агента","agent:add")],[("⬅️ Главное меню","nav:home")]]
    await m.answer(f"🤖 ИИ-агенты\nГлобально: {'включён' if on else 'выключен'}\nАккаунтов с локальным ИИ: {enabled_count}",reply_markup=kb(buttons))
@router.callback_query(F.data=="agent:menu")
async def agents_cb(c):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        aa=list((await db.scalars(select(AIAgent).order_by(AIAgent.id))).all()); on=await global_ai_enabled(db)
    buttons=[[(f"{'⭐ ' if a.is_default else ''}{'🟢' if a.enabled else '⚪'} {a.name}",f"agent:view:{a.id}")] for a in aa]
    buttons += [[(("🟢 Выключить ИИ для всех" if on else "🤖 Включить ИИ для всех"),"agent:global")],[("🔀 Назначить агент всем аккаунтам","agent:assignall")],[("➕ Добавить ИИ-агента","agent:add")],[("⬅️ Главное меню","nav:home")]]
    await edit(c,f"🤖 ИИ-агенты · глобально {'включён' if on else 'выключен'}",kb(buttons))
@router.callback_query(F.data=="agent:global")
async def global_toggle(c):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        s=await db.get(AppSetting,"ai_global_enabled")
        if not s: s=AppSetting(key="ai_global_enabled",value="false"); db.add(s)
        s.value="false" if s.value.lower()=="true" else "true"; await db.commit()
    await agents_cb(c)
@router.callback_query(F.data=="agent:add")
async def agent_add(c,state):
    if not await check_cb(c): return
    await state.clear(); await state.set_state(Flow.agent_name)
    await c.message.answer("Введите название профиля, например Groq или OpenAI.",reply_markup=CANCEL); await c.answer()
@router.message(Flow.agent_name)
async def agent_name(m,state):
    if not await check_msg(m) or not await private(m): return
    name=(m.text or "").strip()
    if not name or len(name)>80: await m.answer("Название — от 1 до 80 символов.",reply_markup=CANCEL); return
    async with SessionFactory() as db:
        if await db.scalar(select(AIAgent.id).where(AIAgent.name==name)): await m.answer("Такое название уже есть.",reply_markup=CANCEL); return
    await state.update_data(name=name); await state.set_state(Flow.agent_url)
    await m.answer("Введите Base URL API, например https://api.openai.com/v1",reply_markup=CANCEL)
@router.message(Flow.agent_url)
async def agent_url(m,state):
    if not await check_msg(m) or not await private(m): return
    v=(m.text or "").strip().rstrip("/")
    if not v.startswith("https://") and not (v.startswith("http://") and "localhost" in v): await m.answer("Нужен HTTPS URL (HTTP только localhost).",reply_markup=CANCEL); return
    await state.update_data(url=v); await state.set_state(Flow.agent_key); await m.answer("Введите API-ключ. Сообщение будет удалено по возможности.",reply_markup=CANCEL)
@router.message(Flow.agent_key)
async def agent_key(m,state):
    if not await check_msg(m) or not await private(m): return
    v=(m.text or "").strip(); await delete_msg(m)
    if len(v)<5 or len(v)>1000: await m.answer("Некорректная длина ключа.",reply_markup=CANCEL); return
    await state.update_data(key=v); await state.set_state(Flow.agent_model); await m.answer("Введите точное имя модели.",reply_markup=CANCEL)
@router.message(Flow.agent_model)
async def agent_model(m,state):
    if not await check_msg(m) or not await private(m): return
    v=(m.text or "").strip()
    if not v or len(v)>160: await m.answer("Некорректное имя модели.",reply_markup=CANCEL); return
    await state.update_data(model=v); await state.set_state(Flow.agent_timeout); await m.answer("Введите тайм-аут в секундах от 10 до 180.",reply_markup=CANCEL)
@router.message(Flow.agent_timeout)
async def agent_timeout(m,state):
    if not await check_msg(m) or not await private(m): return
    v=(m.text or "").strip()
    if not v.isdigit() or not 10<=int(v)<=180: await m.answer("Введите число 10–180.",reply_markup=CANCEL); return
    d=await state.get_data()
    async with SessionFactory() as db:
        has=bool(await db.scalar(select(AIAgent.id)))
        a=AIAgent(name=d["name"],base_url=d["url"],api_key=d["key"],model=d["model"],timeout_seconds=int(v),is_default=not has)
        db.add(a); await db.commit(); await db.refresh(a)
    await state.clear(); await m.answer(f"Профиль «{a.name}» сохранён.",reply_markup=MAIN); await agents_screen(m)
@router.callback_query(F.data=="agent:assignall")
async def assign_all_menu(c):
    if not await check_cb(c): return
    async with SessionFactory() as db: aa=list((await db.scalars(select(AIAgent).where(AIAgent.enabled.is_(True)))).all())
    await edit(c,"Выберите агент для всех аккаунтов:",kb([[ (a.name,f"agent:assignall:{a.id}") ] for a in aa]+[[("⬅️ Назад","agent:menu")]]))
@router.callback_query(F.data.startswith("agent:assignall:"))
async def assign_all(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(AIAgent,aid)
        if not a or not a.enabled: await c.answer("Агент не найден или выключен",show_alert=True); return
        for account in (await db.scalars(select(Account))).all(): account.ai_agent_id=aid
        for other in (await db.scalars(select(AIAgent))).all(): other.is_default=other.id==aid
        await db.commit()
    await edit(c,f"«{a.name}» назначен всем аккаунтам. Локальные переключатели ИИ не менялись.",back_home())
@router.callback_query(F.data.startswith("agent:view:"))
async def agent_view(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(AIAgent,aid)
        if not a: await c.answer("Агент не найден",show_alert=True); return
        assigned=list((await db.scalars(select(Account).where(Account.ai_agent_id==aid))).all())
    masked=("•"*min(12,len(a.api_key))) if a.api_key else "не задан"
    txt=f"🤖 {a.name}\nСтатус: {'включён' if a.enabled else 'выключен'}\nПо умолчанию: {'да' if a.is_default else 'нет'}\nBase URL: {a.base_url}\nМодель: {a.model}\nAPI-ключ: {masked}\nТайм-аут: {a.timeout_seconds} сек.\nАккаунты: {', '.join('#'+str(x.id) for x in assigned) or 'нет'}"
    buttons=[[(("⏸ Выключить профиль" if a.enabled else "▶️ Включить профиль"),f"agent:toggle:{aid}")],
        [("⭐ По умолчанию",f"agent:default:{aid}")],
        [("✏️ Base URL",f"agent:edit:url:{aid}"),("✏️ Модель",f"agent:edit:model:{aid}")],
        [("🔑 API-ключ",f"agent:edit:key:{aid}"),("⏱ Тайм-аут",f"agent:edit:timeout:{aid}")],
        [("🗑 Удалить профиль",f"agent:delete:{aid}")],[("⬅️ К ИИ-агентам","agent:menu")]]
    await edit(c,txt,kb(buttons))
@router.callback_query(F.data.startswith("agent:toggle:"))
async def agent_toggle(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(AIAgent,aid)
        if not a: await c.answer("Агент не найден",show_alert=True); return
        a.enabled=not a.enabled; await db.commit()
    await agent_view(c)
@router.callback_query(F.data.startswith("agent:default:"))
async def agent_default(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(AIAgent,aid)
        if not a or not a.enabled: await c.answer("Сначала включите агента",show_alert=True); return
        for x in (await db.scalars(select(AIAgent))).all(): x.is_default=x.id==aid
        await db.commit()
    await agent_view(c)
@router.callback_query(F.data.startswith("agent:edit:"))
async def agent_edit_start(c,state):
    if not await check_cb(c): return
    _,_,field,aid=c.data.split(":")
    await state.clear(); await state.update_data(edit_field=field,edit_aid=int(aid)); await state.set_state(Flow.agent_edit)
    await c.message.answer("Введите новое значение для "+field+". Для ключа сообщение будет удалено.",reply_markup=CANCEL); await c.answer()
@router.message(Flow.agent_edit)
async def agent_edit_save(m,state):
    if not await check_msg(m) or not await private(m): return
    d=await state.get_data(); field=d["edit_field"]; v=(m.text or "").strip()
    if field=="key":
        await delete_msg(m)
        if len(v)<5 or len(v)>1000: await m.answer("Некорректный ключ.",reply_markup=CANCEL); return
    elif field=="url":
        v=v.rstrip("/")
        if not v.startswith("https://") and not (v.startswith("http://") and "localhost" in v): await m.answer("Нужен HTTPS URL.",reply_markup=CANCEL); return
    elif field=="model":
        if not v or len(v)>160: await m.answer("Некорректная модель.",reply_markup=CANCEL); return
    elif field=="timeout":
        if not v.isdigit() or not 10<=int(v)<=180: await m.answer("Введите число 10–180.",reply_markup=CANCEL); return
        v=int(v)
    attr={"url":"base_url","model":"model","key":"api_key","timeout":"timeout_seconds"}[field]
    async with SessionFactory() as db:
        a=await db.get(AIAgent,d["edit_aid"])
        if not a: await state.clear(); await m.answer("Агент не найден.",reply_markup=MAIN); return
        setattr(a,attr,v); await db.commit()
    await state.clear(); await m.answer("Настройки сохранены.",reply_markup=MAIN)
@router.callback_query(F.data.startswith("agent:delete:"))
async def agent_delete(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(AIAgent,aid)
        if not a: await c.answer("Агент не найден",show_alert=True); return
        was=a.is_default
        for acc in (await db.scalars(select(Account).where(Account.ai_agent_id==aid))).all(): acc.ai_agent_id=None; acc.ai_enabled=False
        await db.delete(a); await db.flush()
        if was:
            replacement=await db.scalar(select(AIAgent).where(AIAgent.enabled.is_(True)).order_by(AIAgent.id))
            if replacement: replacement.is_default=True
        await db.commit()
    await agents_cb(c)

# Personas
@router.message(F.text=="🎭 Персоны")
async def personas(m):
    if not await check_msg(m): return
    await persona_screen(m)
async def persona_screen(m):
    async with SessionFactory() as db: ps=list((await db.scalars(select(Persona).order_by(Persona.id))).all())
    await m.answer("🎭 Персоны\nВыберите персону." if ps else "Персон пока нет. Добавьте первую.",reply_markup=kb([[(p.name,f"persona:view:{p.id}")] for p in ps]+[[("➕ Добавить персону","persona:add")],[("⬅️ Главное меню","nav:home")]]))
@router.callback_query(F.data=="persona:menu")
async def persona_menu(c):
    if not await check_cb(c): return
    async with SessionFactory() as db: ps=list((await db.scalars(select(Persona).order_by(Persona.id))).all())
    await edit(c,"🎭 Персоны",kb([[(p.name,f"persona:view:{p.id}")] for p in ps]+[[("➕ Добавить персону","persona:add")],[("⬅️ Главное меню","nav:home")]]))
@router.callback_query(F.data=="persona:add")
async def persona_add(c,state):
    if not await check_cb(c): return
    await state.clear(); await state.set_state(Flow.persona_name); await c.message.answer("Введите название персоны.",reply_markup=CANCEL); await c.answer()
@router.message(Flow.persona_name)
async def persona_name(m,state):
    if not await check_msg(m) or not await private(m): return
    name=(m.text or "").strip()
    if not name or len(name)>80: await m.answer("Название — 1–80 символов.",reply_markup=CANCEL); return
    async with SessionFactory() as db:
        if await db.scalar(select(Persona.id).where(Persona.name==name)): await m.answer("Такое имя уже есть.",reply_markup=CANCEL); return
    await state.update_data(persona_name=name); await state.set_state(Flow.persona_style); await m.answer("Введите инструкции стиля общения.",reply_markup=CANCEL)
@router.message(Flow.persona_style)
async def persona_style(m,state):
    if not await check_msg(m) or not await private(m): return
    style=(m.text or "").strip()
    if not style or len(style)>5000: await m.answer("Текст должен быть от 1 до 5000 символов.",reply_markup=CANCEL); return
    d=await state.get_data()
    async with SessionFactory() as db: p=Persona(name=d["persona_name"],description="Создана через бот",style_instructions=style); db.add(p); await db.commit()
    await state.clear(); await m.answer("Персона сохранена.",reply_markup=MAIN)
@router.callback_query(F.data.startswith("persona:view:"))
async def persona_view(c):
    if not await check_cb(c): return
    pid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        p=await db.get(Persona,pid); n=len((await db.scalars(select(Example).where(Example.persona_id==pid))).all())
    if not p: await c.answer("Персона не найдена",show_alert=True); return
    await edit(c,f"🎭 {p.name}\n\n{p.style_instructions[:2500]}\n\nПримеров: {n}",kb([[("🧩 Добавить пример",f"example:add:{pid}")],[("🗑 Удалить персону",f"persona:delete:{pid}")],[("⬅️ К персонам","persona:menu")]]))
@router.callback_query(F.data.startswith("persona:delete:"))
async def persona_delete(c):
    if not await check_cb(c): return
    pid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        p=await db.get(Persona,pid)
        if p:
            for a in (await db.scalars(select(Account).where(Account.persona_id==pid))).all(): a.persona_id=None
            await db.delete(p); await db.commit()
    await persona_menu(c)

# Examples
@router.message(F.text=="🧩 Примеры")
async def examples(m):
    if not await check_msg(m): return
    await example_screen(m)
async def example_screen(m):
    async with SessionFactory() as db:
        es=list((await db.scalars(select(Example).order_by(Example.id.desc()).limit(15))).all())
        ps={p.id:p.name for p in (await db.scalars(select(Persona))).all()}
    buttons=[[(f"#{e.id} · {ps.get(e.persona_id,'Общие')} · {e.incoming_text[:24]}",f"example:view:{e.id}")] for e in es]
    buttons += [[("➕ Добавить общий пример","example:add:0")],[("⬅️ Главное меню","nav:home")]]
    await m.answer("🧩 Примеры · выберите запись." if es else "Примеров пока нет.",reply_markup=kb(buttons))
@router.callback_query(F.data=="example:menu")
async def example_menu(c):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        es=list((await db.scalars(select(Example).order_by(Example.id.desc()).limit(15))).all()); ps={p.id:p.name for p in (await db.scalars(select(Persona))).all()}
    buttons=[[(f"#{e.id} · {ps.get(e.persona_id,'Общие')} · {e.incoming_text[:24]}",f"example:view:{e.id}")] for e in es]
    buttons += [[("➕ Добавить общий пример","example:add:0")],[("⬅️ Главное меню","nav:home")]]
    await edit(c,"🧩 Примеры",kb(buttons))
@router.callback_query(F.data.startswith("example:add:"))
async def example_add(c,state):
    if not await check_cb(c): return
    pid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        if pid and not await db.get(Persona,pid): await c.answer("Персона не найдена",show_alert=True); return
    await state.clear(); await state.update_data(example_pid=pid); await state.set_state(Flow.example_incoming)
    await c.message.answer("Введите входящее сообщение для примера. Удалите личные данные и сохраняйте только разрешённый текст.",reply_markup=CANCEL); await c.answer()
@router.message(Flow.example_incoming)
async def example_incoming(m,state):
    if not await check_msg(m) or not await private(m): return
    v=(m.text or "").strip()
    if not v or len(v)>6000: await m.answer("Введите текст до 6000 символов.",reply_markup=CANCEL); return
    await state.update_data(example_incoming=v); await state.set_state(Flow.example_reply); await m.answer("Введите предпочтительный ответ.",reply_markup=CANCEL)
@router.message(Flow.example_reply)
async def example_reply(m,state):
    if not await check_msg(m) or not await private(m): return
    v=(m.text or "").strip()
    if not v or len(v)>6000: await m.answer("Введите ответ до 6000 символов.",reply_markup=CANCEL); return
    d=await state.get_data()
    async with SessionFactory() as db: e=Example(persona_id=d["example_pid"] or None,title="Добавлено через бот",incoming_text=d["example_incoming"],preferred_reply=v,consent_confirmed=True); db.add(e); await db.commit()
    await state.clear(); await m.answer("Пример сохранён.",reply_markup=MAIN)
@router.callback_query(F.data.startswith("example:view:"))
async def example_view(c):
    if not await check_cb(c): return
    eid=int(c.data.split(":")[2])
    async with SessionFactory() as db: e=await db.get(Example,eid); p=await db.get(Persona,e.persona_id) if e and e.persona_id else None
    if not e: await c.answer("Пример не найден",show_alert=True); return
    await edit(c,f"Пример #{eid} · {p.name if p else 'Общий'}\n\nВходящее:\n{e.incoming_text[:1600]}\n\nОтвет:\n{e.preferred_reply[:1600]}",kb([[("🗑 Удалить",f"example:delete:{eid}")],[("⬅️ К примерам","example:menu")]]))
@router.callback_query(F.data.startswith("example:delete:"))
async def example_delete(c):
    if not await check_cb(c): return
    eid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        e=await db.get(Example,eid)
        if e: await db.delete(e); await db.commit()
    await example_menu(c)

# Chats
@router.message(F.text=="💬 Чаты")
async def chats(m):
    if not await check_msg(m): return
    await chats_screen(m)
async def chats_screen(m):
    async with SessionFactory() as db:
        aa=list((await db.scalars(select(Account).order_by(Account.id))).all())
        bs=list((await db.scalars(select(ChatBinding).order_by(ChatBinding.account_id,ChatBinding.id))).all())
    buttons=[[(f"Загрузить чаты · #{a.id} {a.display_name[:22]}",f"chat:account:{a.id}")] for a in aa]
    buttons += [[(f"{'🟢' if b.enabled else '⚪'} #{b.account_id} · {b.title[:30]}",f"chat:toggle:{b.id}")] for b in bs[:20]]
    buttons += [[("⬅️ Главное меню","nav:home")]]
    await m.answer("💬 Чаты\nВыберите аккаунт, чтобы показать его существующие чаты. Автоматического вступления в чаты нет." if aa else "Сначала подключите аккаунт в разделе «👤 Аккаунты».",reply_markup=kb(buttons))
@router.callback_query(F.data=="chat:menu")
async def chats_cb(c):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        aa=list((await db.scalars(select(Account).order_by(Account.id))).all()); bs=list((await db.scalars(select(ChatBinding).order_by(ChatBinding.account_id,ChatBinding.id))).all())
    buttons=[[(f"Загрузить чаты · #{a.id} {a.display_name[:22]}",f"chat:account:{a.id}")] for a in aa]
    buttons += [[(f"{'🟢' if b.enabled else '⚪'} #{b.account_id} · {b.title[:30]}",f"chat:toggle:{b.id}")] for b in bs[:20]]
    buttons += [[("⬅️ Главное меню","nav:home")]]
    await edit(c,"💬 Чаты · выберите аккаунт или переключите выбранный чат.",kb(buttons))
@router.callback_query(F.data.startswith("chat:account:"))
async def chat_account(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    if not cfg.telegram_api_id or not cfg.telegram_api_hash: await c.answer("Не заданы Telegram API credentials.",show_alert=True); return
    async with SessionFactory() as db: a=await db.get(Account,aid)
    if not a: await c.answer("Аккаунт не найден",show_alert=True); return
    client=TelegramClient(a.session_path,cfg.telegram_api_id,cfg.telegram_api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized(): await c.answer("Сессия не авторизована",show_alert=True); return
        dialogs=[]
        async for d in client.iter_dialogs(limit=100): dialogs.append((d.id,d.name or str(d.id)))
    except Exception as exc: await c.answer(f"Ошибка загрузки: {type(exc).__name__}",show_alert=True); return
    finally: await client.disconnect()
    async with SessionFactory() as db: selected={b.chat_id for b in (await db.scalars(select(ChatBinding).where(ChatBinding.account_id==aid,ChatBinding.enabled.is_(True)))).all()}
    buttons=[[(f"{'🟢' if cid in selected else '▫️'} {title[:36]}",f"chat:select:x:{aid}:{cid}")] for cid,title in dialogs[:40]]
    buttons += [[("⬅️ К чатам","chat:menu")]]
    await edit(c,f"Выберите существующие чаты аккаунта #{aid}. Показано до 40 из первых 100.",kb(buttons))
@router.callback_query(F.data.startswith("chat:select:"))
async def chat_select(c):
    if not await check_cb(c): return
    _,_,_,aid_s,cid_s=c.data.split(":"); aid,cid=int(aid_s),int(cid_s)
    async with SessionFactory() as db: a=await db.get(Account,aid)
    client=TelegramClient(a.session_path,cfg.telegram_api_id,cfg.telegram_api_hash)
    try:
        await client.connect(); entity=await client.get_entity(cid)
        title=getattr(entity,"title",None) or getattr(entity,"username",None) or " ".join(x for x in [getattr(entity,"first_name",None),getattr(entity,"last_name",None)] if x) or str(cid)
    except Exception as exc: await c.answer(f"Не удалось открыть чат: {type(exc).__name__}",show_alert=True); return
    finally: await client.disconnect()
    async with SessionFactory() as db:
        b=await db.scalar(select(ChatBinding).where(ChatBinding.account_id==aid,ChatBinding.chat_id==cid))
        if b:
            b.enabled = not b.enabled
            b.title = title
        else:
            db.add(ChatBinding(account_id=aid, chat_id=cid, title=title, enabled=True))
        await db.commit()
    await chat_account_refresh(c,aid)
async def chat_account_refresh(c,aid):
    async with SessionFactory() as db: bs=list((await db.scalars(select(ChatBinding).where(ChatBinding.account_id==aid).order_by(ChatBinding.id))).all())
    await edit(c,f"Выбранные чаты аккаунта #{aid}. Чтобы добавить другие, загрузите список существующих чатов.",kb([[(f"{'🟢' if b.enabled else '⚪'} {b.title[:36]}",f"chat:toggle:{b.id}")] for b in bs]+[[("↻ Загрузить существующие чаты",f"chat:account:{aid}")],[("⬅️ К чатам","chat:menu")]]))
@router.callback_query(F.data.startswith("chat:toggle:"))
async def chat_toggle(c):
    if not await check_cb(c): return
    bid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        b=await db.get(ChatBinding,bid)
        if not b: await c.answer("Чат не найден",show_alert=True); return
        b.enabled = not b.enabled
        aid = b.account_id
        await db.commit()
    await chat_account_refresh(c,aid)

# Drafts
@router.message(F.text=="📝 Черновики")
async def drafts(m):
    if not await check_msg(m): return
    async with SessionFactory() as db: ds=list((await db.scalars(select(Draft).where(Draft.status==DraftStatus.PENDING).order_by(Draft.id.desc()).limit(10))).all())
    if not ds: await m.answer("Ожидающих черновиков нет.",reply_markup=MAIN); return
    for d in ds:
        await m.answer(f"Черновик #{d.id} · {d.chat_label}\n\nВходящее:\n{d.incoming_text[:900]}\n\nОтвет ИИ:\n{d.proposed_text[:1600]}\n\nАвтоматической отправки этой кнопкой нет.",reply_markup=kb([[("✓ Отметить рассмотренным",f"draft:approve:{d.id}"),("Отклонить",f"draft:reject:{d.id}")]]))
@router.callback_query(F.data.startswith("draft:"))
async def draft_review(c):
    if not await check_cb(c): return
    p=c.data.split(":")
    async with SessionFactory() as db:
        d=await db.get(Draft,int(p[2])) if len(p)==3 and p[2].isdigit() else None
        if not d or d.status!=DraftStatus.PENDING: await c.answer("Черновик не найден или уже обработан",show_alert=True); return
        d.status=DraftStatus.APPROVED if p[1]=="approve" else DraftStatus.REJECTED; d.reviewed_at=datetime.now(timezone.utc); await db.commit()
    await edit(c,"Статус сохранён. Сообщение в исходный чат не отправлялось.",back_home())
@router.message(F.text=="✨ Создать черновик")
async def draft_start(m,state):
    if not await check_msg(m): return
    async with SessionFactory() as db:
        if not await global_ai_enabled(db): await m.answer("Сначала включите глобальный ИИ в разделе «🤖 ИИ-агенты».",reply_markup=MAIN); return
        has=await db.scalar(select(AIAgent.id).where(AIAgent.enabled.is_(True)))
    if not has: await m.answer("Сначала создайте ИИ-агента.",reply_markup=MAIN); return
    await state.set_state(Flow.draft_text); await m.answer("Введите текст сообщения для подготовки черновика.",reply_markup=CANCEL)
@router.message(Flow.draft_text)
async def draft_create(m,state):
    if not await check_msg(m): return
    incoming=(m.text or "").strip()
    if not incoming or len(incoming)>12000: await m.answer("Введите текст до 12000 символов.",reply_markup=CANCEL); return
    async with SessionFactory() as db:
        if not await global_ai_enabled(db): await state.clear(); await m.answer("Глобальный ИИ выключен.",reply_markup=MAIN); return
        try: d=await create_draft(db,incoming_text=incoming)
        except (AIUnavailableError,RuntimeError) as exc: await state.clear(); await m.answer(str(exc),reply_markup=MAIN); return
    await state.clear(); await m.answer(f"Черновик #{d.id}\n\n{d.proposed_text}\n\nСообщение не отправлено.",reply_markup=MAIN)
