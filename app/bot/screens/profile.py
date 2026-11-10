"""Profile field, photo and story input screens."""

from __future__ import annotations

from app.bot.core import Flow
from app.bot.core import check_msg
from app.bot.core import delete_msg
from app.bot.core import finish_prompt
from app.bot.core import finish_prompt_keep_state
from app.bot.core import kb
from app.bot.core import private
from app.bot.core import prompt_message
from app.bot.core import router
from app.storage.db import SessionFactory
from app.storage.models import AppSetting
from app.telegram_gateway import management as telegram_management
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
import logging
import os
import secrets
import tempfile


@router.message(Flow.management_input)
async def management_input_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
    data = await state.get_data()
    action = str(data.get("management_action") or "")
    aid = int(data.get("management_account_id") or 0)
    account_ids = [int(value) for value in (data.get("management_account_ids") or [aid])]
    raw = (m.text or "").strip()
    await delete_msg(m)
    try:
        if action == "autoresponder":
            parts = [value.strip() for value in raw.split("|", 3)]
            if len(parts) != 4:
                raise ValueError(
                    "Формат: ВКЛ/ВЫКЛ | ключевые слова или - | текст ответа | пауза в секундах."
                )
            flag = parts[0].lower()
            if flag not in {
                "вкл",
                "включить",
                "on",
                "true",
                "1",
                "выкл",
                "выключить",
                "off",
                "false",
                "0",
            }:
                raise ValueError("Первое поле должно быть ВКЛ или ВЫКЛ.")
            enabled = flag in {"вкл", "включить", "on", "true", "1"}
            keywords = "" if parts[1].lower() in {"-", "все", "all"} else parts[1]
            response = parts[2]
            try:
                cooldown = int(parts[3])
            except ValueError as exc:
                raise ValueError("Пауза должна быть целым числом секунд.") from exc
            if enabled and not response:
                raise ValueError("Для включённого автоответчика укажите текст ответа.")
            if not 0 <= cooldown <= 86400:
                raise ValueError("Пауза должна быть от 0 до 86400 секунд.")
            async with SessionFactory() as db:
                for account_id in account_ids:
                    settings = {
                        f"autoresponder.{account_id}.enabled": "true" if enabled else "false",
                        f"autoresponder.{account_id}.keywords": keywords,
                        f"autoresponder.{account_id}.response": response,
                        f"autoresponder.{account_id}.cooldown_seconds": str(cooldown),
                    }
                    for key, value in settings.items():
                        row = await db.get(AppSetting, key)
                        if row:
                            row.value = value
                        else:
                            db.add(AppSetting(key=key, value=value))
                await db.commit()
            message = (
                f"Автоответчик включён для {len(account_ids)} аккаунтов. Он отвечает только в личных чатах и только при выключенном ИИ каждого аккаунта."
                if enabled
                else f"Автоответчик выключен для {len(account_ids)} аккаунтов."
            )
        elif action.startswith("profilefield_"):
            field = action.removeprefix("profilefield_")
            if field not in {"first_name", "last_name", "about", "username"}:
                raise ValueError("Неизвестное поле профиля.")
            value = "" if raw == "-" else raw
            if field == "first_name" and not value:
                raise ValueError("Имя не может быть пустым.")
            results = []
            for account_id in account_ids:
                try:
                    await telegram_management.update_profile(account_id, **{field: value})
                    results.append(f"✅ #{account_id}: поле «{field}» обновлено")
                except Exception as exc:
                    results.append(f"❌ #{account_id}: {type(exc).__name__}: {str(exc)[:140]}")
            message = "\n".join(results)
        else:
            raise ValueError("Неизвестное действие настройки аккаунта.")

        keyboard = kb(
            [
                [("↩️ К настройкам аккаунтов", "tool:account-settings:menu")],
                [("🏠 Главное меню", "nav:home")],
            ]
        )
        if action.startswith("profilefield_"):
            await finish_prompt_keep_state(m, state, message, keyboard)
        else:
            await finish_prompt(m, state, message, keyboard)
    except telegram_management.FloodWaitError as exc:
        await finish_prompt(
            m,
            state,
            f"⏳ Telegram установил FloodWait: {int(exc.seconds)} сек. Операция не повторялась автоматически.",
            kb([[("↩️ К настройкам аккаунтов", "tool:account-settings:menu")]]),
        )
    except Exception as exc:
        await prompt_message(
            m,
            state,
            f"❌ {type(exc).__name__}: {str(exc)[:400]}\n\nПроверьте ввод и попробуйте ещё раз.",
        )


@router.message(Flow.management_photo)
async def management_photo_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
    photo = m.photo[-1] if m.photo else None
    if not photo and m.document and (m.document.mime_type or "").startswith("image/"):
        photo = m.document
    if not photo:
        await prompt_message(m, state, "Отправьте изображение как фото или файл.")
        return
    if int(getattr(photo, "file_size", 0) or 0) > 10 * 1024 * 1024:
        await prompt_message(m, state, "Изображение больше 10 МБ. Отправьте файл меньшего размера.")
        return
    data = await state.get_data()
    aid = int(data.get("management_account_id") or 0)
    account_ids = [int(value) for value in (data.get("management_account_ids") or [aid])]
    original_name = getattr(photo, "file_name", None) or "profile.jpg"
    extension = os.path.splitext(original_name)[1].lower()
    if extension not in {".jpg", ".jpeg", ".png"}:
        extension = ".jpg"
    path = os.path.join(
        tempfile.gettempdir(),
        f"atciv-profile-{aid}-{secrets.token_hex(8)}{extension}",
    )
    try:
        await m.bot.download(photo, destination=path)
        results = []
        for account_id in account_ids:
            try:
                await telegram_management.set_profile_photo(account_id, path)
                results.append(f"✅ #{account_id}: фото обновлено")
            except Exception as exc:
                results.append(f"❌ #{account_id}: {type(exc).__name__}: {str(exc)[:120]}")
        photo_keyboard = kb(
            [
                [
                    (
                        "↩️ К модулю",
                        data.get("module_return_callback") or "tool:account-settings:menu",
                    )
                ],
                [("🏠 Главное меню", "nav:home")],
            ]
        )
        if data.get("module_return_callback") == "tool:account-settings:menu":
            await finish_prompt_keep_state(m, state, "\n".join(results), photo_keyboard)
        else:
            await finish_prompt(m, state, "\n".join(results), photo_keyboard)
    except telegram_management.FloodWaitError as exc:
        back_callback = data.get("module_return_callback") or "tool:account-settings:menu"
        flood_text = f"Telegram установил FloodWait: {int(exc.seconds)} сек. Фото не загружено."
        flood_keyboard = kb([[("↩️ Назад", back_callback)]])
        if back_callback == "tool:account-settings:menu":
            await finish_prompt_keep_state(m, state, flood_text, flood_keyboard)
        else:
            await finish_prompt(m, state, flood_text, flood_keyboard)
    except Exception as exc:
        await prompt_message(
            m, state, f"Не удалось обновить фото: {type(exc).__name__}: {str(exc)[:350]}"
        )
    finally:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            logging.exception("Could not remove temporary profile photo %s", path)


@router.message(Flow.management_story)
async def management_story_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
    media = m.photo[-1] if m.photo else None
    if not media and m.document and (m.document.mime_type or "").startswith("image/"):
        media = m.document
    if not media:
        await prompt_message(m, state, "Отправьте вертикальное фото как фото или изображение-файл.")
        return
    if int(getattr(media, "file_size", 0) or 0) > 20 * 1024 * 1024:
        await prompt_message(m, state, "Фото для истории превышает лимит загрузки 20 МБ.")
        return
    data = await state.get_data()
    aid = int(data.get("management_account_id") or 0)
    account_ids = [int(value) for value in (data.get("management_account_ids") or [aid])]
    original_name = getattr(media, "file_name", None) or "story.jpg"
    extension = os.path.splitext(original_name)[1].lower()
    if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
        extension = ".jpg"
    path = os.path.join(
        tempfile.gettempdir(), f"atciv-story-{aid}-{secrets.token_hex(8)}{extension}"
    )
    try:
        await m.bot.download(media, destination=path)
        results = []
        for account_id in account_ids:
            try:
                await telegram_management.publish_story_photo(
                    account_id, path, caption=(m.caption or "").strip()
                )
                results.append(f"✅ #{account_id}: история опубликована для контактов")
            except Exception as exc:
                results.append(f"❌ #{account_id}: {type(exc).__name__}: {str(exc)[:180]}")
        await finish_prompt_keep_state(
            m,
            state,
            "\n".join(results),
            kb(
                [
                    [("↩️ К настройке аккаунтов", "tool:account-settings:menu")],
                    [("🏠 Главное меню", "nav:home")],
                ]
            ),
        )
    except Exception as exc:
        await prompt_message(
            m, state, f"Не удалось обработать историю: {type(exc).__name__}: {str(exc)[:300]}"
        )
    finally:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            logging.exception("Could not remove temporary story image %s", path)
