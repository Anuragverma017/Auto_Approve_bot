import asyncio
import os
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    ChatJoinRequest,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")  # without @ e.g. AutoApproveRobot


# --------------------------
# /start  -> message + INLINE buttons (only)
# --------------------------
async def cmd_start(message: Message):
    if not BOT_USERNAME:
        await message.answer("❌ BOT_USERNAME missing in .env file")
        return

    text = (
        "Add This Bot To Your Channel To Accept Join Requests Automatically 😊\n\n"
        "➕ Just add me as admin with *Add Members* rights in your private "
        "channel or group. I will auto-approve all join requests ✅"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Add to channel",
                    url=f"https://t.me/{BOT_USERNAME}?startchannel=auto_approve",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Add to group",
                    url=f"https://t.me/{BOT_USERNAME}?startgroup=auto_approve",
                )
            ],
        ]
    )

    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")


# ---------------------------------
# Auto-approve join requests here
# ---------------------------------
async def handle_join_request(event: ChatJoinRequest, bot: Bot):
    try:
        await bot.approve_chat_join_request(
            chat_id=event.chat.id,
            user_id=event.from_user.id,
        )

        try:
            await bot.send_message(
                event.from_user.id,
                f"✅ Your request to join **{event.chat.title}** has been approved automatically.",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    except Exception as e:
        print("Error approving join request:", e)


# ---------------------
# MAIN
# ---------------------
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("❌ BOT_TOKEN missing in environment")

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    dp.message.register(cmd_start, CommandStart())
    dp.chat_join_request.register(handle_join_request)

    print("Auto-approve bot running...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
