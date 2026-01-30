import os
import requests
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
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY")

# Assets to monitor (Forex, Metals, Crypto)
# Note: TwelveData format is usually just "EUR/USD" or "BTC/USD"
ASSETS = [
    "EUR/USD", "GBP/JPY", "AUD/USD", "GBP/USD",
    "XAU/USD", "AUD/CAD", "AUD/JPY", "BTC/USD"
]

TIMEFRAME_MAIN = "4h"  # Major Trend
TIMEFRAME_ENTRY = "1h" # Entry Precision

# Initialize Bot
bot = Bot(token=TELEGRAM_BOT_TOKEN)

bot_stats = {
    "status": "initializing",
    "total_analyses": 0,
    "last_analysis": None,
    "monitored_assets": ASSETS,
    "uptime_start": datetime.now().isoformat(),
    "version": "V3.0 Ultra-Quant (TwelveData)"
}

# =========================================================================
# === DATA ENGINE (TwelveData) ===
# =========================================================================

def fetch_data_twelvedata(symbol, interval):
    """
    Fetches data from TwelveData API.
    """
    base_url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": 50, # We need enough for indicators
        "apikey": TWELVEDATA_API_KEY
    }
    
    try:
        response = requests.get(base_url, params=params)
        data = response.json()
        
        if "values" not in data:
            print(f"⚠️ Error fetching {symbol}: {data.get('message', 'Unknown error')}")
            return pd.DataFrame()

        df = pd.DataFrame(data['values'])
        # Clean and convert types
        cols = ['open', 'high', 'low', 'close']
        for col in cols:
            df[col] = pd.to_numeric(df[col])
        
        df['datetime'] = pd.to_datetime(df['datetime'])
        df.set_index('datetime', inplace=True)
        
        # TwelveData returns newest first, we need oldest first for calculation
        df = df.sort_index(ascending=True)
        
        return df
    except Exception as e:
        print(f"❌ Connection error for {symbol}: {e}")
        return pd.DataFrame()

# =========================================================================
# === ADVANCED INDICATOR LIBRARY ===
# =========================================================================

def add_indicators(df):
    """Adds RSI, MACD, Bollinger Bands, and EMAs."""
    if df.empty: return df

    # 1. EMAs (Trend)
    df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()

    # 2. RSI (Momentum)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))

    # 3. MACD (Trend Strength)
    exp12 = df['close'].ewm(span=12, adjust=False).mean()
    exp26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = exp12 - exp26
    df['signal_line'] = df['macd'].ewm(span=9, adjust=False).mean()

    # 4. Bollinger Bands (Volatility)
    df['sma_20'] = df['close'].rolling(window=20).mean()
    df['std_dev'] = df['close'].rolling(window=20).std()
    df['bb_upper'] = df['sma_20'] + (df['std_dev'] * 2)
    df['bb_lower'] = df['sma_20'] - (df['std_dev'] * 2)
    
    # 5. ATR (Volatility for Stop Loss)
    df['tr1'] = df['high'] - df['low']
    df['tr2'] = abs(df['high'] - df['close'].shift(1))
    df['tr3'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
    df['atr'] = df['tr'].rolling(window=14).mean()

    return df.dropna()

def calculate_cpr_levels(df_daily):
    """Calculates Pivot Points (Standard + CPR)."""
    if df_daily.empty or len(df_daily) < 2: return None
    
    # Use the previous completed day
    prev_day = df_daily.iloc[-2]
    H, L, C = prev_day['high'], prev_day['low'], prev_day['close']
    
    PP = (H + L + C) / 3.0
    BC = (H + L) / 2.0
    TC = (PP - BC) + PP
    
    return {
        'PP': PP, 'TC': TC, 'BC': BC,
        'R1': (2 * PP) - L,
        'S1': (2 * PP) - H,
        'R2': PP + (H - L),
        'S2': PP - (H - L)
    }

# =========================================================================
# === SIGNAL GENERATION ENGINE ===
# =========================================================================

def analyze_market(symbol):
    global bot_stats
    try:
        # Fetch Data
        df_4h = fetch_data_twelvedata(symbol, TIMEFRAME_MAIN)
        df_1h = fetch_data_twelvedata(symbol, TIMEFRAME_ENTRY)
        
        # We need Daily data for CPR
        df_d = fetch_data_twelvedata(symbol, "1day")

        if df_4h.empty or df_1h.empty or df_d.empty: return

        # Apply Indicators
        df_4h = add_indicators(df_4h)
        df_1h = add_indicators(df_1h)
        cpr = calculate_cpr_levels(df_d)

        if df_1h.empty: return

        # Current Candle Data (1H)
        curr = df_1h.iloc[-1]
        price = curr['close']
        
        # --- LOGIC SCORING SYSTEM ---
        score = 0
        reasons = []

        # 1. Trend (EMA 50 vs 200)
        if curr['ema_50'] > curr['ema_200']:
            score += 1
            trend_status = "BULLISH"
        else:
            score -= 1
            trend_status = "BEARISH"

        # 2. RSI Filter
        if curr['rsi'] > 55: score += 1
        elif curr['rsi'] < 45: score -= 1
        
        # 3. MACD
        if curr['macd'] > curr['signal_line']: score += 1
        else: score -= 1

        # 4. CPR Logic
        if price > cpr['TC']: score += 1
        elif price < cpr['BC']: score -= 1

        # --- SIGNAL DECISION ---
        signal = "NEUTRAL"
        emoji = "⚖️"
        
        if score >= 3:
            signal = "STRONG BUY"
            emoji = "🟢"
        elif score <= -3:
            signal = "STRONG SELL"
            emoji = "🔴"
        elif score == 1 or score == 2:
            signal = "WEAK BUY (Wait)"
            emoji = "⚠️"
        elif score == -1 or score == -2:
            signal = "WEAK SELL (Wait)"
            emoji = "⚠️"

        # Volatility Check
        bb_gap = ((curr['bb_upper'] - curr['bb_lower']) / curr['sma_20']) * 100
        volatility = "HIGH" if bb_gap > 1.0 else "NORMAL"

        # Targets (ATR Based)
        atr = curr['atr']
        if "BUY" in signal:
            tp = price + (atr * 2)
            sl = price - (atr * 1.5)
        else:
            tp = price - (atr * 2)
            sl = price + (atr * 1.5)

        # Only send Strong signals to reduce spam
        if "STRONG" in signal:
            message = (
                f"🚨 <b>MARKET ALERT: {symbol}</b>\n"
                f"═══════════════════════\n"
                f"<b>SIGNAL:</b> {emoji} <b>{signal}</b>\n"
                f"<b>Price:</b> <code>{price:,.4f}</code>\n\n"
                f"📊 <b>TECHNICALS:</b>\n"
                f"• Trend: <b>{trend_status}</b>\n"
                f"• RSI: <code>{curr['rsi']:.1f}</code>\n"
                f"• MACD: {'Bullish' if curr['macd'] > curr['signal_line'] else 'Bearish'}\n"
                f"• Volatility: {volatility}\n\n"
                f"🎯 <b>LEVELS:</b>\n"
                f"TP (Target): <code>{tp:,.4f}</code>\n"
                f"SL (Stop): <code>{sl:,.4f}</code>\n"
                f"Pivot: <code>{cpr['PP']:,.4f}</code>\n\n"
                f"<i>Powered by Nilesh's Algo V3</i>"
            )
            asyncio.run(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML'))
            
            bot_stats['total_analyses'] += 1
            bot_stats['last_analysis'] = datetime.now().isoformat()
            bot_stats['status'] = "operational"

    except Exception as e:
        print(f"❌ Analysis failed for {symbol}: {e}")
        traceback.print_exc()

# =========================================================================
# === RUNNER ===
# =========================================================================

def start_bot():
    print(f"🚀 Initializing {bot_stats['version']}...")
    scheduler = BackgroundScheduler()
    
    # Check every 15 minutes
    for s in ASSETS:
        scheduler.add_job(analyze_market, 'interval', minutes=15, args=[s])
    
    scheduler.start()
    
    # Initial run
    for s in ASSETS:
        threading.Thread(target=analyze_market, args=(s,)).start()

import traceback 
start_bot()

app = Flask(__name__)

@app.route('/')
def home():
    return render_template_string("""
        <body style="font-family:sans-serif; background:#0f172a; color:#f8fafc; text-align:center; padding-top:100px;">
            <div style="background:#1e293b; display:inline-block; padding:40px; border-radius:15px; border: 1px solid #334155;">
                <h1 style="color:#22d3ee;">Nilesh Algo Dashboard</h1>
                <p style="font-size:1.2em;">Status: <span style="color:#4ade80;">Active</span></p>
                <p>Signals Sent: <b>{{a}}</b></p>
                <p>Version: <i>{{v}}</i></p>
            </div>
        </body>
    """, a=bot_stats['total_analyses'], v=bot_stats['version'])

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
