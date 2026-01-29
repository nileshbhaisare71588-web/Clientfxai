import os
import requests
import pandas as pd
import numpy as np
import asyncio
import time
import traceback
import threading
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from flask import Flask, render_template_string

# --- CONFIGURATION ---
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TD_API_KEY = os.getenv("TD_API_KEY", "YOUR_API_KEY_HERE")

# --- ASSETS ---
WATCHLIST = [
    "EUR/USD", "GBP/JPY", "AUD/USD", "GBP/USD",
    "XAU/USD", "AUD/CAD", "AUD/JPY", "BTC/USD"
]

TIMEFRAME = "1h"

# Global dictionary to store the latest status of every pair
market_status = {} 

# =========================================================================
# === DATA ENGINE ===
# =========================================================================

def fetch_data(symbol):
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": TIMEFRAME, "apikey": TD_API_KEY, "outputsize": 60}
    try:
        response = requests.get(url, params=params)
        data = response.json()
        if "values" not in data: return pd.DataFrame()
        df = pd.DataFrame(data["values"])
        df['datetime'] = pd.to_datetime(df['datetime'])
        df.set_index('datetime', inplace=True)
        df = df.iloc[::-1] # Oldest first
        df[['open', 'high', 'low', 'close']] = df[['open', 'high', 'low', 'close']].astype(float)
        return add_indicators(df)
    except: return pd.DataFrame()

def calculate_cpr(df):
    try:
        df_daily = df.resample('D').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
        if len(df_daily) < 2: return None
        prev = df_daily.iloc[-2]
        PP = (prev['high'] + prev['low'] + prev['close']) / 3.0
        return {'PP': PP, 'R1': 2*PP - prev['low'], 'S1': 2*PP - prev['high']}
    except: return None

def add_indicators(df):
    if df.empty: return df
    df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
    delta = df['close'].diff()
    up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
    rs = up.ewm(com=13, adjust=False).mean() / down.ewm(com=13, adjust=False).mean()
    df['rsi'] = 100 - (100 / (1 + rs))
    exp1 = df['close'].ewm(span=12, adjust=False).mean()
    exp2 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = exp1 - exp2
    df['signal_line'] = df['macd'].ewm(span=9, adjust=False).mean()
    
    # ATR for Stop Loss
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    df['atr'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(14).mean()
    
    return df.dropna()

# =========================================================================
# === PROFESSIONAL MESSAGE FORMATTER ===
# =========================================================================

def format_signal_message(symbol, signal, price, rsi, trend, tp1, tp2, sl):
    """Creates a beautiful, high-end signal card for Telegram."""
    
    # Emoji & Color Logic
    if "STRONG BUY" in signal:
        header = "💎 <b>PREMIUM BUY SIGNAL</b>"
        action = f"🚀 <b>LONG {symbol}</b>"
        color_line = "🟢🟢🟢🟢🟢🟢🟢🟢"
    elif "STRONG SELL" in signal:
        header = "💎 <b>PREMIUM SELL SIGNAL</b>"
        action = f"🔻 <b>SHORT {symbol}</b>"
        color_line = "🔴🔴🔴🔴🔴🔴🔴🔴"
    else:
        return None # We don't send weak signals automatically

    fmt = ",.2f" if "JPY" in symbol or "XAU" in symbol else ",.4f"

    msg = (
        f"{header}\n"
        f"{color_line}\n\n"
        f"{action}\n"
        f"💵 <b>Price:</b> <code>{price:{fmt}}</code>\n\n"
        f"<b>📊 TECHNICALS</b>\n"
        f"• Trend: <b>{trend}</b>\n"
        f"• RSI: <code>{rsi:.1f}</code>\n"
        f"• Momentum: {'Bullish' if 'BUY' in signal else 'Bearish'}\n\n"
        f"<b>🎯 TRADE TARGETS</b>\n"
        f"✅ <b>TP 1:</b> <code>{tp1:{fmt}}</code>\n"
        f"🚀 <b>TP 2:</b> <code>{tp2:{fmt}}</code>\n"
        f"🛡️ <b>Stop Loss:</b> <code>{sl:{fmt}}</code>\n\n"
        f"{color_line}\n"
        f"<i>Nilesh Quant System V11</i>"
    )
    return msg

# =========================================================================
# === ANALYSIS ENGINE ===
# =========================================================================

async def run_analysis_cycle(context: ContextTypes.DEFAULT_TYPE = None):
    """Scans all pairs. Sends Alerts for STRONG signals. Saves status for /report."""
    global market_status
    print(f"🔄 Scanning Markets... {datetime.now()}")
    
    # Initialize the bot object if running from scheduler
    bot_sender = Bot(token=TELEGRAM_BOT_TOKEN) if context is None else context.bot

    for symbol in WATCHLIST:
        try:
            df = fetch_data(symbol)
            if df.empty:
                time.sleep(8) 
                continue

            cpr = calculate_cpr(df)
            last = df.iloc[-1]
            price = last['close']
            
            # Logic
            score = 0
            if price > last['ema_200']: score += 1
            else: score -= 1
            if last['macd'] > last['signal_line']: score += 1
            else: score -= 1
            
            rsi = last['rsi']
            if 50 < rsi < 70: score += 0.5
            elif rsi < 30: score += 0.5
            elif rsi > 70: score -= 0.5
            elif 30 < rsi < 50: score -= 0.5
            
            if cpr and price > cpr['PP']: score += 0.5
            else: score -= 0.5

            # Determine Signal
            signal = "WAIT (Neutral)"
            if score >= 2.5: signal = "STRONG BUY"
            elif 1.0 <= score < 2.5: signal = "BUY"
            elif -2.5 < score <= -1.0: signal = "SELL"
            elif score <= -2.5: signal = "STRONG SELL"

            # Targets
            tp1 = cpr['R1'] if score > 0 else cpr['S1']
            tp2 = cpr['R2'] if score > 0 else cpr['S2']
            sl = price - (last['atr'] * 1.5) if score > 0 else price + (last['atr'] * 1.5)
            trend = "Bullish 📈" if price > last['ema_200'] else "Bearish 📉"

            # 1. SAVE STATUS (For /report command)
            market_status[symbol] = f"{signal} | RSI: {rsi:.0f}"

            # 2. SEND ALERT (Only if STRONG)
            msg = format_signal_message(symbol, signal, price, rsi, trend, tp1, tp2, sl)
            if msg:
                print(f"🚨 Sending Signal for {symbol}")
                await bot_sender.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='HTML')

            time.sleep(8) # Wait to avoid API ban

        except Exception as e:
            print(f"❌ Error {symbol}: {e}")

# =========================================================================
# === TELEGRAM COMMAND HANDLERS ===
# =========================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 <b>Nilesh Bot Online!</b>\nType /report to see all pairs.", parse_mode='HTML')

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a summary of ALL pairs when user types /report"""
    if not market_status:
        await update.message.reply_text("⏳ collecting data... try again in 1 minute.")
        return

    msg = "📊 <b>LIVE MARKET REPORT</b>\n\n"
    for sym, status in market_status.items():
        icon = "⚪"
        if "STRONG BUY" in status: icon = "🟢"
        elif "STRONG SELL" in status: icon = "🔴"
        elif "BUY" in status: icon = "🔹"
        elif "SELL" in status: icon = "🔸"
        
        msg += f"{icon} <b>{sym}:</b> {status}\n"
    
    msg += "\n<i>Updated just now.</i>"
    await update.message.reply_text(msg, parse_mode='HTML')

# =========================================================================
# === MAIN EXECUTION ===
# =========================================================================

app_flask = Flask(__name__)

@app_flask.route('/')
def home():
    return "Nilesh Bot V11 - Telegram Interface Active"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host='0.0.0.0', port=port)

def main():
    # 1. Start Web Server (Background)
    threading.Thread(target=run_flask).start()
    
    # 2. Setup Telegram Application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # 3. Add Commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("report", report_command))
    
    # 4. Setup Scheduler for Auto-Analysis
    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: asyncio.run(run_analysis_cycle()), 'interval', minutes=30)
    scheduler.start()
    
    # 5. Send Startup Message
    asyncio.get_event_loop().run_until_complete(
        application.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, 
            text="🚀 <b>SYSTEM RESTARTED</b>\n\nType <code>/report</code> to check all pairs immediately.", 
            parse_mode='HTML'
        )
    )

    # 6. Run Bot
    print("🚀 Bot Polling Started...")
    application.run_polling()

if __name__ == '__main__':
    main()
