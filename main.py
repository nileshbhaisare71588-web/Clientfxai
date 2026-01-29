import os
import pandas as pd
import numpy as np
import asyncio
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Bot
from flask import Flask, jsonify, render_template_string
import threading
import time
import traceback
import yfinance as yf

# --- CONFIGURATION ---
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- ASSET MAPPING (User Friendly Name -> Yahoo Ticker) ---
# This ensures we get the EXACT data for the pairs you requested.
ASSET_MAP = {
    "EUR/USD": "EURUSD=X",
    "GBP/JPY": "GBPJPY=X",
    "AUD/USD": "AUDUSD=X",
    "GBP/USD": "GBPUSD=X",
    "XAU/USD": "GC=F",      # Gold Futures (Better data/volume than Spot)
    "AUD/CAD": "AUDCAD=X",
    "AUD/JPY": "AUDJPY=X",
    "BTC/USD": "BTC-USD"
}

# List of keys to iterate over
WATCHLIST = list(ASSET_MAP.keys())

TIMEFRAME_MAIN = "4h"  # Major Trend
TIMEFRAME_ENTRY = "1h" # Entry Precision

# Initialize Bot
bot = Bot(token=TELEGRAM_BOT_TOKEN)

bot_stats = {
    "status": "initializing",
    "total_analyses": 0,
    "last_analysis": None,
    "monitored_assets": WATCHLIST,
    "version": "V4.0 Unified Engine"
}

# =========================================================================
# === ADVANCED INDICATOR ENGINE ===
# =========================================================================

def calculate_cpr(df):
    """Calculates Pivot Points based on previous day's data."""
    try:
        # Resample to Daily to find yesterday's High/Low/Close
        # Yahoo gives us hourly data, so we aggregate it to finding 'Daily' candles
        df_daily = df.resample('D').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
        
        if len(df_daily) < 2: return None
        
        # Get yesterday's candle (iloc[-2] because -1 is 'today' which is incomplete)
        prev_day = df_daily.iloc[-2]
        
        H, L, C = prev_day['high'], prev_day['low'], prev_day['close']
        PP = (H + L + C) / 3.0
        BC = (H + L) / 2.0
        TC = PP - BC + PP
        
        return {
            'PP': PP, 'TC': TC, 'BC': BC,
            'R1': 2*PP - L, 'S1': 2*PP - H,
            'R2': PP + (H - L), 'S2': PP - (H - L)
        }
    except Exception as e:
        print(f"Error calculating CPR: {e}")
        return None

def add_indicators(df):
    """Adds RSI, MACD, BB, EMA, ATR."""
    if df.empty: return df
    
    # 1. EMAs
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
    df['bb_middle'] = df['close'].rolling(window=20).mean()
    df['bb_std'] = df['close'].rolling(window=20).std()
    df['bb_upper'] = df['bb_middle'] + (2 * df['bb_std'])
    df['bb_lower'] = df['bb_middle'] - (2 * df['bb_std'])

    # 5. ATR (Average True Range)
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    df['atr'] = np.max(ranges, axis=1).rolling(14).mean()

    return df.dropna()

def fetch_data(symbol_name):
    """Fetches data from Yahoo Finance using the mapped ticker."""
    ticker = ASSET_MAP.get(symbol_name)
    if not ticker: return pd.DataFrame()

    try:
        # Fetch 1 month of 1h data (covers both 1h and 4h analysis)
        # Using 1h interval is reliable on Yahoo
        df = yf.download(tickers=ticker, period="1mo", interval="1h", progress=False, multi_level_index=False)
        
        if df.empty:
            print(f"⚠️ No data for {symbol_name} ({ticker})")
            return pd.DataFrame()

        # Clean Columns (Yahoo sometimes returns MultiIndex, sometimes not)
        # We force lowercase for consistency
        df.columns = df.columns.str.lower()
        
        # Ensure required columns exist
        required = ['open', 'high', 'low', 'close']
        if not all(col in df.columns for col in required):
            print(f"⚠️ Missing columns for {symbol_name}")
            return pd.DataFrame()

        # Add Indicators
        df = add_indicators(df)
        
        return df

    except Exception as e:
        print(f"❌ Error fetching {symbol_name}: {e}")
        return pd.DataFrame()

def resample_to_4h(df_1h):
    """Aggregates 1H data into 4H candles."""
    try:
        agg_dict = {
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum' if 'volume' in df_1h.columns else 'first'
        }
        df_4h = df_1h.resample('4H').agg(agg_dict).dropna()
        # Re-calculate indicators on the 4H timeframe
        df_4h = add_indicators(df_4h)
        return df_4h
    except:
        return pd.DataFrame()

# =========================================================================
# === SIGNAL ANALYSIS ===
# =========================================================================

def analyze_market(symbol):
    global bot_stats
    try:
        # 1. Get Data (Base 1H)
        df_1h = fetch_data(symbol)
        if df_1h.empty: return

        # 2. Derive 4H Data
        df_4h = resample_to_4h(df_1h)
        if df_4h.empty: return

        # 3. Calculate CPR (using the daily aggregation of 1H data)
        cpr = calculate_cpr(df_1h)
        if not cpr: return

        # 4. Get Latest Candles
        last_1h = df_1h.iloc[-1]
        last_4h = df_4h.iloc[-1]
        price = last_1h['close']

        # --- SCORING LOGIC ---
        score = 0
        
        # Trend (4H) - Weight 1.0
        if price > last_4h['ema_200']: score += 1
        else: score -= 1
        
        # Momentum (MACD) - Weight 1.0
        if last_4h['macd'] > last_4h['signal_line']: score += 1
        else: score -= 1

        # RSI Filter - Weight 0.5
        rsi = last_4h['rsi']
        if 50 < rsi < 70: score += 0.5   # Healthy Bull
        elif 30 < rsi < 50: score -= 0.5 # Healthy Bear
        elif rsi > 70: score -= 0.5      # Overbought (Risk)
        elif rsi < 30: score += 0.5      # Oversold (Bounce opportunity)

        # Pivot Context - Weight 0.5
        if price > cpr['PP']: score += 0.5
        else: score -= 0.5

        # Volatility Check
        vol_status = "Normal"
        if price > last_4h['bb_upper']: vol_status = "⚠️ High (Overbought)"
        elif price < last_4h['bb_lower']: vol_status = "⚠️ High (Oversold)"

        # --- DECISION ---
        signal = "WAIT"
        emoji = "⚖️"
        
        if score >= 2.5:
            signal = "STRONG BUY"
            emoji = "🚀"
        elif 1.0 <= score < 2.5:
            signal = "BUY"
            emoji = "🟢"
        elif -2.5 < score <= -1.0:
            signal = "SELL"
            emoji = "🔴"
        elif score <= -2.5:
            signal = "STRONG SELL"
            emoji = "🔻"

        # --- MESSAGE FORMATTING ---
        # Targets
        is_buy = score > 0
        tp1 = cpr['R1'] if is_buy else cpr['S1']
        tp2 = cpr['R2'] if is_buy else cpr['S2']
        
        # Stop Loss (ATR Based)
        atr_buffer = last_4h['atr'] * 1.5
        sl = price - atr_buffer if is_buy else price + atr_buffer
        
        # Decimals: Gold/JPY need 2 decimals, others need 4 or 5
        fmt = ",.2f" if "JPY" in symbol or "XAU" in symbol else ",.4f"

        message = (
            f"╔════════════════════════════════╗\n"
            f"  🔥 <b>NILESH FX & GOLD SIGNALS</b>\n"
            f"╚════════════════════════════════╝\n\n"
            f"<b>Asset:</b> {symbol}\n"
            f"<b>Price:</b> <code>{price:{fmt}}</code>\n"
            f"<b>Volatility:</b> {vol_status}\n\n"
            f"--- 🚨 {emoji} <b>{signal}</b> 🚨 ---\n\n"
            f"<b>📊 ANALYSIS:</b>\n"
            f"• <b>Trend (EMA200):</b> {'UP 📈' if price > last_4h['ema_200'] else 'DOWN 📉'}\n"
            f"• <b>RSI (4h):</b> <code>{rsi:.1f}</code>\n"
            f"• <b>Momentum:</b> {'Bullish' if last_4h['macd'] > last_4h['signal_line'] else 'Bearish'}\n"
            f"• <b>Pivot:</b> {'Above' if price > cpr['PP'] else 'Below'} PP\n\n"
            f"<b>🎯 LEVELS:</b>\n"
            f"✅ <b>TP 1:</b> <code>{tp1:{fmt}}</code>\n"
            f"🔥 <b>TP 2:</b> <code>{tp2:{fmt}}</code>\n"
            f"🛑 <b>Stop Loss:</b> <code>{sl:{fmt}}</code>\n\n"
            f"----------------------------------------\n"
            f"<i>Powered by Nilesh System</i>"
        )

        # Send to Telegram
        asyncio.run(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML'))
        
        print(f"✅ Analyzed {symbol} -> {signal}")
        bot_stats['total_analyses'] += 1
        bot_stats['last_analysis'] = datetime.now().isoformat()

    except Exception as e:
        print(f"❌ Failed {symbol}: {e}")
        traceback.print_exc()

# =========================================================================
# === RUNNER ===
# =========================================================================

def start_bot():
    print(f"🚀 Starting Nilesh System {bot_stats['version']}...")
    scheduler = BackgroundScheduler()
    
    # Schedule every 30 minutes
    # Stagger execution to prevent network congestion
    for i, pair in enumerate(WATCHLIST):
        scheduler.add_job(analyze_market, 'cron', minute='0,30', second=i*5, args=[pair])
    
    scheduler.start()
    
    # Run Immediate Analysis
    for pair in WATCHLIST:
        threading.Thread(target=analyze_market, args=(pair,)).start()

start_bot()

app = Flask(__name__)

@app.route('/')
def home():
    return render_template_string("<h1>Nilesh Bot Active</h1><p>Status: OK</p>")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
