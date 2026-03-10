"""
core/bot_handlers.py
--------------------
Telegram bot command and callback handlers.

Covers:
  - /start  onboarding flow
  - Alert categories (high / medium / low risk)
  - Wishlist management
  - Top calls display
  - Sniper & Swap menu navigation
"""

import asyncio
import logging
import os

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from database.firebase import (
    activate_trial,
    add_to_wishlist,
    alert_exists,
    get_alerts_by_risk,
    get_recent_alerts,
    get_user_subscription,
    get_wishlist,
    record_terms_agreement,
    remove_from_wishlist,
    save_alert,
)
from scanner.token_scanner import TokenScanner

logger = logging.getLogger(__name__)

MINI_APP_URL   = os.getenv("MINI_APP_URL", "")
PAGE_SIZE      = 6
_scanner       = TokenScanner()

TERMS_TEXT = (
    "⚠️ <b>Terms of Service</b>\n\n"
    "<b>1. Not Financial Advice:</b> All data is for research only.\n\n"
    "<b>2. High Risk:</b> Crypto markets are extremely volatile.\n\n"
    "<b>3. No Guarantees:</b> Past performance ≠ future results.\n\n"
    "<b>4. Your Responsibility:</b> Always DYOR.\n\n"
    "Tap <b>I Agree</b> to continue."
)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user    = update.effective_user
    user_id = str(user.id)

    # Capture referral payload
    if context.args and context.args[0].startswith("ref_"):
        potential = context.args[0].split("_")[1]
        if potential.isdigit() and potential != user_id:
            context.user_data["referrer_id"] = potential

    sub = get_user_subscription(user_id)

    # First-time user — show T&C
    if sub is None or sub["status"] == "pending_agreement":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ I Agree & Get Started", callback_data="agree_terms")]])
        await _send_or_edit(update, TERMS_TEXT, kb)
        return

    # Build main menu
    is_active = sub and sub["status"] == "active"
    is_trial  = is_active and sub["data"].get("plan") == "trial"

    caption = "👋 <b>Welcome back to MoonshotAlpha</b>\n\nYour intelligence terminal for Solana."
    if sub["status"] == "expired":
        caption = "⚠️ <b>Subscription expired.</b> Renew to access live alerts."

    rows = []
    if is_trial:
        rows.append([InlineKeyboardButton("🧪 Preview Alerts", callback_data="test_showcase")])
    elif is_active:
        rows.append([InlineKeyboardButton("🚀 Alert Categories", callback_data="alert_categories")])

    if is_active:
        rows.append([InlineKeyboardButton("⭐ Wishlist", callback_data="wishlist_menu")])

    rows += [
        [InlineKeyboardButton("🔫 Sniper", callback_data="sniper_menu"),
         InlineKeyboardButton("🔄 Swap",   callback_data="swap_menu")],
        [InlineKeyboardButton("🏆 Top Calls",          callback_data="top_calls")],
        [InlineKeyboardButton("📢 Community",           url="https://t.me/MoonshotAlphaCommunity")],
        [InlineKeyboardButton("ℹ️ How It Works",        callback_data="how_it_works")],
        [InlineKeyboardButton("💬 Support",             callback_data="support")],
    ]

    await _send_or_edit(update, caption, InlineKeyboardMarkup(rows), photo="Moon.png")


# ---------------------------------------------------------------------------
# Callback dispatcher
# ---------------------------------------------------------------------------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    user_id = str(query.from_user.id)
    data    = query.data

    # --- Wishlist quick-actions (no answer needed first) ---
    if data.startswith("wishlist_add_") and not data.endswith("_prompt"):
        ca = data.split("_", 2)[2]
        token = await _scanner.get_pair_data(ca)
        if token:
            ok, msg = await add_to_wishlist(user_id, ca, {"chain": token.chain, "name": token.name, "symbol": token.symbol})
        else:
            ok, msg = False, "❌ Token not found."
        await query.answer(msg[:200], show_alert=True)
        return

    if data.startswith("wishlist_remove_") and not data.endswith("_prompt"):
        ca = data.split("_", 2)[2]
        ok, msg = remove_from_wishlist(user_id, ca)
        await query.answer(msg[:200], show_alert=False)
        if ok:
            await _show_remove_wishlist(update, context)
        return

    try:
        await query.answer()
    except Exception:
        pass

    # --- Route ---
    if data == "agree_terms":
        await _handle_agree_terms(update, context, user_id)

    elif data == "start_menu":
        await start(update, context)

    elif data == "sniper_menu":
        await _sniper_menu(update)

    elif data == "swap_menu":
        await _swap_menu(update)

    elif data == "wishlist_menu":
        await _wishlist_menu(update)

    elif data == "wishlist_add_prompt":
        context.user_data["state"] = "AWAITING_WISHLIST_ADD"
        await query.edit_message_caption(
            caption="Send the <b>Contract Address (CA)</b> of the token to watch.\n\n✅ Supports: Solana, Ethereum, BSC, Base",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Cancel", callback_data="wishlist_menu")]]),
            parse_mode=ParseMode.HTML,
        )

    elif data == "wishlist_view":
        await _wishlist_view(update, user_id)

    elif data == "wishlist_remove_prompt":
        await _show_remove_wishlist(update, context)

    elif data == "top_calls":
        await _top_calls(update, context, user_id)

    elif data == "test_showcase":
        await _test_showcase(update, context, user_id)

    elif data == "alert_categories":
        await _alert_categories(update, user_id)

    elif data.startswith("confirm_risk_"):
        await _risk_warning(update, data.split("_")[2])

    elif data.startswith("view_alerts_"):
        parts = data.split("_")
        await _view_alerts(update, context, user_id, parts[2], int(parts[3]))

    elif data == "toggle_mute_alerts":
        await _toggle_mute(update, user_id)

    elif data == "how_it_works":
        await _how_it_works(update)

    elif data in ("faq", "glossary", "support"):
        await _info_page(update, data)


# ---------------------------------------------------------------------------
# Handler implementations
# ---------------------------------------------------------------------------

async def _handle_agree_terms(update, context, user_id):
    if context.user_data.get("is_agreeing"):
        return
    context.user_data["is_agreeing"] = True
    await update.callback_query.message.delete()
    referrer = context.user_data.pop("referrer_id", None)
    record_terms_agreement(user_id, update.effective_user.full_name, referrer)
    activate_trial(user_id)
    await start(update, context)
    context.user_data["is_agreeing"] = False


async def _sniper_menu(update):
    text = (
        "🔫 <b>Auto-Sniper</b>\n\n"
        "Automated token entry with two modes:\n\n"
        "• <b>Safe Mode</b> — runs full rug check before firing\n"
        "• <b>Degen Mode</b> — fires fast on any new pair\n\n"
        "Open the Agency Mini App to configure and launch."
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="start_menu")]])
    try:
        media = InputMediaPhoto(media=open("sniper.png", "rb"), caption=text, parse_mode=ParseMode.HTML)
        await update.callback_query.message.edit_media(media=media, reply_markup=kb)
    except Exception:
        await update.callback_query.edit_message_caption(caption=text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _swap_menu(update):
    text = (
        "🔄 <b>Swap Engine</b>\n\n"
        "Instant token swaps via Jupiter + Jito bundles.\n\n"
        "• Best-route quotes across all Solana DEXes\n"
        "• MEV-protected via Jito bundle submission\n"
        "• Configurable slippage\n\n"
        "Open the Agency Mini App → SWAP tab."
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="start_menu")]])
    try:
        media = InputMediaPhoto(media=open("swap.png", "rb"), caption=text, parse_mode=ParseMode.HTML)
        await update.callback_query.message.edit_media(media=media, reply_markup=kb)
    except Exception:
        await update.callback_query.edit_message_caption(caption=text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _wishlist_menu(update):
    text = "⭐ <b>Wishlist</b>\n\nTrack tokens you're watching. We'll alert you on major moves."
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Token",       callback_data="wishlist_add_prompt")],
        [InlineKeyboardButton("👁️ View Watchlist",  callback_data="wishlist_view")],
        [InlineKeyboardButton("➖ Remove Token",    callback_data="wishlist_remove_prompt")],
        [InlineKeyboardButton("« Back",             callback_data="start_menu")],
    ])
    await update.callback_query.edit_message_caption(caption=text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _wishlist_view(update, user_id):
    wl = get_wishlist(user_id)
    if not wl:
        text = "Your wishlist is empty. Use ➕ Add Token to start tracking."
    else:
        text = "<b>👁️ Watched Tokens:</b>\n\n"
        for t in wl:
            emoji = "🟣" if t.get("chain") == "solana" else "🔵"
            text += f"{emoji} <b>{t.get('name')} (${t.get('symbol')})</b>\n<code>{t.get('ca')}</code>\n\n"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="wishlist_menu")]])
    await update.callback_query.edit_message_caption(caption=text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _show_remove_wishlist(update, context):
    query   = update.callback_query
    user_id = str(query.from_user.id)
    wl = get_wishlist(user_id)
    if not wl:
        await query.answer("Wishlist is empty.", show_alert=True)
        return
    rows = [
        [InlineKeyboardButton(f"❌ {t.get('name')} (${t.get('symbol')})", callback_data=f"wishlist_remove_{t['ca']}")]
        for t in wl
    ]
    rows.append([InlineKeyboardButton("« Cancel", callback_data="wishlist_menu")])
    await query.edit_message_caption(
        caption="Select a token to remove:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _top_calls(update, context, user_id):
    await update.callback_query.edit_message_caption("🏆 Fetching live data…", parse_mode=ParseMode.HTML)
    alerts = get_recent_alerts(20)
    if not alerts:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="start_menu")]])
        await update.callback_query.edit_message_caption("No calls recorded yet.", reply_markup=kb)
        return

    import httpx
    results = []
    async with httpx.AsyncClient(timeout=15) as client:
        for alert in alerts:
            try:
                resp = await client.get(f"https://api.dexscreener.com/latest/dex/pairs/{alert.get('chain','solana')}/{alert.get('pairAddress','')}")
                if resp.status_code == 200:
                    pairs = resp.json().get("pairs", [])
                    if pairs:
                        current = float(pairs[0].get("priceUsd", 0))
                        entry   = float(alert.get("priceUsd", 0))
                        if entry > 0:
                            alert["gain"] = ((current - entry) / entry) * 100
                            alert["current_price"] = current
                            results.append(alert)
            except Exception:
                pass
            await asyncio.sleep(0.3)

    results.sort(key=lambda x: x.get("gain", -999), reverse=True)
    top5 = results[:5]

    await update.callback_query.message.delete()
    await update.callback_query.message.chat.send_message("🏆 <b>Top 5 All-Time Calls</b>", parse_mode=ParseMode.HTML)

    for i, p in enumerate(top5):
        gain = p.get("gain", 0)
        caption = (
            f"<b>#{i+1}: {p.get('name')} (${p.get('symbol')})</b>\n\n"
            f"<b>Gain Since Call:</b> +{gain:,.0f}%\n"
            f"<b>Called Price:</b> ${p.get('priceUsd', 0):.6f}\n"
            f"<b>Current Price:</b> ${p.get('current_price', 0):.6f}"
        )
        dex_url = f"https://dexscreener.com/{p.get('chain','solana')}/{p.get('pairAddress','')}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📈 View Chart", url=dex_url)]])
        try:
            await update.callback_query.message.chat.send_photo(
                photo=p.get("imageUrl") or "https://static.thenounproject.com/png/4967329-200.png",
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        except Exception:
            await update.callback_query.message.chat.send_message(caption, parse_mode=ParseMode.HTML, reply_markup=kb)
        await asyncio.sleep(0.5)

    await update.callback_query.message.chat.send_message(
        "Back to menu.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Main Menu", callback_data="start_menu")]]),
    )


async def _test_showcase(update, context, user_id):
    await update.callback_query.edit_message_caption("Loading preview alerts…")
    alerts = get_recent_alerts(5)
    if not alerts:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="start_menu")]])
        await update.callback_query.edit_message_caption("No alerts yet.", reply_markup=kb)
        return
    await update.callback_query.message.delete()
    for alert in reversed(alerts):
        wl = {t["ca"] for t in get_wishlist(user_id)}
        ca = alert.get("ca", "")
        rows = []
        if ca:
            app_url = f"{MINI_APP_URL}?startapp={ca}" if MINI_APP_URL else f"https://t.me/MoonshotAlphaBot/app?startapp={ca}"
            rows.append([InlineKeyboardButton("🚀 Terminal", web_app=WebAppInfo(url=app_url))])
            rows.append([
                InlineKeyboardButton("➖ Remove from Wishlist" if ca in wl else "⭐ Add to Wishlist",
                                     callback_data=f"wishlist_{'remove' if ca in wl else 'add'}_{ca}")
            ])
        await update.callback_query.message.chat.send_message(
            alert.get("message", ""), reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML
        )
        await asyncio.sleep(0.3)


async def _alert_categories(update, user_id):
    from database.firebase import db
    is_muted = False
    if db:
        doc = db.collection("subscribers").document(user_id).get()
        if doc.exists:
            is_muted = doc.to_dict().get("alerts_muted", False)
    mute_text = "🔴 Alerts: OFF" if is_muted else "🟢 Alerts: ON"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("High-Risk 🔥",   callback_data="confirm_risk_high")],
        [InlineKeyboardButton("Medium-Risk 📈", callback_data="confirm_risk_medium")],
        [InlineKeyboardButton("Low-Risk 🛡️",   callback_data="confirm_risk_low")],
        [InlineKeyboardButton(mute_text,         callback_data="toggle_mute_alerts")],
        [InlineKeyboardButton("« Back",          callback_data="start_menu")],
    ])
    await update.callback_query.edit_message_caption(
        caption="🚀 <b>Alert Categories</b>",
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )


async def _risk_warning(update, risk: str):
    warnings = {
        "high":   "⚠️ <b>HIGH-RISK</b> — Extremely volatile. High chance of loss.",
        "medium": "📈 <b>MEDIUM-RISK</b> — Momentum-based. Do your own research.",
        "low":    "🛡️ <b>LOW-RISK</b> — More stable, but all crypto carries risk.",
    }
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚠️ I Understand, Show Alerts", callback_data=f"view_alerts_{risk}_0")],
        [InlineKeyboardButton("« Back",                        callback_data="alert_categories")],
    ])
    await update.callback_query.edit_message_caption(
        caption=warnings.get(risk, "Trade responsibly."), reply_markup=kb, parse_mode=ParseMode.HTML
    )


async def _view_alerts(update, context, user_id, risk, page):
    await update.callback_query.message.delete()
    wl_cas  = {t["ca"] for t in get_wishlist(user_id)}
    alerts, total = get_alerts_by_risk(risk, page, PAGE_SIZE)
    if not alerts:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"No alerts for <b>{risk}-risk</b> yet.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="alert_categories")]]),
            parse_mode=ParseMode.HTML,
        )
        return

    for alert in reversed(alerts):
        ca   = alert.get("ca", "")
        rows = []
        if ca:
            app_url = f"{MINI_APP_URL}?startapp={ca}" if MINI_APP_URL else ""
            if app_url:
                rows.append([InlineKeyboardButton("🚀 Terminal", web_app=WebAppInfo(url=app_url))])
            rows.append([
                InlineKeyboardButton("➖ Remove" if ca in wl_cas else "⭐ Add to Wishlist",
                                     callback_data=f"wishlist_{'remove' if ca in wl_cas else 'add'}_{ca}")
            ])
        await context.bot.send_message(
            chat_id=user_id,
            text=alert.get("message", ""),
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode=ParseMode.HTML,
        )
        await asyncio.sleep(0.3)

    pagination = []
    if page > 0:
        pagination.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"view_alerts_{risk}_{page-1}"))
    if (page + 1) * PAGE_SIZE < total:
        pagination.append(InlineKeyboardButton("Next ➡️", callback_data=f"view_alerts_{risk}_{page+1}"))

    rows = [pagination] if pagination else []
    rows.append([InlineKeyboardButton("« Categories", callback_data="alert_categories")])
    await context.bot.send_message(chat_id=user_id, text="Browse more.", reply_markup=InlineKeyboardMarkup(rows))


async def _toggle_mute(update, user_id):
    from database.firebase import db
    if not db:
        return
    ref = db.collection("subscribers").document(user_id)
    doc = ref.get()
    if doc.exists:
        new_muted = not doc.to_dict().get("alerts_muted", False)
        ref.update({"alerts_muted": new_muted})
        await update.callback_query.answer("🔕 Muted" if new_muted else "🔔 Unmuted", show_alert=False)
        await _alert_categories(update, user_id)


async def _how_it_works(update):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❓ FAQ",             callback_data="faq")],
        [InlineKeyboardButton("📚 Glossary",        callback_data="glossary")],
        [InlineKeyboardButton("« Back",             callback_data="start_menu")],
    ])
    await update.callback_query.edit_message_caption(caption="Select a topic:", reply_markup=kb)


async def _info_page(update, page: str):
    content = {
        "faq": (
            "<b>FAQ</b>\n\n"
            "<b>Q: How often are alerts sent?</b>\nA: Whenever our system detects a high-potential token.\n\n"
            "<b>Q: What chains do you cover?</b>\nA: Solana, Ethereum, BSC, Base.\n\n"
            "<b>Q: Is this financial advice?</b>\nA: No. Always DYOR."
        ),
        "glossary": (
            "<b>Glossary</b>\n\n"
            "<b>Market Cap:</b> Total token value.\n\n"
            "<b>Liquidity:</b> Capital in the trading pool.\n\n"
            "<b>Rug Pull:</b> Devs drain liquidity, token → $0.\n\n"
            "<b>MEV:</b> Miner extractable value — front-running protection."
        ),
        "support": (
            "<b>Support</b>\n\n"
            "📧 moonshotalphabot@gmail.com\n\n"
            "For urgent issues, email with your Telegram ID."
        ),
    }
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="how_it_works")]])
    await update.callback_query.edit_message_caption(
        caption=content.get(page, ""), reply_markup=kb, parse_mode=ParseMode.HTML
    )


# ---------------------------------------------------------------------------
# Text message handler
# ---------------------------------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    state   = context.user_data.get("state")
    text    = update.message.text.strip()

    if state == "AWAITING_WISHLIST_ADD":
        is_sol = 32 <= len(text) <= 44 and text.isalnum() and not text.startswith("0x")
        is_evm = text.startswith("0x") and len(text) == 42
        if is_sol or is_evm:
            msg = await update.message.reply_text("🔍 Verifying token…")
            token = await _scanner.get_pair_data(text)
            if token:
                ok, reply = await add_to_wishlist(
                    user_id, text,
                    {"chain": token.chain, "name": token.name, "symbol": token.symbol},
                )
            else:
                ok, reply = False, "❌ Token not found on any supported chain."
            await msg.edit_text(reply, parse_mode=ParseMode.HTML)
            if ok:
                context.user_data.pop("state", None)
        else:
            await update.message.reply_text(
                "❌ Invalid address. Send a valid Solana (32-44 chars) or EVM (0x…) contract address.",
                parse_mode=ParseMode.HTML,
            )
        return

    await update.message.reply_text("Use the menu buttons or /start to navigate.")


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

async def _send_or_edit(update, caption, reply_markup, photo=None):
    if update.callback_query:
        try:
            if photo:
                media = InputMediaPhoto(media=open(photo, "rb"), caption=caption, parse_mode=ParseMode.HTML)
                await update.callback_query.message.edit_media(media=media, reply_markup=reply_markup)
            else:
                await update.callback_query.edit_message_caption(
                    caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML
                )
        except Exception:
            pass
    elif update.message:
        if photo:
            try:
                await update.message.reply_photo(
                    photo=open(photo, "rb"), caption=caption,
                    parse_mode=ParseMode.HTML, reply_markup=reply_markup,
                )
                return
            except Exception:
                pass
        await update.message.reply_text(caption, parse_mode=ParseMode.HTML, reply_markup=reply_markup)