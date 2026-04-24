"""
Crypto Trading Signal Bot
- Real-time Binance WebSocket price feeds
- Pivot point-based support/resistance
- RSI confirmation filter
- Per-coin signal cooldowns
- Structured logging
- Graceful error handling & self-alerts
"""

import os
import asyncio
import logging
import time
from collections import deque
from dotenv import load_dotenv
import websockets
import json
import requests
import telegram


load_dotenv()
 
TOKEN   = "8681412895:AAGr8BRbEyvsAdBD4WY_t3NqWlH3FGlAkOg"
CHAT_ID = "8068895394"
SYMBOLS = ["btcusdt", "ethusdt", "solusdt"]

RSI_PERIOD        = 14
RSI_OVERSOLD      = 35
RSI_OVERBOUGHT    = 65
KLINE_INTERVAL    = "1h"          # candle interval for pivot + RSI
KLINE_LIMIT       = 50            # candles to fetch
SIGNAL_COOLDOWN   = 3600          # seconds between alerts per coin
PIVOT_TOLERANCE   = 0.003         # ±0.3 % around pivot levels counts as a hit

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
coin_state: dict[str, dict] = {
    s: {
        "price":           None,
        "last_signal":     None,   # "BUY" | "SELL" | None
        "last_alert_time": 0,
        "support":         None,
        "resistance":      None,
        "rsi":             None,
    }
    for s in SYMBOLS
}

bot = telegram.Bot(token=TOKEN)

# ─────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────
async def send_alert(message: str) -> None:
    try:
        await bot.send_message(chat_id=CHAT_ID, text=message)
        logger.info(f"Alert sent: {message[:60]}…")
    except Exception as e:
        logger.error(f"Failed to send Telegram alert: {e}")

# ─────────────────────────────────────────────
# Binance REST – klines (candles)
# ─────────────────────────────────────────────
def fetch_klines(symbol: str, interval: str = KLINE_INTERVAL, limit: int = KLINE_LIMIT) -> list[dict]:
    """Return list of OHLCV dicts for the given symbol."""
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={symbol.upper()}&interval={interval}&limit={limit}"
    )
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
        return [
            {
                "open":  float(c[1]),
                "high":  float(c[2]),
                "low":   float(c[3]),
                "close": float(c[4]),
            }
            for c in raw
        ]
    except Exception as e:
        logger.error(f"klines fetch failed for {symbol}: {e}")
        return []

# ─────────────────────────────────────────────
# Indicators
# ─────────────────────────────────────────────
def calculate_rsi(closes: list[float], period: int = RSI_PERIOD) -> float | None:
    """Wilder RSI on the last `period` closes."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        delta = closes[-period - 1 + i] - closes[-period - 2 + i]
        (gains if delta > 0 else losses).append(abs(delta))
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 1e-10
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_pivot_levels(candles: list[dict]) -> tuple[float, float] | tuple[None, None]:
    """
    Classic pivot point using the most recent completed candle.
    S1 = 2*P - High   R1 = 2*P - Low
    """
    if not candles:
        return None, None
    last = candles[-2] if len(candles) >= 2 else candles[-1]   # use completed bar
    pivot      = (last["high"] + last["low"] + last["close"]) / 3
    support    = round(2 * pivot - last["high"], 4)
    resistance = round(2 * pivot - last["low"],  4)
    return support, resistance

# ─────────────────────────────────────────────
# Signal logic
# ─────────────────────────────────────────────
async def evaluate_signal(symbol: str) -> None:
    state = coin_state[symbol]
    price = state["price"]
    if price is None:
        return

    # ── Fetch candles & recalculate indicators ──
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

    logger.info(
        f"{symbol.upper():10s} | price={price:.4f} | "
        f"S={support:.4f} | R={resistance:.4f} | RSI={f'{rsi:.1f}' if rsi else 'n/a'}"
    )

    now         = time.time()
    last_signal = state["last_signal"]
    on_cooldown = (now - state["last_alert_time"]) < SIGNAL_COOLDOWN

    # ── SELL ──
    near_resistance = price >= resistance * (1 - PIVOT_TOLERANCE)
    rsi_overbought  = rsi is None or rsi >= RSI_OVERBOUGHT

    if near_resistance and rsi_overbought and last_signal != "SELL" and not on_cooldown:
        await send_alert(
            f"🚨 {symbol.upper()} SELL SIGNAL\n"
            f"Price:      {price:.4f}\n"
            f"Resistance: {resistance:.4f}\n"
            f"RSI:        {rsi:.1f if rsi else 'n/a'}"
        )
        state["last_signal"]     = "SELL"
        state["last_alert_time"] = now
        return

    # ── BUY ──
    near_support  = price <= support * (1 + PIVOT_TOLERANCE)
    rsi_oversold  = rsi is None or rsi <= RSI_OVERSOLD

    if near_support and rsi_oversold and last_signal != "BUY" and not on_cooldown:
        await send_alert(
            f"📈 {symbol.upper()} BUY SIGNAL\n"
            f"Price:   {price:.4f}\n"
            f"Support: {support:.4f}\n"
            f"RSI:     {rsi:.1f if rsi else 'n/a'}"
        )
        state["last_signal"]     = "BUY"
        state["last_alert_time"] = now
        return

    # ── Reset if price back inside range ──
    if support < price < resistance:
        state["last_signal"] = None

# ─────────────────────────────────────────────
# WebSocket price feed
# ─────────────────────────────────────────────
async def price_stream(symbol: str) -> None:
    """Connect to Binance trade stream; update price and run signals."""
    url = f"wss://stream.binance.com:9443/ws/{symbol}@aggTrade"
    reconnect_delay = 5

    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                logger.info(f"WebSocket connected: {symbol}")
                reconnect_delay = 5   # reset on successful connect
                async for raw in ws:
                    msg   = json.loads(raw)
                    price = float(msg["p"])
                    coin_state[symbol]["price"] = price

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"WebSocket closed for {symbol}: {e}. Reconnecting in {reconnect_delay}s…")
        except Exception as e:
            logger.error(f"WebSocket error for {symbol}: {e}. Reconnecting in {reconnect_delay}s…")
            await send_alert(f"⚠️ Bot WebSocket error ({symbol}): {e}")

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60)   # exponential back-off, cap 60s

# ─────────────────────────────────────────────
# Periodic signal evaluation loop
# ─────────────────────────────────────────────
async def signal_loop(interval_seconds: int = 60) -> None:
    """Every `interval_seconds`, evaluate signals for all coins."""
    await asyncio.sleep(5)   # brief warm-up so WebSocket can populate prices
    while True:
        tasks = [evaluate_signal(symbol) for symbol in SYMBOLS]
        await asyncio.gather(*tasks)
        await asyncio.sleep(interval_seconds)

# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
async def main() -> None:
    await send_alert("🤖 Crypto signal bot started.")
    streams     = [price_stream(symbol) for symbol in SYMBOLS]
    all_tasks   = streams + [signal_loop()]
    await asyncio.gather(*all_tasks)


if __name__ == "__main__":
    asyncio.run(main())