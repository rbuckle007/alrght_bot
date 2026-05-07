"""
PivotAlert - Multi-user Crypto Signal Bot
Phase 1: Multi-user + PostgreSQL database
- /start     - subscribe to signals
- /stop      - unsubscribe
- /plan      - see free vs pro features
- /list      - show tracked coins
- /status    - show current indicators
- /add       - add coin (admin only)
- /remove    - remove coin (admin only)
- /stats     - admin: see subscriber count
- /broadcast - admin: send message to all users
"""

import os
import asyncio
import logging
import time
import json
import requests
import websockets
import asyncpg
import telegram
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update
from dotenv import load_dotenv

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
load_dotenv()

TOKEN          = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID")
ADMIN_CHAT_ID  = os.getenv("ADMIN_CHAT_ID")
DATABASE_URL   = os.getenv("DATABASE_URL")

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
    },
    "pro": {
        "rsi_oversold":   38,
        "rsi_overbought": 62,
        "cooldown":       3600,
        "coins":          None,
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
                chat_id     BIGINT PRIMARY KEY,
                username    TEXT,
                plan        TEXT DEFAULT 'free',
                subscribed  BOOLEAN DEFAULT TRUE,
                joined_at   TIMESTAMP DEFAULT NOW()
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
                symbol      TEXT PRIMARY KEY,
                added_at    TIMESTAMP DEFAULT NOW()
            )
        """)
        for symbol in DEFAULT_SYMBOLS:
            await conn.execute("""
                INSERT INTO tracked_coins (symbol)
                VALUES ($1) ON CONFLICT DO NOTHING
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

# ─────────────────────────────────────────────
# Telegram helpers
# ─────────────────────────────────────────────
bot = telegram.Bot(token=TOKEN)


async def send_alert(message: str) -> None:
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="Markdown")
        logger.info(f"Alert sent: {message[:60]}…")
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
# Binance helpers
# ─────────────────────────────────────────────
def validate_symbol(symbol: str) -> bool:
    try:
        url  = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol.upper()}"
        resp = requests.get(url, timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def fetch_klines(symbol: str) -> list[dict]:
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={symbol.upper()}&interval={KLINE_INTERVAL}&limit={KLINE_LIMIT}"
    )
    try:
        resp = requests.get(url, timeout=10)
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
    support    = round(2 * pivot - last["high"], 4)
    resistance = round(2 * pivot - last["low"],  4)
    return support, resistance

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
        "Use /plan to see all plans\n"
        "Use /help to see all commands",
        parse_mode="Markdown"
    )
    logger.info(f"New subscriber: {username} ({chat_id})")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET subscribed = FALSE WHERE chat_id = $1", chat_id)
    await update.message.reply_text(
        "😢 You have unsubscribed from PivotAlert.\n"
        "Send /start anytime to resubscribe!"
    )


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📋 *PivotAlert Plans:*\n\n"
        "🆓 *Free — ₹0/month*\n"
        "• BTC, ETH, SOL signals only\n"
        "• 2 hour cooldown between alerts\n"
        "• RSI filter (35/65)\n\n"
        "⭐ *Pro — ₹299/month*\n"
        "• All tracked coins\n"
        "• 1 hour cooldown\n"
        "• Tighter RSI filter (38/62)\n\n"
        "💎 *Elite — ₹799/month*\n"
        "• Everything in Pro\n"
        "• Forex + Stocks signals (coming soon)\n"
        "• 30 min cooldown\n\n"
        "To upgrade contact @YourUsername",
        parse_mode="Markdown"
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    coins = await get_tracked_coins()
    if not coins:
        await update.message.reply_text("No coins tracked yet.")
        return
    lines = ["📋 *Tracked Coins:*\n"]
    for symbol in coins:
        price     = coin_state.get(symbol, {}).get("price")
        price_str = f"${price:,.4f}" if price else "loading..."
        lines.append(f"• {symbol.upper()} — {price_str}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    coins = await get_tracked_coins()
    if not coins:
        await update.message.reply_text("No coins being tracked.")
        return
    await update.message.reply_text("⏳ Fetching latest data...")
    lines = ["📊 *Current Status:*\n"]
    for symbol in coins:
        state      = coin_state.get(symbol, {})
        price      = state.get("price")
        support    = state.get("support")
        resistance = state.get("resistance")
        rsi        = state.get("rsi")
        signal     = state.get("last_signal") or "None"
        lines.append(f"*{symbol.upper()}*")
        lines.append(f"  Price:      {f'${price:,.4f}' if price else 'loading...'}")
        lines.append(f"  Support:    {f'${support:,.4f}' if support else 'n/a'}")
        lines.append(f"  Resistance: {f'${resistance:,.4f}' if resistance else 'n/a'}")
        lines.append(f"  RSI:        {f'{rsi:.1f}' if rsi else 'n/a'}")
        lines.append(f"  Signal:     {signal}\n")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 *PivotAlert Commands:*\n\n"
        "/start   — Subscribe to signals\n"
        "/stop    — Unsubscribe\n"
        "/plan    — See Free vs Pro plans\n"
        "/list    — Show tracked coins & prices\n"
        "/status  — Full indicator data\n"
        "/help    — Show this message",
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
        total      = await conn.fetchval("SELECT COUNT(*) FROM users")
        subscribed = await conn.fetchval("SELECT COUNT(*) FROM users WHERE subscribed = TRUE")
        free_users = await conn.fetchval("SELECT COUNT(*) FROM users WHERE plan = 'free' AND subscribed = TRUE")
        pro_users  = await conn.fetchval("SELECT COUNT(*) FROM users WHERE plan = 'pro' AND subscribed = TRUE")
        signals    = await conn.fetchval("SELECT COUNT(*) FROM signal_history")
        coins      = await conn.fetchval("SELECT COUNT(*) FROM tracked_coins")
    await update.message.reply_text(
        f"📊 *PivotAlert Stats:*\n\n"
        f"👥 Total users:   {total}\n"
        f"✅ Subscribed:    {subscribed}\n"
        f"🆓 Free users:    {free_users}\n"
        f"⭐ Pro users:     {pro_users}\n"
        f"📈 Signals fired: {signals}\n"
        f"🪙 Tracked coins: {coins}\n\n"
        f"💰 Est. revenue:  ₹{pro_users * 299:,}/mo",
        parse_mode="Markdown"
    )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast Your message here")
        return
    message    = " ".join(context.args)
    users      = await get_all_subscribers()
    sent, fail = 0, 0
    for user in users:
        try:
            await bot.send_message(
                chat_id=user["chat_id"],
                text=f"📢 *Announcement:*\n\n{message}",
                parse_mode="Markdown"
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    await update.message.reply_text(f"✅ Sent: {sent} | Failed: {fail}")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("❌ Only admins can add coins.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /add BNBUSDT")
        return
    symbol = context.args[0].lower()
    coins  = await get_tracked_coins()
    if symbol in coins:
        await update.message.reply_text(f"⚠️ {symbol.upper()} already tracked.")
        return
    await update.message.reply_text(f"🔍 Validating {symbol.upper()}...")
    if not validate_symbol(symbol):
        await update.message.reply_text(f"❌ {symbol.upper()} not found on Binance.")
        return
    await add_tracked_coin(symbol)
    add_coin_state(symbol)
    task = asyncio.create_task(price_stream(symbol))
    active_streams[symbol] = task
    await update.message.reply_text(f"✅ Now tracking {symbol.upper()}")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("❌ Only admins can remove coins.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /remove BNBUSDT")
        return
    symbol = context.args[0].lower()
    coins  = await get_tracked_coins()
    if symbol not in coins:
        await update.message.reply_text(f"⚠️ {symbol.upper()} not being tracked.")
        return
    await remove_tracked_coin(symbol)
    remove_coin_state(symbol)
    await update.message.reply_text(f"🗑️ Removed {symbol.upper()}")


async def cmd_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually upgrade a user - admin only. Usage: /upgrade CHAT_ID pro"""
    if not is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /upgrade CHAT_ID pro")
        return
    target_id = int(context.args[0])
    plan      = context.args[1].lower()
    if plan not in ("free", "pro"):
        await update.message.reply_text("Plan must be 'free' or 'pro'")
        return
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET plan = $1 WHERE chat_id = $2", plan, target_id)
    await update.message.reply_text(f"✅ User {target_id} upgraded to {plan}")
    try:
        await bot.send_message(
            chat_id=target_id,
            text=f"🎉 Your PivotAlert plan has been upgraded to *{plan.upper()}*!\n\nEnjoy your new features.",
            parse_mode="Markdown"
        )
    except Exception:
        pass

# ─────────────────────────────────────────────
# WebSocket price stream
# ─────────────────────────────────────────────
async def price_stream(symbol: str) -> None:
    url             = f"wss://stream.binance.com:443/ws/{symbol}@aggTrade"
    reconnect_delay = 5
    while symbol in coin_state:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                logger.info(f"WebSocket connected: {symbol}")
                reconnect_delay = 5
                async for raw in ws:
                    if symbol not in coin_state:
                        return
                    msg   = json.loads(raw)
                    price = float(msg["p"])
                    coin_state[symbol]["price"] = price
        except asyncio.CancelledError:
            return
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"WebSocket closed for {symbol}: {e}")
        except Exception as e:
            logger.error(f"WebSocket error for {symbol}: {e}")
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60)

# ─────────────────────────────────────────────
# Signal evaluation
# ─────────────────────────────────────────────
async def evaluate_signal(symbol: str) -> None:
    state = coin_state.get(symbol)
    if not state:
        return
    price = state["price"]
    if price is None:
        return

    candles = fetch_klines(symbol)
    if not candles:
        return

    support, resistance = calculate_pivot_levels(candles)
    closes              = [c["close"] for c in candles]
    rsi                 = calculate_rsi(closes)

    if support is None or resistance is None:
        return

    state["support"]    = support
    state["resistance"] = resistance
    state["rsi"]        = rsi

    rsi_str = f"{rsi:.1f}" if rsi else "n/a"
    logger.info(
        f"{symbol.upper():10s} | price={price:.4f} | "
        f"S={support:.4f} | R={resistance:.4f} | RSI={rsi_str}"
    )

    now         = time.time()
    last_signal = state["last_signal"]
    on_cooldown = (now - state["last_alert_time"]) < TIER_SETTINGS["free"]["cooldown"]

    # SELL
    near_resistance = price >= resistance * (1 - PIVOT_TOLERANCE)
    rsi_overbought  = rsi is None or rsi >= TIER_SETTINGS["free"]["rsi_overbought"]
    if near_resistance and rsi_overbought and last_signal != "SELL" and not on_cooldown:
        message = (
            f"🚨 *{symbol.upper()} SELL SIGNAL*\n"
            f"Price:      ${price:,.4f}\n"
            f"Resistance: ${resistance:,.4f}\n"
            f"RSI:        {rsi_str}\n\n"
            f"_Powered by PivotAlert_ 📊"
        )
        await broadcast_signal(message, symbol)
        await log_signal(symbol, "SELL", price, support, resistance, rsi)
        state["last_signal"]     = "SELL"
        state["last_alert_time"] = now
        return

    # BUY
    near_support = price <= support * (1 + PIVOT_TOLERANCE)
    rsi_oversold = rsi is None or rsi <= TIER_SETTINGS["free"]["rsi_oversold"]
    if near_support and rsi_oversold and last_signal != "BUY" and not on_cooldown:
        message = (
            f"📈 *{symbol.upper()} BUY SIGNAL*\n"
            f"Price:   ${price:,.4f}\n"
            f"Support: ${support:,.4f}\n"
            f"RSI:     {rsi_str}\n\n"
            f"_Powered by PivotAlert_ 📊"
        )
        await broadcast_signal(message, symbol)
        await log_signal(symbol, "BUY", price, support, resistance, rsi)
        state["last_signal"]     = "BUY"
        state["last_alert_time"] = now
        return

    if support < price < resistance:
        state["last_signal"] = None


async def signal_loop(interval_seconds: int = 60) -> None:
    await asyncio.sleep(5)
    while True:
        coins = await get_tracked_coins()
        tasks = [evaluate_signal(symbol) for symbol in coins]
        await asyncio.gather(*tasks)
        await asyncio.sleep(interval_seconds)

# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
async def main() -> None:
    await init_db()

    coins = await get_tracked_coins()
    for symbol in coins:
        add_coin_state(symbol)

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("stop",      cmd_stop))
    app.add_handler(CommandHandler("plan",      cmd_plan))
    app.add_handler(CommandHandler("list",      cmd_list))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("add",       cmd_add))
    app.add_handler(CommandHandler("remove",    cmd_remove))
    app.add_handler(CommandHandler("upgrade",   cmd_upgrade))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    await send_alert(
        "🚀 *PivotAlert is live!*\n"
        "Multi-user mode active.\n"
        "Send /stats to see subscribers."
    )

    for symbol in coins:
        task = asyncio.create_task(price_stream(symbol))
        active_streams[symbol] = task

    await signal_loop()

    await app.updater.stop()
    await app.stop()
    await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
