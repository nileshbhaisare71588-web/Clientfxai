import os
import ccxt
import pandas as pd
import numpy as np
import asyncio
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Bot
from flask import Flask, jsonify, render_template_string
import threading
import time

# --- CONFIGURATION ---
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Your requested pairs
FOREX_PAIRS = ["EUR/USD", "GBP/JPY", "AUD/USD", "GBP/USD", "XAU/USD", "AUD/CAD", "AUD/JPY", "BTC/USD"]

TIMEFRAME_MAIN = "4h"
TIMEFRAME_ENTRY = "1h"

# Initialize
bot = Bot(token=TELEGRAM_BOT_TOKEN)
exchange = ccxt.kraken({'enableRateLimit': True, 'rateLimit': 2000})

bot_stats = {
    "status": "initializing",
    "total_analyses": 0,
    "last_analysis": None,
    "monitored_assets": FOREX_PAIRS,
    "uptime_start": datetime.now().isoformat(),
    "version": "V3.0 Simple Elite"
}

# =========================================================================
# === SIMPLE INDICATOR CALCULATIONS ===
# =========================================================================

def calculate_indicators(df):
    """Calculate essential indicators"""
    if len(df) < 50:
        return df
    
    close = df['close']
    high = df['high']
    low = df['low']
    
    # Moving Averages
    df['sma9'] = close.rolling(9).mean()
    df['sma20'] = close.rolling(20).mean()
    df['sma50'] = close.rolling(50).mean()
    df['ema9'] = close.ewm(span=9, adjust=False).mean()
    df['ema21'] = close.ewm(span=21, adjust=False).mean()
    
    # RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = -delta.where(delta < 0, 0).rolling(14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df['macd'] = ema12 - ema26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']
    
    # Bollinger Bands
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    df['bb_upper'] = sma20 + (std20 * 2)
    df['bb_lower'] = sma20 - (std20 * 2)
    
    # ATR (Average True Range)
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df['atr'] = tr.rolling(14).mean()
    
    # ADX (Trend Strength)
    up_move = high - high.shift()
    down_move = low.shift() - low
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
    atr_val = df['atr'].fillna(1)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).rolling(14).mean() / atr_val
    minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(14).mean() / atr_val
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
    df['adx'] = dx.rolling(14).mean()
    
    return df.dropna()

def calculate_cpr_levels(df_daily):
    """CPR Pivot Points"""
    if df_daily.empty or len(df_daily) < 2:
        return None
    
    prev_day = df_daily.iloc[-2]
    H, L, C = prev_day['high'], prev_day['low'], prev_day['close']
    PP = (H + L + C) / 3.0
    BC = (H + L) / 2.0
    TC = PP - BC + PP
    
    return {
        'PP': PP, 'TC': TC, 'BC': BC,
        'R1': 2 * PP - L, 'S1': 2 * PP - H,
        'R2': PP + (H - L), 'S2': PP - (H - L)
    }

def fetch_data_safe(symbol, timeframe):
    """Fetch data with retry logic"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if not exchange.markets:
                exchange.load_markets()
            market_id = exchange.market(symbol)['id']
            ohlcv = exchange.fetch_ohlcv(market_id, timeframe, limit=100)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            df = calculate_indicators(df)
            return df
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(5)
    return pd.DataFrame()

# =========================================================================
# === ADVANCED SIGNAL LOGIC ===
# =========================================================================

def analyze_signal(df_4h, df_1h, cpr, price):
    """Improved multi-indicator confluence analysis"""
    score = 0
    
    row_4h = df_4h.iloc[-1]
    row_1h = df_1h.iloc[-1]
    
    # 1. TREND ANALYSIS (40 points)
    trend_score = 0
    if row_4h['ema9'] > row_4h['ema21'] > row_4h['sma50'] and row_1h['ema9'] > row_1h['ema21']:
        trend_score = 40  # Strong bullish
    elif row_4h['ema9'] < row_4h['ema21'] < row_4h['sma50'] and row_1h['ema9'] < row_1h['ema21']:
        trend_score = -40  # Strong bearish
    elif row_4h['ema9'] > row_4h['ema21'] and row_1h['ema9'] > row_1h['ema21']:
        trend_score = 25  # Moderate bullish
    elif row_4h['ema9'] < row_4h['ema21'] and row_1h['ema9'] < row_1h['ema21']:
        trend_score = -25  # Moderate bearish
    
    # ADX confirmation (strong trend = more reliable)
    if row_4h['adx'] > 25:
        trend_score = trend_score * 1.2
    
    # 2. MOMENTUM (30 points)
    momentum_score = 0
    
    # RSI
    if 30 < row_1h['rsi'] < 45:
        momentum_score += 10
    elif 55 < row_1h['rsi'] < 70:
        momentum_score -= 10
    elif row_1h['rsi'] < 30:
        momentum_score += 15
    elif row_1h['rsi'] > 70:
        momentum_score -= 15
    
    # MACD
    if row_1h['macd'] > row_1h['macd_signal'] and row_1h['macd_hist'] > 0:
        momentum_score += 15
    elif row_1h['macd'] < row_1h['macd_signal'] and row_1h['macd_hist'] < 0:
        momentum_score -= 15
    
    # 3. VOLATILITY & POSITION (30 points)
    volatility_score = 0
    
    # Bollinger Bands
    bb_position = (price - row_1h['bb_lower']) / (row_1h['bb_upper'] - row_1h['bb_lower'])
    if bb_position < 0.3:
        volatility_score += 20
    elif bb_position > 0.7:
        volatility_score -= 20
    
    # CPR Position
    if cpr:
        if price > cpr['PP']:
            volatility_score += 10
        else:
            volatility_score -= 10
    
    # Total Score
    total_score = trend_score + momentum_score + volatility_score
    
    if total_score > 0:
        score = min(100, abs(total_score))
        direction = "BUY"
    else:
        score = min(100, abs(total_score))
        direction = "SELL"
    
    return score, direction

def calculate_targets(df, price, direction, cpr, atr_multiplier=2.0):
    """Calculate SL and TP levels"""
    atr = df.iloc[-1]['atr']
    
    if direction == "BUY":
        stop_loss = price - (atr * atr_multiplier)
        tp1 = price + (atr * 2)
        tp2 = price + (atr * 3)
        if cpr:
            stop_loss = max(stop_loss, cpr['S1'])
            tp1 = min(tp1, cpr['R1'])
            tp2 = min(tp2, cpr['R2'])
    else:
        stop_loss = price + (atr * atr_multiplier)
        tp1 = price - (atr * 2)
        tp2 = price - (atr * 3)
        if cpr:
            stop_loss = min(stop_loss, cpr['R1'])
            tp1 = max(tp1, cpr['S1'])
            tp2 = max(tp2, cpr['S2'])
    
    risk = abs(price - stop_loss)
    reward = abs(tp1 - price)
    rr_ratio = reward / risk if risk > 0 else 0
    
    return {
        'sl': stop_loss,
        'tp1': tp1,
        'tp2': tp2,
        'rr_ratio': rr_ratio,
        'atr': atr
    }

# =========================================================================
# === SIGNAL GENERATION ===
# =========================================================================

def generate_and_send_signal(symbol):
    global bot_stats
    
    try:
        # 1. Fetch data
        df_4h = fetch_data_safe(symbol, TIMEFRAME_MAIN)
        df_1h = fetch_data_safe(symbol, TIMEFRAME_ENTRY)
        
        if df_4h.empty or df_1h.empty:
            return
        
        # 2. Get CPR levels
        if not exchange.markets:
            exchange.load_markets()
        market_id = exchange.market(symbol)['id']
        ohlcv_d = exchange.fetch_ohlcv(market_id, '1d', limit=5)
        df_d = pd.DataFrame(ohlcv_d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        cpr = calculate_cpr_levels(df_d)
        
        if cpr is None:
            return
        
        # 3. Analyze
        price = df_1h.iloc[-1]['close']
        score, direction = analyze_signal(df_4h, df_1h, cpr, price)
        
        # 4. Calculate targets
        targets = calculate_targets(df_1h, price, direction, cpr)
        
        # 5. Filter weak signals
        if score < 60 or targets['rr_ratio'] < 1.5:
            return
        
        # 6. Determine signal strength
        if score >= 80 and targets['rr_ratio'] >= 2.0:
            signal_strength = "VERY STRONG"
            emoji = "🔥🚀" if direction == "BUY" else "🔥🔻"
        elif score >= 70:
            signal_strength = "STRONG"
            emoji = "🚀" if direction == "BUY" else "🔻"
        else:
            signal_strength = "MEDIUM"
            emoji = "📊" if direction == "BUY" else "📉"
        
        # 7. Format message
        decimals = 5 if 'JPY' not in symbol else 3
        if 'XAU' in symbol or 'BTC' in symbol:
            decimals = 2
        
        trend_4h = "BULLISH 📈" if df_4h.iloc[-1]['ema9'] > df_4h.iloc[-1]['ema21'] else "BEARISH 📉"
        trend_1h = "BULLISH 📈" if df_1h.iloc[-1]['ema9'] > df_1h.iloc[-1]['ema21'] else "BEARISH 📉"
        
        message = (
            f"╔═══════════════════════════════╗\n"
            f"  💎 <b>FOREX AI ELITE SIGNAL</b> 💎\n"
            f"╚═══════════════════════════════╝\n\n"
            f"<b>📌 Pair:</b> <code>{symbol}</code>\n"
            f"<b>💰 Price:</b> <code>{price:.{decimals}f}</code>\n"
            f"<b>⏰ Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>{emoji} SIGNAL: {direction} - {signal_strength}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>📊 CONFIDENCE: {score:.0f}/100</b>\n\n"
            f"<b>🔍 ANALYSIS:</b>\n"
            f"├ 4H Trend: <code>{trend_4h}</code>\n"
            f"├ 1H Trend: <code>{trend_1h}</code>\n"
            f"├ RSI: <code>{df_1h.iloc[-1]['rsi']:.1f}</code>\n"
            f"├ ADX: <code>{df_1h.iloc[-1]['adx']:.1f}</code> (Strength)\n"
            f"└ MACD: <code>{'Bullish ✅' if df_1h.iloc[-1]['macd_hist'] > 0 else 'Bearish ⚠️'}</code>\n\n"
            f"<b>🎯 TRADE SETUP:</b>\n"
            f"├ 🟢 <b>Entry:</b> <code>{price:.{decimals}f}</code>\n"
            f"├ 🛑 <b>Stop Loss:</b> <code>{targets['sl']:.{decimals}f}</code>\n"
            f"├ ✅ <b>Take Profit 1:</b> <code>{targets['tp1']:.{decimals}f}</code>\n"
            f"└ 🔥 <b>Take Profit 2:</b> <code>{targets['tp2']:.{decimals}f}</code>\n\n"
            f"<b>📈 RISK:</b> R:R = <code>1:{targets['rr_ratio']:.2f}</code>\n\n"
            f"<b>🏛️ CPR LEVELS:</b>\n"
            f"├ Pivot: <code>{cpr['PP']:.{decimals}f}</code>\n"
            f"├ R1: <code>{cpr['R1']:.{decimals}f}</code> | S1: <code>{cpr['S1']:.{decimals}f}</code>\n"
            f"└ R2: <code>{cpr['R2']:.{decimals}f}</code> | S2: <code>{cpr['S2']:.{decimals}f}</code>\n\n"
            f"─────────────────────────────────\n"
            f"<i>⚡ Elite AI V3.0 | Use SL Always</i>"
        )
        
        # 8. Send signal
        asyncio.run(bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode='HTML'
        ))
        
        bot_stats['total_analyses'] += 1
        bot_stats['last_analysis'] = datetime.now().isoformat()
        bot_stats['status'] = "operational"
        
        print(f"✅ {symbol}: {direction} ({score:.0f})")
        
    except Exception as e:
        print(f"❌ Error {symbol}: {e}")

# =========================================================================
# === BOT START ===
# =========================================================================

def start_bot():
    print(f"🚀 Starting {bot_stats['version']}")
    print(f"📊 Monitoring: {', '.join(FOREX_PAIRS)}")
    print(f"⏰ Signals every 30 minutes\n")
    
    scheduler = BackgroundScheduler()
    
    for pair in FOREX_PAIRS:
        scheduler.add_job(generate_and_send_signal, 'cron', minute='0,30', args=[pair])
    
    scheduler.start()
    
    # Initial analysis
    for pair in FOREX_PAIRS:
        threading.Thread(target=generate_and_send_signal, args=(pair,), daemon=True).start()
        time.sleep(2)

start_bot()

# =========================================================================
# === FLASK DASHBOARD ===
# =========================================================================

app = Flask(__name__)

@app.route('/')
def home():
    return render_template_string("""
        <body style="font-family:sans-serif; background:#020617; color:#f8fafc; text-align:center; padding-top:100px;">
            <div style="background:#0f172a; display:inline-block; padding:40px; border-radius:12px; border:1px solid #1e293b;">
                <h1 style="color:#38bdf8;">💎 Forex AI Elite</h1>
                <p>Status: <span style="color:#4ade80;">{{s}}</span> | Version: {{v}}</p>
                <hr style="border-color:#1e293b;">
                <p>Signals: <b>{{a}}</b></p>
                <p style="font-size:0.8em; color:#94a3b8;">{{t}}</p>
            </div>
        </body>
    """, s=bot_stats['status'], v=bot_stats['version'], 
         a=bot_stats['total_analyses'], t=bot_stats['last_analysis'])

@app.route('/health')
def health():
    return jsonify({"status": "healthy"}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
