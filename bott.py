"""
Crypto Trading Signal Bot
- Real-time Binance WebSocket price feeds
- Pivot point-based support/resistance
- RSI confirmation filter
- Per-coin signal cooldowns
- Telegram commands: /add /remove /list /status
- Structured logging
"""

import os
import asyncio
import logging
import time
import json
import requests
import websockets
import telegram
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update
from dotenv import load_dotenv

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
load_dotenv()

TOKEN   = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DEFAULT_SYMBOLS = ["btcusdt", "ethusdt", "solusdt"]

RSI_PERIOD      = 14
RSI_OVERSOLD    = 35
RSI_OVERBOUGHT  = 65
KLINE_INTERVAL  = "1h"
KLINE_LIMIT     = 50
SIGNAL_COOLDOWN = 3600
PIVOT_TOLERANCE = 0.003

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("crypto_bot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# State
# ─────────────────────────────────────────────
coin_state: dict[str, dict] = {}
active_streams: dict[str, asyncio.Task] = {}  # symbol -> running asyncio task

def add_coin(symbol: str) -> None:
    """Add a coin to tracking state."""
    symbol = symbol.lower()
    if symbol not in coin_state:
        coin_state[symbol] = {
            "price":           None,
            "last_signal":     None,
            "last_alert_time": 0,
            "support":         None,
            "resistance":      None,
            "rsi":             None,
        }

def remove_coin(symbol: str) -> None:
    """Remove a coin from tracking state and cancel its stream."""
    symbol = symbol.lower()
    if symbol in coin_state:
        del coin_state[symbol]
    if symbol in active_streams:
        active_streams[symbol].cancel()
        del active_streams[symbol]

# Initialise default coins
for s in DEFAULT_SYMBOLS:
    add_coin(s)

bot = telegram.Bot(token=TOKEN)

# ─────────────────────────────────────────────
# Telegram alert
# ─────────────────────────────────────────────
async def send_alert(message: str) -> None:
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message)
        logger.info(f"Alert sent: {message[:60]}…")
    except Exception as e:
        logger.error(f"Failed to send Telegram alert: {e}")

# ─────────────────────────────────────────────
# Binance helpers
# ─────────────────────────────────────────────
def validate_symbol(symbol: str) -> bool:
    """Check if symbol exists on Binance."""
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
            {
                "open":  float(c[1]),
                "high":  float(c[2]),
                "low":   float(c[3]),
                "close": float(c[4]),
            }
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

def calculate_pivot_levels(candles: list[dict]) -> tuple[float, float] | tuple[None, None]:
    if not candles:
        return None, None
    last       = candles[-2] if len(candles) >= 2 else candles[-1]
    pivot      = (last["high"] + last["low"] + last["close"]) / 3
    support    = round(2 * pivot - last["high"], 4)
    resistance = round(2 * pivot - last["low"],  4)
    return support, resistance

# ─────────────────────────────────────────────
# Telegram Commands
# ─────────────────────────────────────────────
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/add BNBUSDT"""
    if not context.args:
        await update.message.reply_text("Usage: /add BNBUSDT")
        return

    symbol = context.args[0].lower()

    if symbol in coin_state:
        await update.message.reply_text(f"⚠️ {symbol.upper()} is already being tracked.")
        return

    await update.message.reply_text(f"🔍 Validating {symbol.upper()}...")

    if not validate_symbol(symbol):
        await update.message.reply_text(f"❌ {symbol.upper()} not found on Binance. Check the symbol and try again.")
        return

    add_coin(symbol)

    # Start a new WebSocket stream for this coin
    task = asyncio.create_task(price_stream(symbol))
    active_streams[symbol] = task

    await update.message.reply_text(
        f"✅ Now tracking {symbol.upper()}\n"
        f"Total coins: {len(coin_state)}"
    )
    logger.info(f"Added coin: {symbol}")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/remove BNBUSDT"""
    if not context.args:
        await update.message.reply_text("Usage: /remove BNBUSDT")
        return

    symbol = context.args[0].lower()

    if symbol not in coin_state:
        await update.message.reply_text(f"⚠️ {symbol.upper()} is not being tracked.")
        return

    remove_coin(symbol)
    await update.message.reply_text(
        f"🗑️ Removed {symbol.upper()}\n"
        f"Remaining coins: {len(coin_state)}"
    )
    logger.info(f"Removed coin: {symbol}")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/list — show all tracked coins"""
    if not coin_state:
        await update.message.reply_text("No coins are being tracked. Use /add BTCUSDT to start.")
        return

    lines = ["📋 *Tracked Coins:*\n"]
    for symbol in coin_state:
        price = coin_state[symbol]["price"]
        price_str = f"${price:,.4f}" if price else "loading..."
        lines.append(f"• {symbol.upper()} — {price_str}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — show full indicator data for all coins"""
    if not coin_state:
        await update.message.reply_text("No coins being tracked. Use /add BTCUSDT.")
        return

    await update.message.reply_text("⏳ Fetching latest data...")

    lines = ["📊 *Current Status:*\n"]
    for symbol, state in coin_state.items():
        price      = state["price"]
        support    = state["support"]
        resistance = state["resistance"]
        rsi        = state["rsi"]
        signal     = state["last_signal"] or "None"

        lines.append(f"*{symbol.upper()}*")
        lines.append(f"  Price:      {f'${price:,.4f}' if price else 'loading...'}")
        lines.append(f"  Support:    {f'${support:,.4f}' if support else 'n/a'}")
        lines.append(f"  Resistance: {f'${resistance:,.4f}' if resistance else 'n/a'}")
        lines.append(f"  RSI:        {f'{rsi:.1f}' if rsi else 'n/a'}")
        lines.append(f"  Signal:     {signal}\n")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help"""
    await update.message.reply_text(
        "🤖 *Crypto Signal Bot Commands:*\n\n"
        "/add SYMBOL    — Start tracking a coin\n"
        "/remove SYMBOL — Stop tracking a coin\n"
        "/list          — Show all tracked coins\n"
        "/status        — Show prices, S/R & RSI\n"
        "/help          — Show this message\n\n"
        "Example: `/add BNBUSDT`",
        parse_mode="Markdown"
    )

# ─────────────────────────────────────────────
# WebSocket price stream
# ─────────────────────────────────────────────
async def price_stream(symbol: str) -> None:
    url = f"wss://stream.binance.com:443/ws/{symbol}@aggTrade"
    reconnect_delay = 5

    while symbol in coin_state:   # stop if coin was removed
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                logger.info(f"WebSocket connected: {symbol}")
                reconnect_delay = 5
                async for raw in ws:
                    if symbol not in coin_state:
                        return   # coin was removed while streaming
                    msg   = json.loads(raw)
                    price = float(msg["p"])
                    coin_state[symbol]["price"] = price

        except asyncio.CancelledError:
            logger.info(f"Stream cancelled: {symbol}")
            return
        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"WebSocket closed for {symbol}: {e}. Reconnecting in {reconnect_delay}s…")
        except Exception as e:
            logger.error(f"WebSocket error for {symbol}: {e}. Reconnecting in {reconnect_delay}s…")

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60)

# ─────────────────────────────────────────────
# Signal evaluation loop
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
    on_cooldown = (now - state["last_alert_time"]) < SIGNAL_COOLDOWN

    # SELL
    near_resistance = price >= resistance * (1 - PIVOT_TOLERANCE)
    rsi_overbought  = rsi is None or rsi >= RSI_OVERBOUGHT
    if near_resistance and rsi_overbought and last_signal != "SELL" and not on_cooldown:
        await send_alert(
            f"🚨 {symbol.upper()} SELL SIGNAL\n"
            f"Price:      {price:.4f}\n"
            f"Resistance: {resistance:.4f}\n"
            f"RSI:        {rsi_str}"
        )
        state["last_signal"]     = "SELL"
        state["last_alert_time"] = now
        return

    # BUY
    near_support = price <= support * (1 + PIVOT_TOLERANCE)
    rsi_oversold = rsi is None or rsi <= RSI_OVERSOLD
    if near_support and rsi_oversold and last_signal != "BUY" and not on_cooldown:
        await send_alert(
            f"📈 {symbol.upper()} BUY SIGNAL\n"
            f"Price:   {price:.4f}\n"
            f"Support: {support:.4f}\n"
            f"RSI:     {rsi_str}"
        )
        state["last_signal"]     = "BUY"
        state["last_alert_time"] = now
        return

    if support < price < resistance:
        state["last_signal"] = None


async def signal_loop(interval_seconds: int = 60) -> None:
    await asyncio.sleep(5)
    while True:
        tasks = [evaluate_signal(symbol) for symbol in list(coin_state.keys())]
        await asyncio.gather(*tasks)
        await asyncio.sleep(interval_seconds)

# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
async def main() -> None:
    # Start Telegram command listener
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("add",    cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help",   cmd_help))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    await send_alert(
        "🤖 Crypto signal bot started!\n"
        "Send /help to see available commands."
    )

    # Start default coin streams
    for symbol in list(coin_state.keys()):
        task = asyncio.create_task(price_stream(symbol))
        active_streams[symbol] = task

    # Run signal evaluation loop
    await signal_loop()

    # Cleanup (reached only on shutdown)
    await app.updater.stop()
    await app.stop()
    await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
