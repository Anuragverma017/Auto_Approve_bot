import asyncio
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    ChatJoinRequest,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from supabase import create_client, Client
import razorpay

# ---------------- ENV LOAD ----------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")  # without @ e.g. Getai_approvedbot

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")

PLAN_DURATION_DAYS = int(os.getenv("PLAN_DURATION_DAYS", "30"))
BASIC_PRICE_PAISE = int(os.getenv("BASIC_PRICE_PAISE", "69900"))
PRO_PRICE_PAISE = int(os.getenv("PRO_PRICE_PAISE", "149900"))
PREMIUM_PRICE_PAISE = int(os.getenv("PREMIUM_PRICE_PAISE", "249900"))

if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN missing in .env file")

assert SUPABASE_URL and SUPABASE_KEY, "Set SUPABASE_URL and SUPABASE_KEY / SUPABASE_SERVICE_ROLE_KEY in .env"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

RAZORPAY_CLIENT: Optional[razorpay.Client] = None
if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
    try:
        RAZORPAY_CLIENT = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    except Exception as e:
        print("Razorpay client init error:", e)

# ---------------- SUBSCRIPTION HELPERS ----------------


def sp_get_subscription(user_id: int) -> Optional[dict]:
    try:
        res = (
            supabase.table("user_subscriptions")
            .select("*")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as ex:
        print("sp_get_subscription error:", ex)
        return None


def is_plan_active(sub: dict) -> bool:
    try:
        exp = sub.get("expires_at")
        if not exp:
            return False
        exp_dt = datetime.fromisoformat(str(exp).replace("Z", "")).astimezone(timezone.utc)
        return datetime.now(timezone.utc) < exp_dt
    except Exception:
        return False


def format_plan_status(user_id: int) -> str:
    sub = sp_get_subscription(user_id)
    if not sub:
        return "🔴 **No active plan found.**\nUse `/upgrade` to purchase any plan."

    if not is_plan_active(sub):
        return "🟠 **Your plan has expired.**\nUse `/upgrade` to renew."

    expires = str(sub.get("expires_at")).replace("Z", "")[:19]
    label = sub.get("plan_label") or "Unknown Plan"

    return (
        f"🟢 **Active Plan: {label}**\n"
        f"📅 **Expires at:** `{expires}`\n\n"
        "Thank you for being a premium user! 🎉"
    )


def has_active_plan(user_id: int) -> bool:
    sub = sp_get_subscription(user_id)
    return bool(sub and is_plan_active(sub))


def sp_get_latest_payment_link(user_id: int, plan_id: str) -> Optional[dict]:
    try:
        res = (
            supabase.table("user_payment_links")
            .select("*")
            .eq("user_id", user_id)
            .eq("plan_id", plan_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as ex:
        print("sp_get_latest_payment_link error:", ex)
        return None


def sp_apply_successful_payment(user_id: int, payment_row: dict) -> datetime:
    now = datetime.now(timezone.utc)
    plan_id = payment_row.get("plan_id") or "unknown"
    plan_label = payment_row.get("plan_label") or plan_id.upper()
    duration_days = int(payment_row.get("duration_days") or PLAN_DURATION_DAYS)

    sub = sp_get_subscription(user_id)
    if sub and is_plan_active(sub):
        try:
            base = datetime.fromisoformat(str(sub["expires_at"]).replace("Z", "")).astimezone(timezone.utc)
        except Exception:
            base = now
    else:
        base = now

    new_exp = base + timedelta(days=duration_days)

    payload = {
        "user_id": user_id,
        "plan_id": plan_id,
        "plan_label": plan_label,
        "expires_at": new_exp.isoformat(),
        "updated_at": now.isoformat(),
    }

    try:
        supabase.table("user_subscriptions").upsert(payload, on_conflict="user_id").execute()
    except Exception as ex:
        print("sp_apply_successful_payment upsert sub error:", ex)

    try:
        supabase.table("user_payment_links").update({"status": "paid"}).eq("id", payment_row["id"]).execute()
    except Exception as ex:
        print("sp_apply_successful_payment update payment error:", ex)

    return new_exp


def create_payment_link(user_id: int, amount_paise: int, plan_id: str, plan_label: str) -> Optional[str]:
    if not RAZORPAY_CLIENT:
        print("create_payment_link: Razorpay client not configured")
        return None

    # Razorpay ko emoji pasand nahi – ASCII clean label
    try:
        ascii_label = plan_label.encode("ascii", "ignore").decode().strip()
        if not ascii_label:
            ascii_label = plan_id.upper()
    except Exception:
        ascii_label = plan_id.upper()

    try:
        res = RAZORPAY_CLIENT.payment_link.create(
            {
                "amount": amount_paise,
                "currency": "INR",
                "description": f"GetAIPilot Subscription - {ascii_label}",
                "customer": {"name": str(user_id)},
                "notify": {"sms": True, "email": False},
                "callback_url": "https://razorpay.com/",
                "callback_method": "get",
            }
        )
        p_url = res.get("short_url") or res.get("url")
        p_id = res.get("id")

        supabase.table("user_payment_links").insert(
            {
                "user_id": user_id,
                "plan_id": plan_id,
                "plan_label": plan_label,
                "price_paise": amount_paise,
                "duration_days": PLAN_DURATION_DAYS,
                "paymentlink_id": p_id,
                "paymentlink_url": p_url,
                "status": "created",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "raw": res,
            }
        ).execute()

        return p_url
    except Exception as ex:
        print("create_payment_link error:", ex)
        return None


# ---------------- PLAN TEXTS (same as joining bot) ----------------

PLANS_HEADER_TEXT = """
✨ **GetAIPilot — Plans**

🎁 **Free with any plan:**
• Auto approval bot — @Getai\_approvedbot
• Join Tracking bot — @Getai\_joincountbot


💠 **BASIC — ₹699 / 30 days**
• Unlimited auto-forwarding between your selected chats
• Choose sources & targets easily
• Start/Stop forwarding anytime
• Manage mappings (remove sources/targets)
• ⚠️ High-size file sending is NOT included

────────────────────────

⚡️ **PRO — ₹1499 / 30 days**
• Everything in BASIC
• Text replacement filters (@old → @new)
• Show / delete one / delete all filters
• Custom delay control between forwards
• ✅ High-size media & file sending supported

────────────────────────

💎 **PREMIUM — ₹2499 / 30 days**
• Everything in PRO
• Add custom text at the START of every forward
• Add custom text at the END of every forward
• Blacklist words (auto-remove from text)
• ✅ High-size media & file sending supported

_Every payment extends your expiry by +30 days._
"""

BASIC_PLAN_TEXT = """
💠 **BASIC — ₹699 / 30 days**

🎁 **Free with this plan:**
• Auto approval bot — @Getai\_approvedbot
• Join Tracking bot — @Getai\_joincountbot

**Features:**
• Unlimited auto-forwarding between your selected chats
• Choose sources & targets easily
• Start/Stop forwarding anytime
• Manage mappings (remove sources/targets)
• ⚠️ High-size file sending is NOT included

**Commands in this plan (AutoForward bot):**
• `/incoming`, `/outgoing`, `/work`, `/stop`
• `/remove_incoming`, `/remove_outgoing`

⚠️ High-size files not supported

_Validity: 30 days • Every renewal adds +30 days._
"""

PRO_PLAN_TEXT = """
⚡️ **PRO — ₹1499 / 30 days**

🎁 **Free with this plan:**
• Auto approval bot — @Getai\_approvedbot
• Join Tracking bot — @Getai\_joincountbot

**Features:**
• Everything in BASIC
• Text replacement filters (@old → @new)
• Show / delete one / delete all filters
• Custom delay control between forwards
• ✅ High-size media & file sending supported

_Validity: 30 days • Every renewal adds +30 days._
"""

PREMIUM_PLAN_TEXT = """
💎 **PREMIUM — ₹2499 / 30 days**

🎁 **Free with this plan:**
• Auto approval bot — @Getai\_approvedbot
• Join Tracking bot — @Getai\_joincountbot

**Features:**
• Everything in PRO
• Add custom text at the START/END of every forward
• Blacklist words (auto-remove from text)
• ✅ High-size media & file sending supported

_Validity: 30 days • Every renewal adds +30 days._
"""


# ---------------- COMMON UPGRADE UI HELPERS ----------------


async def show_plans_root(message_or_cb):
    user_id = message_or_cb.from_user.id if isinstance(message_or_cb, Message) else message_or_cb.from_user.id
    status = format_plan_status(user_id)
    txt = status + "\n\n" + PLANS_HEADER_TEXT

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💠 View BASIC", callback_data="plans_basic")],
            [InlineKeyboardButton(text="⚡ View PRO", callback_data="plans_pro")],
            [InlineKeyboardButton(text="💎 View PREMIUM", callback_data="plans_premium")],
        ]
    )

    if isinstance(message_or_cb, Message):
        await message_or_cb.answer(txt, reply_markup=kb, parse_mode="Markdown")
    else:
        await message_or_cb.message.edit_text(txt, reply_markup=kb, parse_mode="Markdown")


async def show_payment_created(cb: CallbackQuery, plan_id: str, plan_label: str, amount_paise: int):
    user_id = cb.from_user.id

    existing = sp_get_latest_payment_link(user_id, plan_id)
    if existing and (existing.get("status") or "").lower() == "created":
        link_url = existing.get("paymentlink_url")
    else:
        link_url = create_payment_link(user_id, amount_paise, plan_id, plan_label)

    if not link_url:
        await cb.message.edit_text(
            "❌ Unable to create payment link right now.\n"
            "Possible reason: Razorpay rate-limit / config issue.\n"
            "⏳ Thodi der baad fir se try karo.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to Plans", callback_data="plans_root")]]
            ),
            parse_mode="Markdown",
        )
        return

    txt = (
        "🔗 **Payment Link Created**\n"
        f"Plan: {plan_label} (₹{amount_paise/100:.0f} / 30 days)\n\n"
        "Payment complete hone ke baad **Verify** dabana mat bhoolna."
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Pay Now", url=link_url)],
            [InlineKeyboardButton(text="✅ I have paid — Verify", callback_data=f"verify_{plan_id}")],
            [InlineKeyboardButton(text="⬅️ Back to Plans", callback_data="plans_root")],
        ]
    )

    await cb.message.edit_text(txt, reply_markup=kb, parse_mode="Markdown")


async def handle_verify(cb: CallbackQuery, plan_id: str):
    user_id = cb.from_user.id

    if not RAZORPAY_CLIENT:
        await cb.message.edit_text(
            "❌ Razorpay client not configured on this bot.\nPlease contact support.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to Plans", callback_data="plans_root")]]
            ),
            parse_mode="Markdown",
        )
        return

    row = sp_get_latest_payment_link(user_id, plan_id)
    if not row:
        await cb.message.edit_text(
            "❌ No recent payment link found for this plan.\nUse /upgrade → Buy again.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to Plans", callback_data="plans_root")]]
            ),
            parse_mode="Markdown",
        )
        return

    if (row.get("status") or "").lower() == "paid":
        msg = "✅ Payment already verified.\n\n" + format_plan_status(user_id)
        await cb.message.edit_text(
            msg,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to Plans", callback_data="plans_root")]]
            ),
            parse_mode="Markdown",
        )
        return

    plink_id = row.get("paymentlink_id")
    if not plink_id:
        await cb.message.edit_text(
            "❌ This payment link record is missing an id.\nPlease create a new one via /upgrade.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to Plans", callback_data="plans_root")]]
            ),
            parse_mode="Markdown",
        )
        return

    try:
        info = RAZORPAY_CLIENT.payment_link.fetch(plink_id)
    except Exception as ex:
        await cb.message.edit_text(
            f"❌ Failed to verify payment:\n`{ex}`",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to Plans", callback_data="plans_root")]]
            ),
            parse_mode="Markdown",
        )
        return

    status = (info or {}).get("status", "").lower()
    if status != "paid":
        purl = row.get("paymentlink_url")
        rows = []
        if purl:
            rows.append([InlineKeyboardButton(text="💳 Pay Now", url=purl)])
        rows.append([InlineKeyboardButton(text="⬅️ Back to Plans", callback_data="plans_root")])

        await cb.message.edit_text(
            f"⚠️ Payment is not completed yet.\n"
            f"Current status: `{status or 'unknown'}`.\n\n"
            "Please finish payment using *Pay Now*, then press **Verify** again.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode="Markdown",
        )
        return

    new_exp = sp_apply_successful_payment(user_id, row)
    new_exp_str = new_exp.strftime("%Y-%m-%d %H:%M UTC")

    msg = (
        "✅ **Payment verified successfully!**\n\n"
        f"Your plan is now active until: `{new_exp_str}`\n\n"
        "Ab aap **Auto Forward bot + Join Counter + Auto Approve bot** sab use kar sakte ho. 🎉"
    )

    await cb.message.edit_text(
        msg,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Back to Plans", callback_data="plans_root")]]
        ),
        parse_mode="Markdown",
    )


# ---------------- BOT COMMAND HANDLERS ----------------


async def cmd_start(message: Message):
    if not BOT_USERNAME:
        await message.answer("❌ BOT_USERNAME missing in .env file")
        return

    if not has_active_plan(message.from_user.id):
        await message.answer(
            "🔒 **No active subscription found for this account.**\n\n"
            "Is Auto Approve bot ka use karne ke liye pehle plan lena zaroori hai.\n"
            "👉 `/upgrade` command run karke plan purchase karein,\n"
            "phir join requests automatically approve ho jayenge.",
            parse_mode="Markdown",
        )
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


async def handle_join_request(event: ChatJoinRequest, bot: Bot):
    user_id = event.from_user.id

    # 🔒 subscription gate
    if not has_active_plan(user_id):
        print(f"Skip auto-approve for user {user_id}: no active plan")
        try:
            await bot.send_message(
                user_id,
                "🔒 **No active subscription found.**\n\n"
                "Aapka join request abhi auto-approve nahi ho sakta.\n"
                "👉 Pehle `/upgrade` command run karke plan purchase karein,\n"
                "phir dobara join request bhejein.",
                parse_mode="Markdown",
            )
        except Exception:
            # user ne kabhi /start nahi kiya ho to DM fail ho sakta hai – ignore
            pass
        return

    # ✅ user has active plan → approve
    try:
        await bot.approve_chat_join_request(
            chat_id=event.chat.id,
            user_id=user_id,
        )

        try:
            await bot.send_message(
                user_id,
                f"✅ Your request to join **{event.chat.title}** has been approved automatically.",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    except Exception as e:
        print("Error approving join request:", e)


# /upgrade & /upgrade_status commands

async def cmd_upgrade(message: Message):
    await show_plans_root(message)


async def cmd_upgrade_status(message: Message):
    await message.answer(format_plan_status(message.from_user.id), parse_mode="Markdown")


# ---------------- CALLBACK HANDLERS ----------------


async def cb_plans_root(cb: CallbackQuery):
    await show_plans_root(cb)


async def cb_plans_basic(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Back to Plans", callback_data="plans_root")],
            [InlineKeyboardButton(text="💳 Buy ₹699 / 30 days", callback_data="buy_basic")],
        ]
    )
    await cb.message.edit_text(BASIC_PLAN_TEXT, reply_markup=kb, parse_mode="Markdown")


async def cb_plans_pro(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Back to Plans", callback_data="plans_root")],
            [InlineKeyboardButton(text="💳 Buy ₹1499 / 30 days", callback_data="buy_pro")],
        ]
    )
    await cb.message.edit_text(PRO_PLAN_TEXT, reply_markup=kb, parse_mode="Markdown")


async def cb_plans_premium(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Back to Plans", callback_data="plans_root")],
            [InlineKeyboardButton(text="💳 Buy ₹2499 / 30 days", callback_data="buy_premium")],
        ]
    )
    await cb.message.edit_text(PREMIUM_PLAN_TEXT, reply_markup=kb, parse_mode="Markdown")


async def cb_buy_basic(cb: CallbackQuery):
    await show_payment_created(cb, "basic", "💠 BASIC", BASIC_PRICE_PAISE)


async def cb_buy_pro(cb: CallbackQuery):
    await show_payment_created(cb, "pro", "⚡️ PRO", PRO_PRICE_PAISE)


async def cb_buy_premium(cb: CallbackQuery):
    await show_payment_created(cb, "premium", "💎 PREMIUM", PREMIUM_PRICE_PAISE)


async def cb_verify_basic(cb: CallbackQuery):
    await handle_verify(cb, "basic")


async def cb_verify_pro(cb: CallbackQuery):
    await handle_verify(cb, "pro")


async def cb_verify_premium(cb: CallbackQuery):
    await handle_verify(cb, "premium")


# ---------------- MAIN ----------------


async def main():
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    # Commands
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_upgrade, Command("upgrade"))
    dp.message.register(cmd_upgrade_status, Command("upgrade_status"))

    # Join request
    dp.chat_join_request.register(handle_join_request)

    # Callback buttons
    dp.callback_query.register(cb_plans_root, F.data == "plans_root")
    dp.callback_query.register(cb_plans_basic, F.data == "plans_basic")
    dp.callback_query.register(cb_plans_pro, F.data == "plans_pro")
    dp.callback_query.register(cb_plans_premium, F.data == "plans_premium")

    dp.callback_query.register(cb_buy_basic, F.data == "buy_basic")
    dp.callback_query.register(cb_buy_pro, F.data == "buy_pro")
    dp.callback_query.register(cb_buy_premium, F.data == "buy_premium")

    dp.callback_query.register(cb_verify_basic, F.data == "verify_basic")
    dp.callback_query.register(cb_verify_pro, F.data == "verify_pro")
    dp.callback_query.register(cb_verify_premium, F.data == "verify_premium")

    print("🤖 Auto Approve Bot with subscription running...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
