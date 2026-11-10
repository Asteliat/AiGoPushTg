"""Tools hub, multi-account profile editing and account selection."""

from __future__ import annotations

from app.bot.core import CANCEL
from app.bot.core import Flow
from app.bot.core import check_cb
from app.bot.core import edit
from app.bot.core import kb
from app.bot.core import management_prompt
from app.bot.core import router
from app.storage.db import SessionFactory
from app.storage.models import Account
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from sqlalchemy import select


@router.callback_query(F.data == "tool:menu")
async def tools_menu(c: CallbackQuery):
    if not await check_cb(c):
        return
    await edit(
        c,
        "🧰 Инструменты\n\nПоиск и дополнительные инструменты для Telegram-аккаунтов.",
        kb(
            [
                [("🪪 Профиль аккаунтов", "tool:account-settings")],
                [("🔎 Поиск чатов", "tool:groups")],
                [("📣 Поиск каналов", "tool:channels")],
                [("🎯 Глобальные триггеры", "trigger:global")],
                [("⬅️ Главное меню", "nav:home")],
            ]
        ),
    )


async def render_account_settings_selection(
    c: CallbackQuery, state: FSMContext, selected: list[int] | None = None
):
    async with SessionFactory() as db:
        accounts = list(
            (
                await db.scalars(
                    select(Account).where(Account.enabled.is_(True)).order_by(Account.id)
                )
            ).all()
        )
    valid_ids = {int(account.id) for account in accounts}
    selected_ids = [int(value) for value in (selected or []) if int(value) in valid_ids]
    await state.update_data(account_settings_selected_ids=selected_ids)
    rows = [
        [
            (
                ("☑️ " if int(account.id) in selected_ids else "▫️ ")
                + f"{account.display_name[:24]} · #{account.id}",
                f"tool:account-settings:toggle:{account.id}",
            )
        ]
        for account in accounts
    ]
    rows.extend(
        [
            [
                ("🌐 Выбрать все", "tool:account-settings:all"),
                ("✖️ Снять выбор", "tool:account-settings:none"),
            ],
            [("➡️ Продолжить", "tool:account-settings:continue")],
            [("↩️ Инструменты", "tool:menu")],
        ]
    )
    await edit(
        c,
        "🪪 Профиль аккаунтов\n\n"
        "Выберите один или несколько включённых аккаунтов. "
        "Изменения будут применяться отдельно к каждому аккаунту через его изолированную Telegram-сессию.\n"
        f"Выбрано: {len(selected_ids)} из {len(accounts)}.",
        kb(rows),
    )


@router.callback_query(F.data == "tool:account-settings")
async def account_settings_start(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    await state.clear()
    await render_account_settings_selection(c, state, [])


@router.callback_query(F.data.startswith("tool:account-settings:toggle:"))
async def account_settings_toggle(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    account_id = int(c.data.rsplit(":", 1)[1])
    data = await state.get_data()
    selected = [int(value) for value in data.get("account_settings_selected_ids", [])]
    if account_id in selected:
        selected.remove(account_id)
    else:
        selected.append(account_id)
    await render_account_settings_selection(c, state, selected)


@router.callback_query(F.data == "tool:account-settings:all")
async def account_settings_select_all(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    async with SessionFactory() as db:
        ids = list(
            (
                await db.scalars(
                    select(Account.id).where(Account.enabled.is_(True)).order_by(Account.id)
                )
            ).all()
        )
    await render_account_settings_selection(c, state, [int(value) for value in ids])


@router.callback_query(F.data == "tool:account-settings:none")
async def account_settings_select_none(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    await render_account_settings_selection(c, state, [])


async def _selected_account_ids(c: CallbackQuery, state: FSMContext) -> list[int] | None:
    """Selected accounts, or the selection screen again instead of a dead-end alert."""
    data = await state.get_data()
    ids = [int(value) for value in data.get("management_account_ids", [])]
    if ids:
        return ids
    await render_account_settings_selection(c, state, [])
    return None


async def render_account_settings_menu(c: CallbackQuery, state: FSMContext):
    ids = await _selected_account_ids(c, state)
    if ids is None:
        return
    async with SessionFactory() as db:
        accounts = list(
            (
                await db.scalars(select(Account).where(Account.id.in_(ids)).order_by(Account.id))
            ).all()
        )
    labels = ", ".join(f"{item.display_name[:20]} · #{item.id}" for item in accounts)
    rows = [
        [
            ("👤 Имя", "tool:account-settings:field:first_name"),
            ("👤 Фамилия", "tool:account-settings:field:last_name"),
        ],
        [
            ("📝 Bio", "tool:account-settings:field:about"),
            ("🔗 Username", "tool:account-settings:field:username"),
        ],
        [("📷 Изменить фото", "tool:account-settings:photo")],
        [("🤖 Автоответчик", "tool:account-settings:autoresponder")],
        [("⭕ Добавить историю", "tool:account-settings:story")],
        [("🔄 Выбрать другие аккаунты", "tool:account-settings")],
        [("↩️ Инструменты", "tool:menu")],
    ]
    await edit(
        c,
        "🪪 Профиль аккаунтов\n\n"
        f"Выбрано аккаунтов: {len(ids)}\n{labels}\n\n"
        "Нажмите поле, которое хотите изменить. После успешного изменения Telegram "
        "имя, фамилия и username автоматически синхронизируются с названием аккаунта в боте.",
        kb(rows),
    )


@router.callback_query(F.data == "tool:account-settings:continue")
async def account_settings_continue(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    data = await state.get_data()
    ids = [int(value) for value in data.get("account_settings_selected_ids", [])]
    if not ids:
        await c.answer("Отметьте хотя бы один аккаунт.", show_alert=True)
        return
    async with SessionFactory() as db:
        valid = list(
            (
                await db.scalars(
                    select(Account.id)
                    .where(Account.id.in_(ids), Account.enabled.is_(True))
                    .order_by(Account.id)
                )
            ).all()
        )
    ids = [int(value) for value in valid]
    if not ids:
        await c.answer("Выбранные аккаунты отключены или удалены.", show_alert=True)
        return
    await state.update_data(
        management_account_ids=ids,
        management_account_id=ids[0],
        module_selection_module=None,
        module_return_callback="tool:account-settings:menu",
        return_callback="tool:account-settings",
    )
    await render_account_settings_menu(c, state)


@router.callback_query(F.data == "tool:account-settings:menu")
async def account_settings_menu_callback(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    await render_account_settings_menu(c, state)


@router.callback_query(F.data.startswith("tool:account-settings:field:"))
async def account_settings_field(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    field = c.data.rsplit(":", 1)[1]
    prompts = {
        "first_name": "Отправьте новое имя для всех выбранных аккаунтов (до 64 символов).",
        "last_name": "Отправьте новую фамилию для всех выбранных аккаунтов. Отправьте «-», чтобы очистить фамилию.",
        "about": "Отправьте новый Bio для всех выбранных аккаунтов (до 70 символов). Отправьте «-», чтобы очистить Bio.",
        "username": "Отправьте новый username без @. Отправьте «-», чтобы удалить username. Username должен быть свободен в Telegram.",
    }
    if field not in prompts:
        await c.answer("Неизвестное поле профиля.", show_alert=True)
        return
    ids = await _selected_account_ids(c, state)
    if ids is None:
        return
    await management_prompt(c, state, f"profilefield_{field}", ids[0], prompts[field])


@router.callback_query(F.data == "tool:account-settings:autoresponder")
async def account_settings_autoresponder(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    ids = await _selected_account_ids(c, state)
    if ids is None:
        return
    await management_prompt(
        c,
        state,
        "autoresponder",
        ids[0],
        "Автоответчик работает только в личных сообщениях и только когда ИИ выключен на соответствующем аккаунте.\n\n"
        "Формат: ВКЛ/ВЫКЛ | ключевые слова или - | текст ответа | пауза в секундах.\n"
        "Пример: ВКЛ | привет, цена | Привет! Чем помочь? | 60",
    )


@router.callback_query(F.data == "tool:account-settings:photo")
async def account_settings_photo(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    ids = await _selected_account_ids(c, state)
    if ids is None:
        return
    await state.update_data(
        management_action="photo",
        management_account_id=ids[0],
        return_callback="tool:account-settings:menu",
        photo_per_account=False,
        module_selection_module=None,
        module_return_callback="tool:account-settings:menu",
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
    )
    await state.set_state(Flow.management_photo)
    await edit(
        c,
        "Отправьте одно фото, которое нужно установить на все выбранные аккаунты. Максимум 10 МБ.",
        CANCEL,
    )


@router.callback_query(F.data == "tool:account-settings:story")
async def account_settings_story(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    ids = await _selected_account_ids(c, state)
    if ids is None:
        return
    await state.update_data(
        management_action="story",
        management_account_id=ids[0],
        return_callback="tool:account-settings:menu",
        module_selection_module=None,
        module_return_callback="tool:account-settings:menu",
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
    )
    await state.set_state(Flow.management_story)
    await edit(
        c,
        "Отправьте вертикальное фото для истории. Можно добавить подпись к фото. "
        "История будет опубликована от каждого выбранного аккаунта и доступна его контактам. "
        "Telegram сам проверит Premium, лимиты и возможность публикации.",
        CANCEL,
    )


@router.callback_query(F.data == "tool:groups")
async def tool_groups_accounts(c: CallbackQuery):
    if not await check_cb(c):
        return
    async with SessionFactory() as db:
        accounts = list(
            (
                await db.scalars(
                    select(Account).where(Account.enabled.is_(True)).order_by(Account.id)
                )
            ).all()
        )
    if not accounts:
        await edit(
            c,
            "🔎 Поиск чатов\n\nНет включённых аккаунтов. Сначала подключите и включите Telegram-аккаунт.",
            kb(
                [
                    [("👤 Аккаунты", "acc:list")],
                    [("⬅️ Инструменты", "tool:menu")],
                ]
            ),
        )
        return
    rows = [[(f"🌐 Все аккаунты · {len(accounts)}", "tool:groups:all")]]
    rows += [
        [(f"👤 {a.display_name[:32]} · #{a.id}", f"tool:groups:account:{a.id}")] for a in accounts
    ]
    rows += [[("⬅️ Инструменты", "tool:menu")]]
    await edit(
        c,
        "🔎 Поиск Telegram-групп\n\nМожно искать сразу всеми включёнными аккаунтами — так Telegram-native поиск соберёт больше вариантов. Либо выберите один аккаунт. В результатах останутся только группы и супергруппы; каналы будут отсеяны.",
        kb(rows),
    )


@router.callback_query(F.data == "tool:groups:all")
async def tool_groups_all_accounts(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    async with SessionFactory() as db:
        accounts = list(
            (
                await db.scalars(
                    select(Account).where(Account.enabled.is_(True)).order_by(Account.id)
                )
            ).all()
        )
    if not accounts:
        await c.answer("Нет включённых аккаунтов.", show_alert=True)
        return
    ids = [int(account.id) for account in accounts]
    await state.clear()
    await state.update_data(
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback="tool:menu",
        chat_search_account_id=ids[0],
        chat_search_account_ids=ids,
    )
    await state.set_state(Flow.chat_search_query)
    await edit(
        c,
        f"🌐 Поиск всеми аккаунтами · {len(ids)} аккаунтов\n\n"
        "Введите тему или несколько ключевых слов. Например: «Python разработка», "
        "«настольные игры», «дизайн».\n\n"
        "Каждый включённый аккаунт выполнит собственный поиск в Telegram. "
        "Результаты объединятся без дублей, затем ссылки будут проверены через Telegram API. "
        "Поисковики работают параллельно; если источник показывает CAPTCHA или ограничивает запросы, "
        "он временно отключается, а остальные продолжают поиск.",
        CANCEL,
    )


@router.callback_query(F.data.startswith("tool:groups:account:"))
async def tool_groups_account(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    aid = int(c.data.rsplit(":", 1)[1])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account or not account.enabled:
        await c.answer("Аккаунт не найден или выключен.", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback="tool:menu",
        chat_search_account_id=aid,
        chat_search_account_ids=[aid],
    )
    await state.set_state(Flow.chat_search_query)
    await edit(
        c,
        f"🔎 Поиск Telegram-групп · {account.display_name} · аккаунт #{aid}\n\n"
        "Введите тему или несколько ключевых слов. Например: «Python разработка», "
        "«настольные игры», «дизайн».\n\n"
        "Поиск использует Telegram и несколько поисковых систем. Каждая найденная ссылка "
        "проверяется через Telegram API: каналы, профили, боты и нерабочие ссылки исключаются. "
        "Приватные группы попадут в результаты только если приглашение доступно и Telegram "
        "позволяет проверить его тип.",
        CANCEL,
    )
