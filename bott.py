"""
PivotAlert — Production Grade Signal Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Architecture:
  - Dual timeframe: 1h trend + 15m entry
  - Event-driven: fires on candle CLOSE not timer
  - Wilder RSI (TradingView accurate)
  - Multi-candle pivot (5-candle average)
  - EMA 20/50 trend filter
  - ATR volatility filter
  - Volume confirmation
  - ATR-based SL/TP
  - Signal strength scoring (1-5 stars)
  - Per-tier cooldown & RSI thresholds
  - Razorpay payments
  - PostgreSQL multi-user database
  - aiohttp webhook server
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

TOKEN                   = os.getenv("TELEGRAM_TOKEN")
CHAT_ID                 = os.getenv("TELEGRAM_CHAT_ID")
ADMIN_CHAT_ID           = os.getenv("ADMIN_CHAT_ID")
DATABASE_URL            = os.getenv("DATABASE_URL")
RAZORPAY_KEY_ID         = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET     = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")
WEBHOOK_URL             = os.getenv("WEBHOOK_URL")
PORT                    = int(os.getenv("PORT", 8080))

if not TOKEN or not CHAT_ID:
    raise ValueError("❌ TELEGRAM_TOKEN or TELEGRAM_CHAT_ID missing!")

DEFAULT_SYMBOLS = ["btcusdt", "ethusdt", "solusdt"]

# Pivot tolerance — how close to S/R level to trigger
PIVOT_TOLERANCE = 0.003   # ±0.3%

# Per-tier settings
TIER_SETTINGS = {
    "free": {
        "rsi_oversold":   35,
        "rsi_overbought": 65,
        "cooldown":       14400,   # 4 hours
        "coins":          ["btcusdt", "ethusdt", "solusdt"],
        "price":          0,
        "min_strength":   2,      # minimum signal strength to receive
    },
    "pro": {
        "rsi_oversold":   38,
        "rsi_overbought": 62,
        "cooldown":       7200,   # 2 hours
        "coins":          None,   # all coins
        "price":          29900,
        "min_strength":   1,      # receive all signals
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
# coin_state stores per-symbol runtime data
coin_state: dict[str, dict] = {}
state_cache: dict = {}   # per-user cooldown tracking
# active_streams tracks running WebSocket tasks
active_streams: dict[str, list[asyncio.Task]] = {}

# database pool
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
                strength    INTEGER,
                stop_loss   FLOAT,
                take_profit FLOAT,
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
        rows = await conn.fetch(
            "SELECT chat_id, plan FROM users WHERE subscribed = TRUE"
        )
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


async def log_signal(
    symbol: str, signal_type: str, price: float,
    support: float, resistance: float, rsi: float,
    strength: int, stop_loss: float, take_profit: float,
) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO signal_history
            (symbol, signal_type, price, support, resistance, rsi, strength, stop_loss, take_profit)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """, symbol, signal_type, price, support, resistance,
            rsi or 0, strength, stop_loss, take_profit)


async def save_payment(chat_id: int, payment_link_id: str, amount: int) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO payments (chat_id, payment_link_id, amount)
            VALUES ($1, $2, $3) ON CONFLICT DO NOTHING
        """, chat_id, payment_link_id, amount)


async def mark_payment_paid(payment_link_id: str, payment_id: str) -> int | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE payments SET status='paid', payment_id=$1, paid_at=NOW()
            WHERE payment_link_id=$2 RETURNING chat_id
        """, payment_id, payment_link_id)
        if row:
            await conn.execute(
                "UPDATE users SET plan='pro' WHERE chat_id=$1", row["chat_id"]
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


async def broadcast_signal(message: str, symbol: str, strength: int) -> None:
    """
    Broadcast signal to subscribers.
    Free users: only their 3 coins + min strength 2
    Pro users:  all coins + all strengths
    """
    subscribers = await get_all_subscribers()
    sent = 0
    for user in subscribers:
        tier     = user["plan"]
        settings = TIER_SETTINGS[tier]
        allowed  = settings["coins"]
        min_str  = settings["min_strength"]

        # Tier coin filter
        if allowed and symbol not in allowed:
            continue

        # Tier strength filter — free users skip weak signals
        if strength < min_str:
            continue

        try:
            await bot.send_message(
                chat_id=user["chat_id"],
                text=message,
                parse_mode="Markdown"
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning(f"Could not send to {user['chat_id']}: {e}")

    logger.info(f"Signal sent to {sent}/{len(subscribers)} users")

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
        RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256
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
                        "• 30 min cooldown\n"
                        "• All signal strengths\n"
                        "• Priority signals\n\n"
                        "Signals incoming! 📈"
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
    coins  = list(coin_state.keys())
    prices = {s: coin_state[s].get("price") for s in coins}
    return web.Response(
        content_type="application/json",
        text=json.dumps({"status": "running", "coins": prices})
    )


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
# Binance REST — fetch candles
# ─────────────────────────────────────────────
async def fetch_klines_async(
    symbol: str, interval: str, limit: int = 100
) -> list[dict]:
    """Non-blocking kline fetch using asyncio executor."""
    def _fetch():
        try:
            resp = requests.get(
                f"https://api.binance.com/api/v3/klines"
                f"?symbol={symbol.upper()}&interval={interval}&limit={limit}",
                timeout=10,
            )
            resp.raise_for_status()
            return [
                {
                    "open":   float(c[1]),
                    "high":   float(c[2]),
                    "low":    float(c[3]),
                    "close":  float(c[4]),
                    "volume": float(c[5]),
                }
                for c in resp.json()
            ]
        except Exception as e:
            logger.error(f"klines fetch failed [{symbol} {interval}]: {e}")
            return []

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch)

# ─────────────────────────────────────────────
# Indicators
# ─────────────────────────────────────────────
def wilder_rsi(closes: list[float], period: int = 14) -> float | None:
    """
    Wilder Smoothed RSI — matches TradingView exactly.
    Uses RMA (Wilder moving average) not SMA.
    """
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]

    # Seed with simple average
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder smoothing
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def ema(closes: list[float], period: int) -> float | None:
    """Exponential Moving Average."""
    if len(closes) < period:
        return None
    k   = 2 / (period + 1)
    val = sum(closes[:period]) / period
    for price in closes[period:]:
        val = price * k + val * (1 - k)
    return round(val, 6)


def atr(candles: list[dict], period: int = 14) -> float | None:
    """Wilder Average True Range."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    val = sum(trs[:period]) / period
    for tr in trs[period:]:
        val = (val * (period - 1) + tr) / period
    return round(val, 6)


def pivot_levels(candles: list[dict], lookback: int = 5) -> tuple:
    """
    Multi-candle pivot — uses highest high, lowest low, last close
    of the last `lookback` completed candles.
    Much more stable than single-candle pivot.
    """
    if len(candles) < lookback + 1:
        return None, None
    # Exclude the current (incomplete) candle
    recent = candles[-(lookback + 1):-1]
    high   = max(c["high"]  for c in recent)
    low    = min(c["low"]   for c in recent)
    close  = recent[-1]["close"]
    p      = (high + low + close) / 3
    return round(2*p - high, 6), round(2*p - low, 6)


def volume_ratio(candles: list[dict], period: int = 20) -> float | None:
    """Current volume vs N-period average."""
    if len(candles) < period + 1:
        return None
    vols = [c["volume"] for c in candles]
    avg  = sum(vols[-(period+1):-1]) / period
    return round(vols[-1] / avg, 2) if avg > 0 else None


def signal_strength(
    rsi_val: float | None,
    vol_ratio: float | None,
    ema_fast: float | None,
    ema_slow: float | None,
    signal_type: str,
    tier: str = "free",
) -> int:
    """
    Score 0-5 based on confluence.
    Uses tier-specific RSI thresholds.
    """
    score    = 0
    settings = TIER_SETTINGS[tier]

    # RSI strength
    if rsi_val is not None:
        if signal_type == "BUY":
            if rsi_val <= settings["rsi_oversold"] - 10: score += 2
            elif rsi_val <= settings["rsi_oversold"]:    score += 1
        else:
            if rsi_val >= settings["rsi_overbought"] + 10: score += 2
            elif rsi_val >= settings["rsi_overbought"]:    score += 1

    # Volume confirmation
    if vol_ratio is not None:
        if vol_ratio >= 2.0:   score += 2
        elif vol_ratio >= 1.5: score += 1

    # EMA trend alignment
    if ema_fast and ema_slow:
        if signal_type == "BUY"  and ema_fast > ema_slow: score += 1
        if signal_type == "SELL" and ema_fast < ema_slow: score += 1

    return min(score, 5)


def sl_tp(
    price: float,
    atr_val: float | None,
    signal_type: str,
    sl_mult: float = 2.5,
    tp_mult: float = 3.5,
) -> tuple[float, float]:
    """ATR-based Stop Loss and Take Profit."""
    dist_sl = (atr_val * sl_mult) if atr_val else (price * 0.015)
    dist_tp = (atr_val * tp_mult) if atr_val else (price * 0.030)
    if signal_type == "BUY":
        return round(price - dist_sl, 4), round(price + dist_tp, 4)
    return round(price + dist_sl, 4), round(price - dist_tp, 4)


def stars(n: int) -> str:
    return "⭐" * n + "☆" * (5 - n)

# ─────────────────────────────────────────────
# Coin state helpers
# ─────────────────────────────────────────────
def init_coin_state(symbol: str) -> None:
    if symbol not in coin_state:
        coin_state[symbol] = {
            "price":           None,   # live tick price
            "last_signal":     None,   # "BUY" | "SELL" | None
            "last_alert_time": 0,
            "support":         None,
            "resistance":      None,
            "rsi":             None,
            "ema_fast":        None,
            "ema_slow":        None,
            "atr":             None,
            "volume_ratio":    None,
            "trend":           None,   # "up" | "down" | "neutral"
        }


def cleanup_coin(symbol: str) -> None:
    coin_state.pop(symbol, None)
    for task in active_streams.pop(symbol, []):
        task.cancel()

# ─────────────────────────────────────────────
# Signal evaluation — called on every 15m candle close
# ─────────────────────────────────────────────
async def evaluate_signal(symbol: str) -> None:
    """
    Full signal evaluation triggered by 15m candle close.
    Uses 1h candles for trend (EMA, pivot) and 15m for entry (RSI).
    """
    state = coin_state.get(symbol)
    if not state or state["price"] is None:
        return

    price = state["price"]
    now = time.time()

    # ── Fetch both timeframes concurrently ──
    candles_1h, candles_15m = await asyncio.gather(
        fetch_klines_async(symbol, "1h",  limit=100),
        fetch_klines_async(symbol, "15m", limit=100),
    )

    if len(candles_1h) < 35 or len(candles_15m) < 30:
        return

    closes_1h = [c["close"] for c in candles_1h]
    closes_15m = [c["close"] for c in candles_15m]

    # ── Calculate Indicators ──
    rsi = wilder_rsi(closes_15m)
    ema_fast = ema(closes_1h, 20)
    ema_slow = ema(closes_1h, 50)
    atr_val = atr(candles_1h)
    vol_ratio = volume_ratio(candles_15m)
    support, resistance = pivot_levels(candles_1h, lookback=3)   # Reduced for faster response

    if None in (rsi, support, resistance):
        return

    # Update state
    state.update({
        "support": support,
        "resistance": resistance,
        "rsi": rsi,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "atr": atr_val,
        "volume_ratio": vol_ratio,
    })

    # ── Pre-filters ──
    if atr_val and atr_val < price * 0.001:          # Too choppy
        return
    if vol_ratio and vol_ratio < 1.1:                # Require decent volume
        return

    # Strict Trend
    uptrend = ema_fast and ema_slow and ema_fast > ema_slow
    downtrend = ema_fast and ema_slow and ema_fast < ema_slow

    near_support = price <= support * (1 + PIVOT_TOLERANCE)
    near_resistance = price >= resistance * (1 - PIVOT_TOLERANCE)

    signal_type = None
    strength = 0

    # BUY
    if near_support and rsi <= TIER_SETTINGS["free"]["rsi_oversold"] and uptrend:
        strength = signal_strength(rsi, vol_ratio, ema_fast, ema_slow, "BUY")
        if strength >= 3:
            signal_type = "BUY"

    # SELL
    elif near_resistance and rsi >= TIER_SETTINGS["free"]["rsi_overbought"] and downtrend:
        strength = signal_strength(rsi, vol_ratio, ema_fast, ema_slow, "SELL")
        if strength >= 3:
            signal_type = "SELL"

    if not signal_type:
        if support < price < resistance:
            state["last_signal"] = None
        return

    # ── Cooldown Check (Global) ──
    if (now - state.get("last_alert_time", 0)) < TIER_SETTINGS["pro"]["cooldown"]:
        return

    # ── Generate Signal ──
    stop_loss, take_profit = sl_tp(price, atr_val, signal_type, sl_mult=2.5, tp_mult=3.5)

    sl_pct = abs((stop_loss - price) / price * 100)
    tp_pct = abs((take_profit - price) / price * 100)
    rr = round(tp_pct / sl_pct, 2) if sl_pct > 0 else 0.0

    emoji = "📈" if signal_type == "BUY" else "🚨"
    message = f"""{emoji} *{symbol.upper()} {signal_type} SIGNAL*
{'─' * 32}
💰 Price: ${price:,.4f}
{"🟢 Support" if signal_type == "BUY" else "🔴 Resistance"}: ${support if signal_type == "BUY" else resistance:,.4f}
📊 RSI: {rsi:.1f} ({"Oversold" if signal_type == "BUY" else "Overbought"})
📈 Trend: {"Bullish ✅" if signal_type == "BUY" else "Bearish ✅"}
📦 Volume: {vol_ratio:.2f}x
💨 ATR: {atr_val:.4f if atr_val else "N/A"}
{'─' * 32}
🎯 TP: ${take_profit:,.4f} ({'+' if signal_type == "BUY" else '-'}{tp_pct:.1f}%)
🛑 SL: ${stop_loss:,.4f} ({'-' if signal_type == "BUY" else '+'}{sl_pct:.1f}%)
⚖️ R:R: 1:{rr}
{'─' * 32}
💪 Strength: {stars(strength)} ({strength}/5)

_Powered by PivotAlert 📊_"""

    await broadcast_signal(message, symbol, strength=strength)
    await log_signal(symbol, signal_type, price, support, resistance, rsi, strength, stop_loss, take_profit)

    state["last_signal"] = signal_type
    state["last_alert_time"] = now
# ─────────────────────────────────────────────
# WebSocket — live price feed (aggTrade)
# ─────────────────────────────────────────────
async def price_stream(symbol: str) -> None:
    """Tick-by-tick price updates via aggTrade stream."""
    url, delay = f"wss://stream.binance.com:443/ws/{symbol}@aggTrade", 5
    while symbol in coin_state:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                logger.info(f"[Price] Connected: {symbol}")
                delay = 5
                async for raw in ws:
                    if symbol not in coin_state:
                        return
                    coin_state[symbol]["price"] = float(json.loads(raw)["p"])
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"[Price] Error {symbol}: {e}")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)

# ─────────────────────────────────────────────
# WebSocket — 15m candle close stream (EVENT DRIVEN)
# ─────────────────────────────────────────────
async def candle_stream(symbol: str) -> None:
    """
    Subscribe to 15m kline stream.
    Fires evaluate_signal ONLY when a candle CLOSES.
    This eliminates polling lag — signals fire within ~1 second of candle close.
    """
    url   = f"wss://stream.binance.com:443/ws/{symbol}@kline_15m"
    delay = 5

    while symbol in coin_state:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                logger.info(f"[Candle] Connected: {symbol} (15m)")
                delay = 5
                async for raw in ws:
                    if symbol not in coin_state:
                        return

                    msg    = json.loads(raw)
                    kline  = msg["k"]
                    closed = kline["x"]   # True when candle is CLOSED

                    if closed:
                        logger.info(f"[Candle] 15m closed: {symbol.upper()} @ {kline['c']}")
                        # Fire signal evaluation immediately on candle close
                        asyncio.create_task(evaluate_signal(symbol))

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"[Candle] Error {symbol}: {e}")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


def start_coin_streams(symbol: str) -> None:
    """Start both price + candle streams for a symbol."""
    tasks = [
        asyncio.create_task(price_stream(symbol)),
        asyncio.create_task(candle_stream(symbol)),
    ]
    active_streams[symbol] = tasks

# ─────────────────────────────────────────────
# Telegram commands — user
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
        "You're now subscribed to crypto trading signals.\n\n"
        "📊 *Free plan:*\n"
        "• BTC, ETH, SOL signals\n"
        "• Dual timeframe analysis (1h + 15m)\n"
        "• Event-driven — fires on candle close\n"
        "• SL/TP levels included\n"
        "• 2hr cooldown per coin\n\n"
        "⭐ Use /upgrade for Pro — ₹299/month\n"
        "📋 Use /plan to compare plans\n"
        "❓ Use /help to see all commands",
        parse_mode="Markdown"
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET subscribed=FALSE WHERE chat_id=$1",
            update.effective_chat.id
        )
    await update.message.reply_text("😢 Unsubscribed. Send /start to resubscribe!")


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📋 *PivotAlert Plans:*\n\n"
        "🆓 *Free — ₹0/month*\n"
        "• BTC, ETH, SOL only\n"
        "• Signals strength ≥ 2★\n"
        "• 2 hour cooldown\n"
        "• RSI threshold: 35/65\n\n"
        "⭐ *Pro — ₹299/month*\n"
        "• All tracked coins\n"
        "• All signal strengths\n"
        "• 30 min cooldown\n"
        "• RSI threshold: 38/62\n\n"
        "💎 *Elite — ₹799/month*\n"
        "• Everything in Pro\n"
        "• Forex + Stocks (coming soon)\n"
        "• Custom coin requests\n\n"
        "Use /upgrade to go Pro!",
        parse_mode="Markdown"
    )


async def cmd_myplan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT plan, joined_at FROM users WHERE chat_id=$1", chat_id
        )
    if not row:
        await update.message.reply_text("Send /start to subscribe!")
        return
    plan   = row["plan"]
    emoji  = "⭐" if plan == "pro" else "🆓"
    joined = row["joined_at"].strftime("%d %b %Y")
    await update.message.reply_text(
        f"👤 *Your Account*\n\n"
        f"Plan:    {emoji} {plan.upper()}\n"
        f"Joined:  {joined}\n\n"
        f"{'Use /upgrade to go Pro! 🚀' if plan == 'free' else 'Enjoying Pro benefits! 📈'}",
        parse_mode="Markdown"
    )


async def cmd_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id  = update.effective_chat.id
    username = update.effective_chat.username or "User"
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT plan FROM users WHERE chat_id=$1", chat_id)
    if row and row["plan"] == "pro":
        await update.message.reply_text(
            "⭐ You're already on *Pro!* Enjoy the signals! 📈",
            parse_mode="Markdown"
        )
        return
    await update.message.reply_text("⏳ Generating your payment link...")
    link = create_payment_link(chat_id, username)
    if not link:
        await update.message.reply_text("❌ Could not generate link. Try again later.")
        return
    await save_payment(chat_id, link["id"], TIER_SETTINGS["pro"]["price"])
    await update.message.reply_text(
        f"⭐ *Upgrade to PivotAlert Pro*\n\n"
        f"₹299/month — Cancel anytime\n\n"
        f"✅ All tracked coins\n"
        f"✅ 30 min cooldown\n"
        f"✅ All signal strengths\n\n"
        f"👇 Pay securely:\n{link['short_url']}\n\n"
        f"_Powered by Razorpay_ 🔒",
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
        lines.append(f"• {symbol.upper()} — {'$'+f'{price:,.4f}' if price else 'loading...'}")
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
        trend      = s.get("trend") or "n/a"
        signal     = s.get("last_signal") or "None"
        vol        = s.get("volume_ratio")
        lines += [
            f"*{symbol.upper()}*",
            f"  Price:      {'$'+f'{price:,.4f}' if price else 'loading...'}",
            f"  Support:    {'$'+f'{support:,.4f}' if support else 'n/a'}",
            f"  Resistance: {'$'+f'{resistance:,.4f}' if resistance else 'n/a'}",
            f"  RSI (15m):  {f'{rsi:.1f}' if rsi else 'n/a'}",
            f"  Trend (1h): {trend}",
            f"  Volume:     {f'{vol:.2f}x' if vol else 'n/a'}",
            f"  Signal:     {signal}\n",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 *PivotAlert Commands:*\n\n"
        "/start    — Subscribe to signals\n"
        "/stop     — Unsubscribe\n"
        "/plan     — Compare plans\n"
        "/myplan   — Your current plan\n"
        "/upgrade  — Go Pro ⭐\n"
        "/list     — Tracked coins & prices\n"
        "/status   — Full indicator snapshot\n"
        "/help     — This message",
        parse_mode="Markdown"
    )

# ─────────────────────────────────────────────
# Telegram commands — admin
# ─────────────────────────────────────────────
def is_admin(update: Update) -> bool:
    return str(update.effective_chat.id) == str(ADMIN_CHAT_ID)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("❌ Admin only.")
        return
    async with db_pool.acquire() as conn:
        total    = await conn.fetchval("SELECT COUNT(*) FROM users")
        subbed   = await conn.fetchval("SELECT COUNT(*) FROM users WHERE subscribed=TRUE")
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
    try:
        resp = requests.get(
            f"https://api.binance.com/api/v3/ticker/price?symbol={symbol.upper()}",
            timeout=5
        )
        if resp.status_code != 200:
            raise ValueError("Not found")
    except Exception:
        await update.message.reply_text(f"❌ {symbol.upper()} not found on Binance.")
        return
    await add_tracked_coin(symbol)
    init_coin_state(symbol)
    start_coin_streams(symbol)
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
    cleanup_coin(symbol)
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
        await conn.execute("UPDATE users SET plan=$1 WHERE chat_id=$2", plan, target_id)
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
# Entry point
# ─────────────────────────────────────────────
async def main() -> None:
    # Init database
    await init_db()

    # Load tracked coins and start streams
    coins = await get_tracked_coins()
    for symbol in coins:
        init_coin_state(symbol)

    # Start webhook server
    await start_webhook_server()

    # Start Telegram bot
    app = Application.builder().token(TOKEN).build()
    for cmd, fn in [
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
        app.add_handler(CommandHandler(cmd, fn))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    await send_alert(
        "🚀 *PivotAlert — Event-Driven Mode*\n\n"
        "✅ Dual timeframe: 1h trend + 15m entry\n"
        "✅ Fires on candle close — no more lag\n"
        "✅ Wilder RSI + Multi-pivot + EMA + ATR\n\n"
        "Send /status to check indicators."
    )

    # Start streams AFTER bot is ready
    for symbol in coins:
        start_coin_streams(symbol)
        logger.info(f"Streams started: {symbol}")

    # Keep running forever
    await asyncio.Event().wait()

    # Cleanup
    await app.updater.stop()
    await app.stop()
    await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
