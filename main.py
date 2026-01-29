import os
import pandas as pd
import numpy as np
import asyncio
import time
import random
import traceback
import yfinance as yf
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

# --- STRICT ASSET MAP (Yahoo Tickers) ---
# We use 'GC=F' for Gold because it has better volume data than 'XAUUSD=X'
ASSET_MAP = {
    "GBP/JPY": "GBPJPY=X",
    "AUD/CAD": "AUDCAD=X",
    "XAU/USD": "GC=F"
}

WATCHLIST = list(ASSET_MAP.keys())
TIMEFRAME_MAIN = "4h"

# Initialize Bot
bot = Bot(token=TELEGRAM_BOT_TOKEN)

bot_stats = {
    "status": "initializing",
    "total_analyses": 0,
    "last_analysis": None,
    "monitored_assets": WATCHLIST,
    "version": "V6.0 Stealth Mode"
}

# =========================================================================
# === INDICATOR ENGINE ===
# =========================================================================

def calculate_cpr(df):
    try:
        # Aggregate hourly data into Daily candles for CPR
        df_daily = df.resample('D').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
        if len(df_daily) < 2: return None
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
    except: return None

def add_indicators(df):
    if df.empty: return df
    # EMAs
    df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
    # RSI
    delta = df['close'].diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    rs = up.ewm(com=13, adjust=False).mean() / down.ewm(com=13, adjust=False).mean()
    df['rsi'] = 100 - (100 / (1 + rs))
    # MACD
    exp1 = df['close'].ewm(span=12, adjust=False).mean()
    exp2 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = exp1 - exp2
    df['signal_line'] = df['macd'].ewm(span=9, adjust=False).mean()
    # BB
    df['bb_middle'] = df['close'].rolling(window=20).mean()
    df['bb_std'] = df['close'].rolling(window=20).std()
    df['bb_upper'] = df['bb_middle'] + (2 * df['bb_std'])
    df['bb_lower'] = df['bb_middle'] - (2 * df['bb_std'])
    # ATR
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    df['atr'] = np.max(ranges, axis=1).rolling(14).mean()
    return df.dropna()

def fetch_data_stealth(symbol_name):
    """Fetches data with heavy stealth delays to avoid Rate Limits."""
    ticker = ASSET_MAP.get(symbol_name)
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            # random sleep 2-5s before every single request
            time.sleep(random.uniform(2, 5))
            
            df = yf.download(tickers=ticker, period="1mo", interval="1h", progress=False, multi_level_index=False)
            
            if df.empty: 
                print(f"⚠️ Empty data for {symbol_name}")
                return pd.DataFrame()

            df.columns = df.columns.str.lower()
            df = add_indicators(df)
            return df

        except Exception as e:
            print(f"⚠️ Fetch error {symbol_name}: {e}")
            # If error, wait 30 seconds before retry
            time.sleep(30)
            
    return pd.DataFrame()

def resample_to_4h(df_1h):
    try:
        agg_dict = {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}
        df_4h = df_1h.resample('4H').agg(agg_dict).dropna()
        return add_indicators(df_4h)
    except: return pd.DataFrame()

# =========================================================================
# === ANALYZER ===
# =========================================================================

def analyze_market(symbol):
    global bot_stats
    try:
        print(f"🔍 Analyzing {symbol}...")
        df_1h = fetch_data_stealth(symbol)
        if df_1h.empty: return

        df_4h = resample_to_4h(df_1h)
        cpr = calculate_cpr(df_1h)
        
        if df_4h.empty or not cpr: return

        last_1h = df_1h.iloc[-1]
        last_4h = df_4h.iloc[-1]
        price = last_1h['close']

        # --- SCORING ---
        score = 0
        # Trend
        if price > last_4h['ema_200']: score += 1
        else: score -= 1
        
        # MACD
        if last_4h['macd'] > last_4h['signal_line']: score += 1
        else: score -= 1

        # RSI
        rsi = last_4h['rsi']
        if 50 < rsi < 70: score += 0.5
        elif 30 < rsi < 50: score -= 0.5
        elif rsi > 70: score -= 0.5
        elif rsi < 30: score += 0.5

        # CPR
        if price > cpr['PP']: score += 0.5
        else: score -= 0.5

        vol_status = "Normal"
        if price > last_4h['bb_upper']: vol_status = "⚠️ High (Overbought)"
        elif price < last_4h['bb_lower']: vol_status = "⚠️ High (Oversold)"

        # --- SIGNAL DECISION ---
        signal = "WAIT"
        emoji = "⚖️"
        if score >= 2.5: signal, emoji = "STRONG BUY", "🚀"
        elif 1.0 <= score < 2.5: signal, emoji = "BUY", "🟢"
        elif -2.5 < score <= -1.0: signal, emoji = "SELL", "🔴"
        elif score <= -2.5: signal, emoji = "STRONG SELL", "🔻"

        # Targets
        is_buy = score > 0
        tp1 = cpr['R1'] if is_buy else cpr['S1']
        tp2 = cpr['R2'] if is_buy else cpr['S2']
        atr_sl = last_4h['atr'] * 1.5
        sl = price - atr_sl if is_buy else price + atr_sl
        
        fmt = ",.2f" if "JPY" in symbol or "XAU" in symbol else ",.4f"

        message = (
            f"╔════════════════════════════════╗\n"
            f"  🔥 <b>NILESH FX & GOLD AI</b>\n"
            f"╚════════════════════════════════╝\n\n"
            f"<b>Asset:</b> {symbol}\n"
            f"<b>Price:</b> <code>{price:{fmt}}</code>\n"
            f"<b>Volatility:</b> {vol_status}\n\n"
            f"--- 🚨 {emoji} <b>{signal}</b> 🚨 ---\n\n"
            f"<b>📊 ANALYSIS:</b>\n"
            f"• <b>Trend:</b> {'UP 📈' if price > last_4h['ema_200'] else 'DOWN 📉'}\n"
            f"• <b>RSI:</b> <code>{rsi:.1f}</code>\n"
            f"• <b>MACD:</b> {'Bullish' if last_4h['macd'] > last_4h['signal_line'] else 'Bearish'}\n"
            f"• <b>Pivot:</b> {'Above' if price > cpr['PP'] else 'Below'} PP\n\n"
            f"<b>🎯 LEVELS:</b>\n"
            f"✅ <b>TP 1:</b> <code>{tp1:{fmt}}</code>\n"
            f"🔥 <b>TP 2:</b> <code>{tp2:{fmt}}</code>\n"
            f"🛑 <b>Stop Loss:</b> <code>{sl:{fmt}}</code>\n\n"
            f"----------------------------------------\n"
            f"<i>Powered by Nilesh System</i>"
        )

        asyncio.run(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML'))
        print(f"✅ Signal sent for {symbol}")
        bot_stats['total_analyses'] += 1
        bot_stats['last_analysis'] = datetime.now().isoformat()

    except Exception as e:
        print(f"❌ Error {symbol}: {e}")
        traceback.print_exc()

# =========================================================================
# === STEALTH SCHEDULER ===
# =========================================================================

def initial_startup_check():
    """Sequential check with LONG delays to prevent Ban."""
    print("⏳ Starting initial checks (Stealth Mode)...")
    for symbol in WATCHLIST:
        analyze_market(symbol)
        # CRITICAL: 20 second wait between pairs
        print("zzz Sleeping 20s...") 
        time.sleep(20) 
    print("✅ Initial checks complete.")

def start_bot():
    print(f"🚀 Initializing {bot_stats['version']}...")
    scheduler = BackgroundScheduler()
    
    # Schedule updates apart from each other
    # GBP/JPY at :00, AUD/CAD at :15, XAU/USD at :30
    scheduler.add_job(analyze_market, 'cron', minute='0,30', args=["GBP/JPY"])
    scheduler.add_job(analyze_market, 'cron', minute='15,45', args=["AUD/CAD"])
    scheduler.add_job(analyze_market, 'cron', minute='10,40', args=["XAU/USD"])
    
    scheduler.start()
    
    threading.Thread(target=initial_startup_check).start()

start_bot()

app = Flask(__name__)

@app.route('/')
def home():
    return render_template_string(f"<h1>Nilesh Bot Active</h1><p>Monitoring: {', '.join(WATCHLIST)}</p>")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
