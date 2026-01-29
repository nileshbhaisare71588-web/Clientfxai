# main.py - PREMIER FOREX AI QUANT V3.5 (Stable & Advanced)

import os
import ccxt
import pandas as pd
import numpy as np
import asyncio
import threading
import time
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Bot
from flask import Flask, jsonify, render_template_string

# --- CONFIGURATION ---
from dotenv import load_dotenv 
load_dotenv() 

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Clean list of pairs
FOREX_PAIRS = [
    "EUR/USD", "GBP/JPY", "AUD/USD", "GBP/USD", 
    "XAU/USD", "AUD/CAD", "AUD/JPY", "BTC/USD"
]

TIMEFRAME_MAIN = "4h"  # Major Trend (Trend Filter)
TIMEFRAME_ENTRY = "1h" # Entry Precision

# Initialize Bot and Exchange 
bot = Bot(token=TELEGRAM_BOT_TOKEN)
exchange = ccxt.kraken({
    'enableRateLimit': True, 
    'rateLimit': 2000,
    'params': {'timeout': 20000} 
})

bot_stats = {
    "status": "initializing",
    "total_analyses": 0,
    "last_analysis": None,
    "monitored_assets": FOREX_PAIRS,
    "uptime_start": datetime.now().isoformat(),
    "version": "V3.5 Stable Trend"
}

# =========================================================================
# === ADVANCED MATH (No Heavy ML Libraries) ===
# =========================================================================

def calculate_indicators(df):
    """Calculates EMA, RSI, and ATR using standard Pandas."""
    # 1. EMAs (Exponential Moving Averages)
    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean() # THE TREND FILTER
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()   # Intermediate
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()   # Fast trigger
    
    # 2. RSI (Relative Strength Index - 14 period)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))

    # 3. ATR (Average True Range) for Volatility
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    df['atr'] = true_range.rolling(14).mean()
    
    return df

def calculate_cpr_levels(df_daily):
    """Calculates Pivot Points for Targets."""
    if df_daily.empty or len(df_daily) < 2: return None
    prev_day = df_daily.iloc[-2]
    H, L, C = prev_day['high'], prev_day['low'], prev_day['close']
    PP = (H + L + C) / 3.0
    R1 = 2*PP - L
    S1 = 2*PP - H
    R2 = PP + (H - L)
    S2 = PP - (H - L)
    return {'PP': PP, 'R1': R1, 'S1': S1, 'R2': R2, 'S2': S2}

def fetch_data_safe(symbol, timeframe, limit=300):
    """Fetches data safely without crashing the bot."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # Lazy load markets only when needed
            if not exchange.markets: exchange.load_markets()
            
            # Normalize Symbol if needed
            try:
                market = exchange.market(symbol)
            except:
                market = exchange.market(symbol.replace("/", "")) # Fallback
                
            ohlcv = exchange.fetch_ohlcv(market['symbol'], timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            
            # Add Indicators
            df = calculate_indicators(df)
            return df.dropna()
        except Exception as e:
            print(f"⚠️ Fetch Error {symbol}: {e}")
            if attempt < max_retries - 1: time.sleep(2)
    return pd.DataFrame()

# =========================================================================
# === MASTER SIGNAL LOGIC (Improved Success Rate) ===
# =========================================================================

def generate_and_send_signal(symbol):
    global bot_stats
    try:
        # 1. Fetch Data
        df_4h = fetch_data_safe(symbol, TIMEFRAME_MAIN) # For Trend
        df_1h = fetch_data_safe(symbol, TIMEFRAME_ENTRY) # For Entry
        
        # 2. Get Targets
        if not exchange.markets: exchange.load_markets()
        ohlcv_d = exchange.fetch_ohlcv(exchange.market(symbol)['symbol'], '1d', limit=5)
        df_d = pd.DataFrame(ohlcv_d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        cpr = calculate_cpr_levels(df_d)

        if df_4h.empty or df_1h.empty or cpr is None: return

        # 3. Analyze
        current_price = df_1h.iloc[-1]['close']
        rsi = df_1h.iloc[-1]['rsi']
        atr = df_1h.iloc[-1]['atr']
        
        # RULE 1: THE TREND FILTER (200 EMA on 4H)
        # We only Buy if price is above 200 EMA. We only Sell if below.
        trend_is_bullish = df_4h.iloc[-1]['close'] > df_4h.iloc[-1]['ema200']
        
        # RULE 2: ENTRY TRIGGER (1H Chart)
        ema21 = df_1h.iloc[-1]['ema21']
        ema50 = df_1h.iloc[-1]['ema50']
        
        signal = "WAIT"
        emoji = "⏳"
        
        # BUY LOGIC
        if trend_is_bullish:
            # 1H Confluence: Price > EMA50 AND RSI > 50 (Momentum)
            if current_price > ema50 and rsi > 50:
                 if current_price > cpr['PP']: # Above Pivot
                    signal = "BUY"
                    emoji = "🟢"

        # SELL LOGIC
        elif not trend_is_bullish:
            # 1H Confluence: Price < EMA50 AND RSI < 50 (Momentum)
            if current_price < ema50 and rsi < 50:
                if current_price < cpr['PP']: # Below Pivot
                    signal = "SELL"
                    emoji = "🔴"

        if signal == "WAIT": return 

        # 4. Risk Calculation (Using ATR)
        stop_loss_pips = atr * 1.5 
        
        if signal == "BUY":
            sl = current_price - stop_loss_pips
            tp1 = cpr['R1']
            tp2 = cpr['R2']
            if tp1 < current_price: tp1 = cpr['R2']; tp2 = tp1 + (tp1-sl)
        else:
            sl = current_price + stop_loss_pips
            tp1 = cpr['S1']
            tp2 = cpr['S2']
            if tp1 > current_price: tp1 = cpr['S2']; tp2 = tp1 - (sl-tp1)

        decimals = 2 if 'JPY' in symbol or 'XAU' in symbol else 4
        if 'BTC' in symbol: decimals = 2

        # 5. Send Message
        message = (
            f"⚡ <b>PREMIER AI SIGNAL {emoji}</b>\n"
            f"───────────────────\n"
            f"💎 <b>Asset:</b> #{symbol.replace('/','')}\n"
            f"📊 <b>Side:</b> {signal} NOW\n"
            f"💵 <b>Price:</b> <code>{current_price:.{decimals}f}</code>\n\n"
            f"🎯 <b>Targets:</b>\n"
            f"1️⃣ TP1: <code>{tp1:.{decimals}f}</code>\n"
            f"2️⃣ TP2: <code>{tp2:.{decimals}f}</code>\n"
            f"🛡️ SL:  <code>{sl:.{decimals}f}</code>\n\n"
            f"🧠 <b>AI Reason:</b>\n"
            f"• Trend (4H): {'Bullish' if trend_is_bullish else 'Bearish'}\n"
            f"• RSI: {rsi:.1f} (Momentum)\n"
            f"• Volatility: {'Normal' if atr > 0 else 'Low'}\n"
            f"───────────────────"
        )

        asyncio.run(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML'))
        
        bot_stats['total_analyses'] += 1
        bot_stats['last_analysis'] = datetime.now().isoformat()

    except Exception as e:
        print(f"❌ Analysis failed for {symbol}: {e}")

# =========================================================================
# === SAFE BOOT SEQUENCE (Fixes Gunicorn Crash) ===
# =========================================================================

app = Flask(__name__)
scheduler = BackgroundScheduler()

def start_bot_safely():
    """Starts the scheduler in a background thread."""
    try:
        if not scheduler.running:
            print(f"🚀 Booting {bot_stats['version']}...")
            for s in FOREX_PAIRS:
                # Run every 15 mins
                scheduler.add_job(generate_and_send_signal, 'cron', minute='0,15,30,45', args=[s], id=f"job_{s}")
            scheduler.start()
            print("✅ Scheduler Active.")
    except Exception as e:
        print(f"⚠️ Scheduler Error: {e}")

# Trigger start inside Flask context to prevent Gunicorn timeout
with app.app_context():
    threading.Thread(target=start_bot_safely).start()

@app.route('/')
def home():
    return render_template_string("""
        <body style="font-family:sans-serif; background:#0f172a; color:#f8fafc; text-align:center; padding-top:50px;">
            <div style="border:1px solid #334155; padding:30px; border-radius:10px; max-width:500px; margin:auto;">
                <h2 style="color:#38bdf8;">Forex AI Status</h2>
                <p>Status: <span style="color:#4ade80;">Running</span></p>
                <p>Signals Sent: <b>{{a}}</b></p>
                <p>Last Activity: {{t}}</p>
            </div>
        </body>
    """, a=bot_stats['total_analyses'], t=bot_stats['last_analysis'])

@app.route('/health')
def health(): return jsonify({"status": "healthy"}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
