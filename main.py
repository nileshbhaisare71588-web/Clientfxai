import os
import ccxt
import pandas as pd
import numpy as np
import asyncio
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Bot
from flask import Flask, jsonify, render_template_string
import threading
import time
import traceback
import yfinance as yf  # NEW: Backup for Forex pairs Kraken doesn't have

# --- CONFIGURATION ---
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# UPDATED: FIXED LIST FOR SPECIFIC PAIRS ONLY
# Kraken doesn't have AUD/CAD or GBP/JPY, so the bot will now use the Yahoo fallback for them.
CRYPTOS = ["GBP/JPY", "XAU/USD", "AUD/CAD"]

TIMEFRAME_MAIN = "4h"  # Major Trend
TIMEFRAME_ENTRY = "1h" # Entry Precision

# Initialize Bot and Exchange
bot = Bot(token=TELEGRAM_BOT_TOKEN)
exchange = ccxt.kraken({'enableRateLimit': True})

bot_stats = {
    "status": "initializing",
    "total_analyses": 0,
    "last_analysis": None,
    "monitored_assets": CRYPTOS,
    "uptime_start": datetime.now().isoformat(),
    "version": "V3.1 Forex Fix"
}

# =========================================================================
# === DATA ENGINE (KRAKEN + YAHOO FALLBACK) ===
# =========================================================================

def calculate_cpr_levels(df_daily):
    """Calculates Daily Pivot Points."""
    if df_daily.empty or len(df_daily) < 2: return None
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

def add_technical_indicators(df):
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

    # 5. ATR
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    df['atr'] = np.max(ranges, axis=1).rolling(14).mean()

    return df

def fetch_yfinance_data(symbol, timeframe):
    """Fallback to Yahoo Finance for pairs Kraken doesn't have."""
    try:
        # Map Common Names to Yahoo Tickers
        # GBP/JPY -> GBPJPY=X
        yf_symbol = symbol.replace("/", "") + "=X"
        if "XAU" in symbol: yf_symbol = "GC=F" # Gold Futures

        # Fetch data
        period = "1mo"
        interval = "1h"
        
        # We always fetch 1h first, then resample if needed
        df = yf.download(tickers=yf_symbol, period=period, interval=interval, progress=False)
        
        if df.empty: return pd.DataFrame()

        # Flatten MultiIndex columns if present (Fix for recent yfinance update)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'})
        df.index.name = 'timestamp'

        # Resample for 4H or Daily
        if timeframe == "4h":
            df = df.resample('4h').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'})
        elif timeframe == "1d":
            df = df.resample('1d').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'})

        return df.dropna()
    except Exception as e:
        print(f"⚠️ Yahoo Fetch Error for {symbol}: {e}")
        return pd.DataFrame()

def fetch_data_safe(symbol, timeframe):
    """Tries Kraken first, then switches to Yahoo Finance if needed."""
    # 1. Try Kraken
    try:
        if not exchange.markets: exchange.load_markets()
        # Search for symbol in Kraken markets
        found_id = None
        for key in exchange.markets.keys():
            if symbol in key:
                found_id = exchange.markets[key]['id']
                break
        
        if found_id:
            ohlcv = exchange.fetch_ohlcv(found_id, timeframe, limit=300)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            df = add_technical_indicators(df)
            return df.dropna()
    except:
        pass # Kraken failed, proceed to fallback

    # 2. Fallback to Yahoo Finance (For AUD/CAD, GBP/JPY etc)
    print(f"🔄 Switching to Backup Data (Yahoo) for {symbol}...")
    df = fetch_yfinance_data(symbol, timeframe)
    df = add_technical_indicators(df)
    return df

# =========================================================================
# === SIGNAL LOGIC ===
# =========================================================================

def determine_signal_strength(row_4h, row_1h, cpr_data):
    score = 0
    price = row_4h['close']
    
    # Trend (4H)
    if price > row_4h['ema_200']: score += 1
    else: score -= 1
    
    if row_4h['ema_50'] > row_4h['ema_200']: score += 1
    else: score -= 1

    # Momentum
    rsi = row_4h['rsi']
    if 50 < rsi < 70: score += 0.5 
    elif rsi < 30: score += 0.5
    elif rsi > 70: score -= 0.5
    elif 30 < rsi < 50: score -= 0.5

    if row_4h['macd'] > row_4h['signal_line']: score += 1
    else: score -= 1

    # Price Action
    if price > cpr_data['PP']: score += 0.5
    else: score -= 0.5

    # Volatility Status
    vol_status = "Normal"
    if price > row_4h['bb_upper']: vol_status = "High (Overbought)"
    elif price < row_4h['bb_lower']: vol_status = "High (Oversold)"
    
    if score >= 3: return "STRONG BUY", "🚀", score, vol_status
    elif 1 <= score < 3: return "BUY", "🟢", score, vol_status
    elif -3 < score <= -1: return "SELL", "🔴", score, vol_status
    elif score <= -3: return "STRONG SELL", "🔻", score, vol_status
    else: return "NEUTRAL", "⚖️", score, vol_status

def generate_and_send_signal(symbol):
    global bot_stats
    try:
        # Fetch Data
        df_4h = fetch_data_safe(symbol, TIMEFRAME_MAIN)
        df_1h = fetch_data_safe(symbol, TIMEFRAME_ENTRY)
        
        # Fetch Daily for CPR
        # For CPR we try to get daily data from the same source
        df_d = fetch_data_safe(symbol, "1d") 
        cpr = calculate_cpr_levels(df_d)

        if df_4h.empty or df_1h.empty or cpr is None: 
            print(f"⚠️ No data found for {symbol}")
            return

        last_4h = df_4h.iloc[-1]
        last_1h = df_1h.iloc[-1]
        price = last_4h['close']
        
        signal, emoji, score, vol_status = determine_signal_strength(last_4h, last_1h, cpr)
        
        # Targets
        is_buy = score > 0
        tp1 = cpr['R1'] if is_buy else cpr['S1']
        tp2 = cpr['R2'] if is_buy else cpr['S2']
        atr_sl = last_4h['atr'] * 1.5
        sl = price - atr_sl if is_buy else price + atr_sl
        
        fmt = ",.2f"

        message = (
            f"╔════════════════════════════════╗\n"
            f"  🏆 <b>NILESH FOREX & GOLD AI</b>\n"
            f"╚════════════════════════════════╝\n\n"
            f"<b>Asset:</b> {symbol}\n"
            f"<b>Price:</b> <code>{price:{fmt}}</code>\n"
            f"<b>Volatility:</b> {vol_status}\n\n"
            f"--- 🚨 {emoji} <b>{signal}</b> 🚨 ---\n\n"
            f"<b>📊 METRICS:</b>\n"
            f"• <b>Trend (200 EMA):</b> {'BULLISH' if price > last_4h['ema_200'] else 'BEARISH'}\n"
            f"• <b>RSI (4h):</b> <code>{last_4h['rsi']:.1f}</code>\n"
            f"• <b>MACD:</b> {'Bullish' if last_4h['macd'] > last_4h['signal_line'] else 'Bearish'}\n"
            f"• <b>Pivot:</b> {'Above' if price > cpr['PP'] else 'Below'} PP\n\n"
            f"<b>🎯 LEVELS:</b>\n"
            f"✅ <b>TP 1:</b> <code>{tp1:{fmt}}</code>\n"
            f"🔥 <b>TP 2:</b> <code>{tp2:{fmt}}</code>\n"
            f"🛑 <b>SL:</b> <code>{sl:{fmt}}</code>\n\n"
            f"----------------------------------------\n"
            f"<i>Powered by Nilesh System</i>"
        )

        asyncio.run(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML'))
        bot_stats['total_analyses'] += 1
        bot_stats['last_analysis'] = datetime.now().isoformat()
        print(f"✅ Signal sent for {symbol}")

    except Exception as e:
        print(f"❌ Error {symbol}: {e}")
        traceback.print_exc()

# =========================================================================
# === STARTUP ===
# =========================================================================

def start_bot():
    print(f"🚀 Initializing {bot_stats['version']}...")
    scheduler = BackgroundScheduler()
    
    for idx, s in enumerate(CRYPTOS):
        # Run every 30 mins
        scheduler.add_job(generate_and_send_signal, 'cron', minute='0,30', second=idx*5, args=[s])
    
    scheduler.start()
    
    # Immediate check
    for s in CRYPTOS:
        threading.Thread(target=generate_and_send_signal, args=(s,)).start()

start_bot()

app = Flask(__name__)

@app.route('/')
def home():
    return render_template_string("<h1>Nilesh Bot Running</h1><p>Active</p>")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
