"""
PivotAlert - Multi-user Crypto Signal Bot
Phase 2: Razorpay Payment Integration
- Payment links generated via /upgrade command
- Webhook server to receive payment confirmations
- Auto-upgrade users after successful payment
"""

import os
import asyncio
import logging
import time
import json
import hmac
import hashlib
import requests
import websockets
import asyncpg
import telegram
from aiohttp import web
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update
from dotenv import load_dotenv

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
load_dotenv()

TOKEN               = os.getenv("TELEGRAM_TOKEN")
CHAT_ID             = os.getenv("TELEGRAM_CHAT_ID")
ADMIN_CHAT_ID       = os.getenv("ADMIN_CHAT_ID")
DATABASE_URL        = os.getenv("DATABASE_URL")
RAZORPAY_KEY_ID     = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
WEBHOOK_URL         = os.getenv("WEBHOOK_URL")
PORT                = int(os.getenv("PORT", 8080))

if not TOKEN or not CHAT_ID:
    raise ValueError("❌ TELEGRAM_TOKEN or TELEGRAM_CHAT_ID missing!")

DEFAULT_SYMBOLS = ["btcusdt", "ethusdt", "solusdt"]

RSI_PERIOD      = 14
KLINE_INTERVAL  = "1h"
KLINE_LIMIT     = 50
PIVOT_TOLERANCE = 0.003

TIER_SETTINGS = {
    "free": {
        "rsi_oversold":   35,
        "rsi_overbought": 65,
        "cooldown":       7200,
        "coins":          ["btcusdt", "ethusdt", "solusdt"],
        "price":          0,
    },
    "pro": {
        "rsi_oversold":   38,
        "rsi_overbought": 62,
        "cooldown":       3600,
        "coins":          None,
        "price":          29900,
    },
}

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("pivotalert.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Global state
# ─────────────────────────────────────────────
coin_state: dict[str, dict] = {}
active_streams: dict[str, asyncio.Task] = {}
db_pool = None

# ─────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────
async def init_db() -> None:
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id    BIGINT PRIMARY KEY,
                username   TEXT,
                plan       TEXT DEFAULT 'free',
                subscribed BOOLEAN DEFAULT TRUE,
                joined_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS signal_history (
                id          SERIAL PRIMARY KEY,
                symbol      TEXT,
                signal_type TEXT,
                price       FLOAT,
                support     FLOAT,
                resistance  FLOAT,
                rsi         FLOAT,
                fired_at    TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tracked_coins (
                symbol   TEXT PRIMARY KEY,
                added_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id              SERIAL PRIMARY KEY,
                chat_id         BIGINT,
                payment_link_id TEXT UNIQUE,
                payment_id      TEXT,
                amount          INTEGER,
                status          TEXT DEFAULT 'pending',
                created_at      TIMESTAMP DEFAULT NOW(),
                paid_at         TIMESTAMP
            )
        """)
        for symbol in DEFAULT_SYMBOLS:
            await conn.execute("""
                INSERT INTO tracked_coins (symbol) VALUES ($1) ON CONFLICT DO NOTHING
            """, symbol)
    logger.info("✅ Database initialized")


async def get_all_subscribers() -> list[dict]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT chat_id, plan FROM users WHERE subscribed = TRUE")
    return [dict(r) for r in rows]


async def get_tracked_coins() -> list[str]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT symbol FROM tracked_coins")
    return [r["symbol"] for r in rows]


async def add_tracked_coin(symbol: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO tracked_coins (symbol) VALUES ($1) ON CONFLICT DO NOTHING
        """, symbol)


async def remove_tracked_coin(symbol: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM tracked_coins WHERE symbol = $1", symbol)


async def log_signal(symbol, signal_type, price, support, resistance, rsi) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO signal_history (symbol, signal_type, price, support, resistance, rsi)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, symbol, signal_type, price, support, resistance, rsi or 0)


async def save_payment(chat_id: int, payment_link_id: str, amount: int) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO payments (chat_id, payment_link_id, amount)
            VALUES ($1, $2, $3) ON CONFLICT DO NOTHING
        """, chat_id, payment_link_id, amount)


async def mark_payment_paid(payment_link_id: str, payment_id: str) -> int | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE payments SET status = 'paid', payment_id = $1, paid_at = NOW()
            WHERE payment_link_id = $2 RETURNING chat_id
        """, payment_id, payment_link_id)
        if row:
            await conn.execute(
                "UPDATE users SET plan = 'pro' WHERE chat_id = $1", row["chat_id"]
            )
            return row["chat_id"]
    return None

# ─────────────────────────────────────────────
# Telegram helpers
# ─────────────────────────────────────────────
bot = telegram.Bot(token=TOKEN)


async def send_alert(message: str) -> None:
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Failed to send alert: {e}")


async def broadcast_signal(message: str, symbol: str) -> None:
    subscribers = await get_all_subscribers()
    sent = 0
    for user in subscribers:
        tier    = user["plan"]
        allowed = TIER_SETTINGS[tier]["coins"]
        if allowed and symbol not in allowed:
            continue
        try:
            await bot.send_message(chat_id=user["chat_id"], text=message, parse_mode="Markdown")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning(f"Could not send to {user['chat_id']}: {e}")
    logger.info(f"Signal broadcasted to {sent}/{len(subscribers)} users")

# ─────────────────────────────────────────────
# Razorpay helpers
# ─────────────────────────────────────────────
def create_payment_link(chat_id: int, username: str) -> dict | None:
    try:
        resp = requests.post(
            "https://api.razorpay.com/v1/payment_links",
            json={
                "amount":          TIER_SETTINGS["pro"]["price"],
                "currency":        "INR",
                "accept_partial":  False,
                "description":     "PivotAlert Pro - Monthly Subscription",
                "customer":        {"name": username or "PivotAlert User"},
                "notify":          {"sms": False, "email": False},
                "reminder_enable": False,
                "upi_link": True,
                "notes":           {"chat_id": str(chat_id), "plan": "pro"},
                "callback_url":    f"{WEBHOOK_URL}/payment/success",
                "callback_method": "get",
            },
            auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        logger.error(f"Razorpay error: {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"Razorpay error: {e}")
        return None


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)

# ─────────────────────────────────────────────
# Webhook server
# ─────────────────────────────────────────────
async def handle_razorpay_webhook(request: web.Request) -> web.Response:
    try:
        body      = await request.read()
        signature = request.headers.get("X-Razorpay-Signature", "")

        if not verify_webhook_signature(body, signature):
            logger.warning("Invalid webhook signature!")
            return web.Response(status=400, text="Invalid signature")

        data  = json.loads(body)
        event = data.get("event")
        logger.info(f"Razorpay webhook: {event}")

        if event == "payment_link.paid":
            payload         = data["payload"]["payment_link"]["entity"]
            payment_link_id = payload["id"]
            payment_id      = data["payload"]["payment"]["entity"]["id"]
            chat_id         = await mark_payment_paid(payment_link_id, payment_id)

            if chat_id:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "🎉 *Payment Successful!*\n\n"
                        "Welcome to *PivotAlert Pro!* ⭐\n\n"
                        "You now have access to:\n"
                        "• All tracked coins\n"
                        "• 1 hour cooldown\n"
                        "• Priority signals\n\n"
                        "Signals will start arriving shortly! 📈"
                    ),
                    parse_mode="Markdown"
                )
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"💰 *New Pro subscriber!*\n"
                        f"Chat ID: `{chat_id}`\n"
                        f"Payment: `{payment_id}`\n"
                        f"Amount: ₹299"
                    ),
                    parse_mode="Markdown"
                )

        return web.Response(status=200, text="OK")
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(status=500, text="Error")


async def handle_payment_success(request: web.Request) -> web.Response:
    return web.Response(
        content_type="text/html",
        text="""
        <html>
        <body style="font-family:sans-serif;text-align:center;padding:50px;background:#f0f4ff">
            <h1>🎉 Payment Successful!</h1>
            <p>Your PivotAlert Pro subscription is now active.</p>
            <p>Go back to Telegram to start receiving signals!</p>
        </body>
        </html>
        """
    )


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="PivotAlert is running! 🚀")


async def start_webhook_server() -> None:
    app = web.Application()
    app.router.add_post("/webhook/razorpay", handle_razorpay_webhook)
    app.router.add_get("/payment/success",   handle_payment_success)
    app.router.add_get("/",                  handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info(f"✅ Webhook server running on port {PORT}")

# ─────────────────────────────────────────────
# Binance helpers
# ─────────────────────────────────────────────
def validate_symbol(symbol: str) -> bool:
    try:
        resp = requests.get(
            f"https://api.binance.com/api/v3/ticker/price?symbol={symbol.upper()}", timeout=5
        )
        return resp.status_code == 200
    except Exception:
        return False


def fetch_klines(symbol: str) -> list[dict]:
    try:
        resp = requests.get(
            f"https://api.binance.com/api/v3/klines"
            f"?symbol={symbol.upper()}&interval={KLINE_INTERVAL}&limit={KLINE_LIMIT}",
            timeout=10,
        )
        resp.raise_for_status()
        return [
            {"open": float(c[1]), "high": float(c[2]),
             "low":  float(c[3]), "close": float(c[4])}
            for c in resp.json()
        ]
    except Exception as e:
        logger.error(f"klines fetch failed for {symbol}: {e}")
        return []

# ─────────────────────────────────────────────
# Indicators
# ─────────────────────────────────────────────
def calculate_rsi(closes: list[float], period: int = RSI_PERIOD) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        delta = closes[-period - 1 + i] - closes[-period - 2 + i]
        (gains if delta > 0 else losses).append(abs(delta))
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 1e-10
    return 100 - (100 / (1 + avg_gain / avg_loss))


def calculate_pivot_levels(candles: list[dict]) -> tuple:
    if not candles:
        return None, None
    last       = candles[-2] if len(candles) >= 2 else candles[-1]
    pivot      = (last["high"] + last["low"] + last["close"]) / 3
    return round(2 * pivot - last["high"], 4), round(2 * pivot - last["low"], 4)

# ─────────────────────────────────────────────
# Coin state helpers
# ─────────────────────────────────────────────
def add_coin_state(symbol: str) -> None:
    if symbol not in coin_state:
        coin_state[symbol] = {
            "price": None, "last_signal": None,
            "last_alert_time": 0, "support": None,
            "resistance": None, "rsi": None,
        }


def remove_coin_state(symbol: str) -> None:
    coin_state.pop(symbol, None)
    if symbol in active_streams:
        active_streams[symbol].cancel()
        del active_streams[symbol]

# ─────────────────────────────────────────────
# User commands
# ─────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id  = update.effective_chat.id
    username = update.effective_chat.username or "unknown"
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (chat_id, username, subscribed)
            VALUES ($1, $2, TRUE)
            ON CONFLICT (chat_id) DO UPDATE SET subscribed = TRUE
        """, chat_id, username)
    await update.message.reply_text(
        "👋 Welcome to *PivotAlert!*\n\n"
        "You are now subscribed to crypto trading signals.\n\n"
        "📊 *Free plan includes:*\n"
        "• BTC, ETH, SOL signals\n"
        "• Pivot point support & resistance\n"
        "• RSI confirmation filter\n"
        "• Alerts every 2 hours max\n\n"
        "⭐ Use /upgrade to go Pro — ₹299/month\n"
        "Use /help to see all commands",
        parse_mode="Markdown"
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET subscribed = FALSE WHERE chat_id = $1",
            update.effective_chat.id
        )
    await update.message.reply_text("😢 Unsubscribed. Send /start to resubscribe!")


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📋 *PivotAlert Plans:*\n\n"
        "🆓 *Free — ₹0/month*\n"
        "• BTC, ETH, SOL signals only\n"
        "• 2 hour cooldown\n"
        "• RSI filter (35/65)\n\n"
        "⭐ *Pro — ₹299/month*\n"
        "• All tracked coins\n"
        "• 1 hour cooldown\n"
        "• Tighter RSI filter (38/62)\n\n"
        "💎 *Elite — ₹799/month*\n"
        "• Everything in Pro\n"
        "• Forex + Stocks (coming soon)\n"
        "• 30 min cooldown\n\n"
        "Use /upgrade to subscribe to Pro!",
        parse_mode="Markdown"
    )


async def cmd_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id  = update.effective_chat.id
    username = update.effective_chat.username or "User"

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT plan FROM users WHERE chat_id = $1", chat_id)

    if row and row["plan"] == "pro":
        await update.message.reply_text(
            "⭐ You are already on *Pro plan!*\nEnjoy your signals! 📈",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text("⏳ Generating your payment link...")
    link = create_payment_link(chat_id, username)

    if not link:
        await update.message.reply_text("❌ Could not generate payment link. Try again later.")
        return

    await save_payment(chat_id, link["id"], TIER_SETTINGS["pro"]["price"])
    await update.message.reply_text(
        f"⭐ *Upgrade to PivotAlert Pro*\n\n"
        f"₹299/month\n\n"
        f"✅ All tracked coins\n"
        f"✅ 1 hour cooldown\n"
        f"✅ Priority signals\n\n"
        f"👇 Pay securely:\n{link['short_url']}\n\n"
        f"_Powered by Razorpay_ 🔒",
        parse_mode="Markdown"
    )


async def cmd_myplan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT plan, joined_at FROM users WHERE chat_id = $1", chat_id
        )
    if not row:
        await update.message.reply_text("Send /start to subscribe!")
        return
    plan   = row["plan"]
    emoji  = "⭐" if plan == "pro" else "🆓"
    joined = row["joined_at"].strftime("%d %b %Y")
    await update.message.reply_text(
        f"👤 *Your Account*\n\n"
        f"Plan:   {emoji} {plan.upper()}\n"
        f"Joined: {joined}\n\n"
        f"{'Use /upgrade to go Pro! 🚀' if plan == 'free' else 'Enjoying Pro! 📈'}",
        parse_mode="Markdown"
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    coins = await get_tracked_coins()
    if not coins:
        await update.message.reply_text("No coins tracked yet.")
        return
    lines = ["📋 *Tracked Coins:*\n"]
    for symbol in coins:
        price = coin_state.get(symbol, {}).get("price")
        lines.append(f"• {symbol.upper()} — {'$' + f'{price:,.4f}' if price else 'loading...'}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    coins = await get_tracked_coins()
    if not coins:
        await update.message.reply_text("No coins being tracked.")
        return
    await update.message.reply_text("⏳ Fetching latest data...")
    lines = ["📊 *Current Status:*\n"]
    for symbol in coins:
        s          = coin_state.get(symbol, {})
        price      = s.get("price")
        support    = s.get("support")
        resistance = s.get("resistance")
        rsi        = s.get("rsi")
        signal     = s.get("last_signal") or "None"
        lines += [
            f"*{symbol.upper()}*",
            f"  Price:      {'$' + f'{price:,.4f}' if price else 'loading...'}",
            f"  Support:    {'$' + f'{support:,.4f}' if support else 'n/a'}",
            f"  Resistance: {'$' + f'{resistance:,.4f}' if resistance else 'n/a'}",
            f"  RSI:        {f'{rsi:.1f}' if rsi else 'n/a'}",
            f"  Signal:     {signal}\n",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 *PivotAlert Commands:*\n\n"
        "/start    — Subscribe to signals\n"
        "/stop     — Unsubscribe\n"
        "/plan     — See all plans\n"
        "/myplan   — See your current plan\n"
        "/upgrade  — Upgrade to Pro ⭐\n"
        "/list     — Show tracked coins\n"
        "/status   — Full indicator data\n"
        "/help     — Show this message",
        parse_mode="Markdown"
    )

# ─────────────────────────────────────────────
# Admin commands
# ─────────────────────────────────────────────
def is_admin(update: Update) -> bool:
    return str(update.effective_chat.id) == str(ADMIN_CHAT_ID)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    async with db_pool.acquire() as conn:
        total    = await conn.fetchval("SELECT COUNT(*) FROM users")
        subbed   = await conn.fetchval("SELECT COUNT(*) FROM users WHERE subscribed = TRUE")
        free_u   = await conn.fetchval("SELECT COUNT(*) FROM users WHERE plan='free' AND subscribed=TRUE")
        pro_u    = await conn.fetchval("SELECT COUNT(*) FROM users WHERE plan='pro' AND subscribed=TRUE")
        signals  = await conn.fetchval("SELECT COUNT(*) FROM signal_history")
        coins    = await conn.fetchval("SELECT COUNT(*) FROM tracked_coins")
        payments = await conn.fetchval("SELECT COUNT(*) FROM payments WHERE status='paid'")
    await update.message.reply_text(
        f"📊 *PivotAlert Stats:*\n\n"
        f"👥 Total users:   {total}\n"
        f"✅ Subscribed:    {subbed}\n"
        f"🆓 Free:          {free_u}\n"
        f"⭐ Pro:           {pro_u}\n"
        f"📈 Signals fired: {signals}\n"
        f"🪙 Coins tracked: {coins}\n"
        f"💳 Payments:      {payments}\n\n"
        f"💰 Est. revenue:  ₹{pro_u * 299:,}/mo",
        parse_mode="Markdown"
    )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast Your message here")
        return
    users      = await get_all_subscribers()
    sent, fail = 0, 0
    for user in users:
        try:
            await bot.send_message(
                chat_id=user["chat_id"],
                text=f"📢 *Announcement:*\n\n{' '.join(context.args)}",
                parse_mode="Markdown"
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    await update.message.reply_text(f"✅ Sent: {sent} | Failed: {fail}")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /add BNBUSDT")
        return
    symbol = context.args[0].lower()
    if symbol in await get_tracked_coins():
        await update.message.reply_text(f"⚠️ {symbol.upper()} already tracked.")
        return
    await update.message.reply_text(f"🔍 Validating {symbol.upper()}...")
    if not validate_symbol(symbol):
        await update.message.reply_text(f"❌ {symbol.upper()} not found on Binance.")
        return
    await add_tracked_coin(symbol)
    add_coin_state(symbol)
    active_streams[symbol] = asyncio.create_task(price_stream(symbol))
    await update.message.reply_text(f"✅ Now tracking {symbol.upper()}")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /remove BNBUSDT")
        return
    symbol = context.args[0].lower()
    if symbol not in await get_tracked_coins():
        await update.message.reply_text(f"⚠️ {symbol.upper()} not tracked.")
        return
    await remove_tracked_coin(symbol)
    remove_coin_state(symbol)
    await update.message.reply_text(f"🗑️ Removed {symbol.upper()}")


async def cmd_manualupgrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /manualupgrade CHAT_ID pro")
        return
    target_id = int(context.args[0])
    plan      = context.args[1].lower()
    if plan not in ("free", "pro"):
        await update.message.reply_text("Plan must be 'free' or 'pro'")
        return
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET plan = $1 WHERE chat_id = $2", plan, target_id)
    await update.message.reply_text(f"✅ User {target_id} → {plan}")
    try:
        await bot.send_message(
            chat_id=target_id,
            text=f"🎉 Your plan has been upgraded to *{plan.upper()}*! 📈",
            parse_mode="Markdown"
        )
    except Exception:
        pass

# ─────────────────────────────────────────────
# WebSocket price stream
# ─────────────────────────────────────────────
async def price_stream(symbol: str) -> None:
    url, reconnect_delay = f"wss://stream.binance.com:443/ws/{symbol}@aggTrade", 5
    while symbol in coin_state:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                logger.info(f"WebSocket connected: {symbol}")
                reconnect_delay = 5
                async for raw in ws:
                    if symbol not in coin_state:
                        return
                    coin_state[symbol]["price"] = float(json.loads(raw)["p"])
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"WebSocket error for {symbol}: {e}")
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60)

# ─────────────────────────────────────────────
# Signal evaluation
# ─────────────────────────────────────────────
async def evaluate_signal(symbol: str) -> None:
    state = coin_state.get(symbol)
    if not state or state["price"] is None:
        return

    candles = fetch_klines(symbol)
    if not candles:
        return

    support, resistance = calculate_pivot_levels(candles)
    closes              = [c["close"] for c in candles]
    rsi                 = calculate_rsi(closes)

    if support is None:
        return

    state.update({"support": support, "resistance": resistance, "rsi": rsi})
    price   = state["price"]
    rsi_str = f"{rsi:.1f}" if rsi else "n/a"
    logger.info(f"{symbol.upper():10s} | price={price:.4f} | S={support:.4f} | R={resistance:.4f} | RSI={rsi_str}")

    now         = time.time()
    on_cooldown = (now - state["last_alert_time"]) < TIER_SETTINGS["free"]["cooldown"]

    if price >= resistance * (1 - PIVOT_TOLERANCE) and \
       (rsi is None or rsi >= TIER_SETTINGS["free"]["rsi_overbought"]) and \
       state["last_signal"] != "SELL" and not on_cooldown:
        msg = (f"🚨 *{symbol.upper()} SELL SIGNAL*\n"
               f"Price:      ${price:,.4f}\nResistance: ${resistance:,.4f}\nRSI: {rsi_str}\n\n"
               f"_Powered by PivotAlert_ 📊")
        await broadcast_signal(msg, symbol)
        await log_signal(symbol, "SELL", price, support, resistance, rsi)
        state["last_signal"], state["last_alert_time"] = "SELL", now
        return

    if price <= support * (1 + PIVOT_TOLERANCE) and \
       (rsi is None or rsi <= TIER_SETTINGS["free"]["rsi_oversold"]) and \
       state["last_signal"] != "BUY" and not on_cooldown:
        msg = (f"📈 *{symbol.upper()} BUY SIGNAL*\n"
               f"Price:   ${price:,.4f}\nSupport: ${support:,.4f}\nRSI: {rsi_str}\n\n"
               f"_Powered by PivotAlert_ 📊")
        await broadcast_signal(msg, symbol)
        await log_signal(symbol, "BUY", price, support, resistance, rsi)
        state["last_signal"], state["last_alert_time"] = "BUY", now
        return

    if support < price < resistance:
        state["last_signal"] = None


async def signal_loop(interval_seconds: int = 60) -> None:
    await asyncio.sleep(5)
    while True:
        coins = await get_tracked_coins()
        await asyncio.gather(*[evaluate_signal(s) for s in coins])
        await asyncio.sleep(interval_seconds)

# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
async def main() -> None:
    await init_db()
    coins = await get_tracked_coins()
    for symbol in coins:
        add_coin_state(symbol)

    await start_webhook_server()

    app = Application.builder().token(TOKEN).build()
    for cmd, handler in [
        ("start",         cmd_start),
        ("stop",          cmd_stop),
        ("plan",          cmd_plan),
        ("myplan",        cmd_myplan),
        ("upgrade",       cmd_upgrade),
        ("list",          cmd_list),
        ("status",        cmd_status),
        ("help",          cmd_help),
        ("stats",         cmd_stats),
        ("broadcast",     cmd_broadcast),
        ("add",           cmd_add),
        ("remove",        cmd_remove),
        ("manualupgrade", cmd_manualupgrade),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    await send_alert(
        "🚀 *PivotAlert Phase 2 is live!*\n"
        "Razorpay payments enabled 💳\n"
        "Send /stats to see subscribers."
    )

    for symbol in coins:
        active_streams[symbol] = asyncio.create_task(price_stream(symbol))

    await signal_loop()

    await app.updater.stop()
    await app.stop()
    await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
