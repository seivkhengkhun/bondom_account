"""Optional Telegram appearance controls, authorized by explicit numeric IDs."""

import html

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.exc import SQLAlchemyError

from app.bot.appearance import cache, current, emoji, extracted_ids, validate_custom_emoji
from shared import bot_appearance as config
from shared.config import settings
from shared.database import AsyncSessionLocal

router = Router(name="bot_appearance_admin")


class SettingsErrorMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            return await handler(event, data)
        except SQLAlchemyError:
            config.warn_once("admin_database_failed")
            text = "Appearance settings could not be saved. Please try again."
            if hasattr(event, "data"):
                await event.answer(text, show_alert=True)
            else:
                await event.answer(text)


router.message.middleware(SettingsErrorMiddleware())
router.callback_query.middleware(SettingsErrorMiddleware())


class IsAdmin(Filter):
    async def __call__(self, event):
        return bool(event.from_user and event.from_user.id in settings.telegram_admin_ids)


class EmojiEdit(StatesGroup):
    waiting_for_id = State()


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, callback_data=data) for text, data in row
    ] for row in rows])


async def refresh():
    cache.expires = 0
    appearance = await cache.get()
    current.set(appearance)
    return appearance


@router.message(Command("admin"), IsAdmin())
async def admin_menu(message, state: FSMContext):
    await state.clear()
    await message.answer("<b>Admin</b>", reply_markup=keyboard([
        [("Bot Appearance", "appearance:menu")],
    ]))


@router.callback_query(F.data == "appearance:menu", IsAdmin())
async def appearance_menu(callback, state: FSMContext):
    await state.clear()
    await callback.message.answer("<b>Bot Appearance</b>", reply_markup=keyboard([
        [("Custom Emoji", "appearance:list")],
    ]))
    await callback.answer()


@router.callback_query(F.data == "appearance:list", IsAdmin())
async def slot_list(callback, state: FSMContext):
    await state.clear()
    appearance = await refresh()
    rows = [[(f"{slot.fallback_emoji} {key} · "
              f"{'on' if slot.enabled else 'off'} · "
              f"{'custom' if slot.custom_emoji_id else 'default'}", f"appearance:slot:{key}")]
            for key, slot in appearance.slots.items()]
    rows += [[("Disable all animations" if appearance.enabled else "Enable animations",
               "appearance:global")], [("Back", "appearance:menu")]]
    await callback.message.answer(
        "<b>Custom Emoji</b>\n\nSelect a slot to edit or preview.\n"
        "Send /emoji_id as a reply to a custom emoji to extract its ID.",
        reply_markup=keyboard(rows),
    )
    await callback.answer()


@router.callback_query(F.data == "appearance:global", IsAdmin())
async def global_toggle(callback, state: FSMContext):
    appearance = await refresh()
    async with AsyncSessionLocal() as session:
        await config.set_global_enabled(session, not appearance.enabled)
    await slot_list(callback, state)


@router.callback_query(F.data.startswith("appearance:slot:"), IsAdmin())
async def slot_details(callback, state: FSMContext):
    await state.clear()
    key = callback.data.rsplit(":", 1)[1]
    appearance = await refresh()
    slot = appearance.slots.get(key)
    if slot is None:
        await callback.answer("Unknown slot.")
        return
    await callback.message.answer(
        f"<b>Custom Emoji · {key}</b>\n\n"
        f"ID: <code>{slot.custom_emoji_id or 'Not set'}</code>\n"
        f"Fallback: {html.escape(slot.fallback_emoji)}\n"
        f"Slot: {'enabled' if slot.enabled else 'disabled'}\n"
        f"Global animations: {'enabled' if appearance.enabled else 'disabled'}",
        reply_markup=keyboard([
            [("Set / change ID", f"appearance:edit:{key}")],
            [("Disable" if slot.enabled else "Enable", f"appearance:toggle:{key}"),
             ("Preview", f"appearance:preview:{key}")],
            [("Reset to default", f"appearance:reset:{key}")],
            [("All slots", "appearance:list")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("appearance:edit:"), IsAdmin())
async def edit_slot(callback, state: FSMContext):
    key = callback.data.rsplit(":", 1)[1]
    if key not in config.DEFAULTS:
        await callback.answer("Unknown slot.")
        return
    await state.set_state(EmojiEdit.waiting_for_id)
    await state.update_data(emoji_key=key)
    await callback.message.answer(
        f"<b>Set {key}</b>\n\nSend the numeric custom emoji ID, or send/forward "
        "a message containing exactly one custom emoji. Telegram will validate it before saving.",
        reply_markup=keyboard([[("Cancel", f"appearance:slot:{key}")]]),
    )
    await callback.answer()


@router.message(EmojiEdit.waiting_for_id, IsAdmin())
async def save_emoji(message, state: FSMContext):
    key = (await state.get_data()).get("emoji_key")
    if key not in config.DEFAULTS:
        await state.clear()
        return
    ids = extracted_ids(message)
    if len(ids) > 1:
        await message.answer("Send exactly one custom emoji, or paste one numeric ID.")
        return
    custom_id = ids[0] if ids else (message.text or "").strip()
    if not custom_id:
        await message.answer("No custom emoji ID found. Send one custom emoji or its numeric ID.")
        return
    try:
        fallback = await validate_custom_emoji(message.bot, custom_id)
        async with AsyncSessionLocal() as session:
            await config.save_slot(session, key, custom_id, True, fallback)
    except (ValueError, TelegramAPIError):
        await message.answer("The ID could not be validated. Check the emoji and try again.")
        return
    except Exception:
        config.warn_once("admin_save_failed")
        await message.answer("Settings could not be saved. Try again.")
        return
    await state.clear()
    await refresh()
    await message.answer(f"{emoji(key)} <b>{key} saved</b>",
                         reply_markup=keyboard([[("Back to slot", f"appearance:slot:{key}")]]))


@router.callback_query(F.data.startswith("appearance:toggle:") |
                       F.data.startswith("appearance:reset:") |
                       F.data.startswith("appearance:preview:"), IsAdmin())
async def slot_action(callback, state: FSMContext):
    _, action, key = callback.data.split(":")
    appearance = await refresh()
    slot = appearance.slots.get(key)
    if slot is None:
        await callback.answer("Unknown slot.")
        return
    if action == "preview":
        await callback.message.answer(f"{emoji(key)} <b>{key.title()} preview</b>\n"
                                      f"Fallback: {html.escape(slot.fallback_emoji)}")
    else:
        async with AsyncSessionLocal() as session:
            if action == "reset":
                await config.reset_slot(session, key)
            else:
                await config.save_slot(session, key, slot.custom_emoji_id,
                                       not slot.enabled, slot.fallback_emoji)
        await refresh()
        await callback.message.answer("Saved.", reply_markup=keyboard([
            [("Back to slot", f"appearance:slot:{key}")],
        ]))
    await callback.answer()


@router.message(Command("emoji_id"), IsAdmin())
async def extract_emoji(message):
    ids = extracted_ids(message.reply_to_message or message)
    text = "\n".join(f"<code>{config.validate_id(value)}</code>" for value in ids)
    await message.answer(text or "Reply with /emoji_id to a message containing a custom emoji.")
