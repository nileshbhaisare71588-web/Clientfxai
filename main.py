import os
import requests
import pandas as pd
import numpy as np
import asyncio
import time
import traceback
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Bot
from flask import Flask, render_template_string
import threading

# --- CONFIGURATION ---
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 🟢 PASTE YOUR TWELVEDATA API KEY HERE
# If you don't use a .env file, replace os.getenv(...) with your actual key string
TD_API_KEY = os.getenv("TD_API_KEY", "YOUR_API_KEY_HERE")

# --- ASSETS ---
# All 8 pairs you requested. TwelveData uses standard format "Base/Quote"
WATCHLIST = [
    "EUR/USD",
    "GBP/JPY",
    "AUD/USD",
    "GBP/USD",
    "XAU/USD",  # Gold
    "AUD/CAD",
    "AUD/JPY",
    "BTC/USD"
]

TIMEFRAME = "1h"  # Using 1H for reliable intraday signals

# Initialize Bot
bot = Bot(token=TELEGRAM_BOT_TOKEN)
bot_stats = {
    "status": "running", 
    "last_run": "Waiting...", 
    "version": "V9.0 TwelveData Pro"
}

# =========================================================================
# === DATA ENGINE (TwelveData) ===
# =========================================================================

def fetch_data(symbol):
    """Fetches clean data from TwelveData API."""
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": TIMEFRAME,
        "apikey": TD_API_KEY,
        "outputsize": 100
    }
    
    try:
        response = requests.get(url, params=params)
        data = response.json()

        # Check for API errors (e.g., Rate Limit)
        if "code" in data and data["code"] == 429:
            print(f"⚠️ Rate Limit Hit for {symbol}. Skipping...")
            return pd.DataFrame()
        
        if "values" not in data:
            print(f"❌ API Error for {symbol}: {data.get('message', 'Unknown')}")
            return pd.DataFrame()

        # Convert to DataFrame
        df = pd.DataFrame(data["values"])
        df['datetime'] = pd.to_datetime(df['datetime'])
        df.set_index('datetime', inplace=True)
        
        # Sort: Oldest first (Crucial for indicators)
        df = df.iloc[::-1]
        
        # Convert strings to floats
        cols = ['open', 'high', 'low', 'close']
        df[cols] = df[cols].astype(float)
        
        return add_indicators(df)

    except Exception as e:
        print(f"❌ Connection Error {symbol}: {e}")
        return pd.DataFrame()

def calculate_cpr(df):
    """Calculates Pivot Points."""
    try:
        # Aggregate to Daily
        df_daily = df.resample('D').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
        if len(df_daily) < 2: return None
        
        prev_day = df_daily.iloc[-2]
        H, L, C = prev_day['high'], prev_day['low'], prev_day['close']
        PP = (H + L + C) / 3.0
        BC = (H + L) / 2.0
        TC = PP - BC + PP
        
        return {
            'PP': PP, 
            'R1': 2*PP - L, 'S1': 2*PP - H,
            'R2': PP + (H-L), 'S2': PP - (H-L)
        }
    except: return None

def add_indicators(df):
    """Adds EMA, RSI, MACD, ATR."""
    if df.empty: return df
    
    # 1. EMA Trend
    df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
    
    # 2. RSI (14)
    delta = df['close'].diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    rs = up.ewm(com=13, adjust=False).mean() / down.ewm(com=13, adjust=False).mean()
    df['rsi'] = 100 - (100 / (1 + rs))
    
    # 3. MACD
    exp1 = df['close'].ewm(span=12, adjust=False).mean()
    exp2 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = exp1 - exp2
    df['signal_line'] = df['macd'].ewm(span=9, adjust=False).mean()
    
    # 4. Bollinger Bands
    df['bb_upper'] = df['close'].rolling(20).mean() + (df['close'].rolling(20).std() * 2)
    df['bb_lower'] = df['close'].rolling(20).mean() - (df['close'].rolling(20).std() * 2)
    
    # 5. ATR (For Stop Loss)
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    df['atr'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(14).mean()
    
    return df.dropna()

# =========================================================================
# === ANALYSIS & TELEGRAM ===
# =========================================================================

async def send_signal(symbol, message):
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML')
        print(f"✅ Signal Sent for {symbol}")
    except Exception as e:
        print(f"⚠️ Telegram Error: {e}")

def run_analysis_cycle():
    """Iterates through all pairs sequentially to respect Rate Limits."""
    print(f"🔄 Starting Analysis Cycle: {datetime.now()}")
    bot_stats['last_run'] = datetime.now().isoformat()
    
    for symbol in WATCHLIST:
        try:
            print(f"🔍 Checking {symbol}...")
            
            # 1. Fetch
            df = fetch_data(symbol)
            if df.empty: 
                time.sleep(10) # Wait before next even if fail
                continue

            # 2. Logic
            cpr = calculate_cpr(df)
            if not cpr: continue

            last = df.iloc[-1]
            price = last['close']
            
            # --- SCORING SYSTEM ---
            score = 0
            # Trend
            if price > last['ema_200']: score += 1
            else: score -= 1
            
            # Momentum
            if last['macd'] > last['signal_line']: score += 1
            else: score -= 1
            
            # RSI
            rsi = last['rsi']
            if 50 < rsi < 70: score += 0.5
            elif rsi < 30: score += 0.5
            elif rsi > 70: score -= 0.5
            elif 30 < rsi < 50: score -= 0.5
            
            # CPR
            if price > cpr['PP']: score += 0.5
            else: score -= 0.5

            # --- DECISION ---
            signal, emoji = "WAIT", "⚖️"
            if score >= 2.5: signal, emoji = "STRONG BUY", "🚀"
            elif 1.0 <= score < 2.5: signal, emoji = "BUY", "🟢"
            elif -2.5 < score <= -1.0: signal, emoji = "SELL", "🔴"
            elif score <= -2.5: signal, emoji = "STRONG SELL", "🔻"

            # Formats
            is_buy = score > 0
            tp1 = cpr['R1'] if is_buy else cpr['S1']
            tp2 = cpr['R2'] if is_buy else cpr['S2']
            sl = price - (last['atr'] * 1.5) if is_buy else price + (last['atr'] * 1.5)
            
            fmt = ",.2f" if "JPY" in symbol or "XAU" in symbol else ",.4f"
            vol_status = "⚠️ High" if (price > last['bb_upper'] or price < last['bb_lower']) else "Normal"

            # Message
            msg = (
                f"⚡ <b>NILESH PRO SIGNAL</b>\n"
                f"<b>Asset:</b> {symbol}\n"
                f"<b>Price:</b> <code>{price:{fmt}}</code>\n"
                f"<b>Volatility:</b> {vol_status}\n\n"
                f"🚨 <b>{emoji} {signal}</b>\n\n"
                f"📈 <b>Trend:</b> {'Bullish' if price > last['ema_200'] else 'Bearish'}\n"
                f"📊 <b>RSI:</b> {rsi:.1f}\n"
                f"🎯 <b>TP1:</b> {tp1:{fmt}}\n"
                f"🎯 <b>TP2:</b> {tp2:{fmt}}\n"
                f"🛑 <b>SL:</b> {sl:{fmt}}"
            )
            
            # Send (Async wrapper)
            asyncio.run(send_signal(symbol, msg))
            
            # CRITICAL: Wait 10 seconds before next pair to respect API limit (8/min)
            print("⏳ Waiting 10s for API limit...")
            time.sleep(10)

        except Exception as e:
            print(f"❌ Cycle Error {symbol}: {e}")
            traceback.print_exc()

# =========================================================================
# === SCHEDULER & STARTUP ===
# =========================================================================

def startup_check():
    """Runs one full cycle immediately on startup."""
    print("🚀 Bot Started. Running immediate diagnostic cycle...")
    asyncio.run(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="🚀 <b>BOT ONLINE:</b> TwelveData Engine Active"))
    run_analysis_cycle()

def start_bot():
    scheduler = BackgroundScheduler()
    # Run the full cycle every 30 minutes
    scheduler.add_job(run_analysis_cycle, 'interval', minutes=30)
    scheduler.start()
    
    # Run startup in separate thread
    threading.Thread(target=startup_check).start()

start_bot()

app = Flask(__name__)
@app.route('/')
def home():
    return render_template_string("<h1>Nilesh Bot Active</h1><p>Status: Running V9</p>")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
