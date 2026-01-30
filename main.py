import os
import requests
import pandas as pd
import numpy as np
import asyncio
import time
import threading
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Bot
from telegram.ext import Application, CommandHandler
from telegram.error import Conflict
from flask import Flask

# --- CONFIGURATION ---
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TD_API_KEY = os.getenv("TD_API_KEY")

WATCHLIST = [
    "EUR/USD", "GBP/JPY", "AUD/USD", "GBP/USD",
    "XAU/USD", "AUD/CAD", "AUD/JPY", "BTC/USD"
]
TIMEFRAME = "1h"

app = Flask(__name__)
@app.route('/')
def home(): return "Nilesh Bot V25 (Ultra-Premium Design)"

# =========================================================================
# === DATA ENGINE ===
# =========================================================================

def fetch_data(symbol):
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": TIMEFRAME, "apikey": TD_API_KEY, "outputsize": 60}
    try:
        response = requests.get(url, params=params)
        data = response.json()
        
        if "code" in data and data["code"] == 429: return "RATE_LIMIT"
        if "values" not in data: return "NO_DATA"
            
        df = pd.DataFrame(data["values"])
        df['datetime'] = pd.to_datetime(df['datetime'])
        df.set_index('datetime', inplace=True)
        df = df.iloc[::-1]
        df[['open', 'high', 'low', 'close']] = df[['open', 'high', 'low', 'close']].astype(float)
        
        df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
        delta = df['close'].diff()
        up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
        rs = up.ewm(com=13, adjust=False).mean() / down.ewm(com=13, adjust=False).mean()
        df['rsi'] = 100 - (100 / (1 + rs))
        exp1 = df['close'].ewm(span=12, adjust=False).mean()
        exp2 = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = exp1 - exp2
        df['signal_line'] = df['macd'].ewm(span=9, adjust=False).mean()
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        df['atr'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(14).mean()
        return df.dropna()
    except Exception as e: return f"ERROR: {str(e)}"

def calculate_cpr(df):
    try:
        df_daily = df.resample('D').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
        if len(df_daily) < 2: return None
        prev = df_daily.iloc[-2]
        H, L, C = prev['high'], prev['low'], prev['close']
        PP = (H + L + C) / 3.0
        R1, S1 = (2 * PP) - L, (2 * PP) - H
        R2, S2 = PP + (H - L), PP - (H - L) # Fixed Math
        return {'PP': PP, 'R1': R1, 'S1': S1, 'R2': R2, 'S2': S2}
    except: return None

# =========================================================================
# === V25 ULTRA-PREMIUM DESIGNER ===
# =========================================================================

def get_flags(symbol):
    """Adds currency flags for visual appeal."""
    base, quote = symbol.split('/')
    flags = {
        "EUR": "🇪🇺", "USD": "🇺🇸", "GBP": "🇬🇧", "JPY": "🇯🇵",
        "AUD": "🇦🇺", "CAD": "🇨🇦", "XAU": "🥇", "BTC": "🅱️"
    }
    return f"{flags.get(base, '')}{flags.get(quote, '')}"

def format_premium_card(symbol, signal, price, rsi, trend, tp1, tp2, sl):
    # 1. Theme & Header Setup
    if "STRONG BUY" in signal:
        header = "🔴💎 <b>INSTITUTIONAL BUY DETECTED</b> 💎🔴"
        side, theme_color = "LONG 🟢", "🟢"
        bar, urgency = "🟩🟩🟩🟩🟩 MAXIMUM", "(💎 Oversold bounce)" if rsi < 30 else ""
    elif "STRONG SELL" in signal:
        header = "🔴💎 <b>INSTITUTIONAL SELL DETECTED</b> 💎🔴"
        side, theme_color = "SHORT 🔴", "🔴"
        bar, urgency = "🟥🟥🟥🟥🟥 MAXIMUM", "(💎 Overbought rejection)" if rsi > 70 else ""
    elif "BUY" in signal:
        header = "🟢 <b>BUY SIGNAL GENERATED</b> 🟢"
        side, theme_color = "LONG 🟢", "🟢"
        bar, urgency = "🟩🟩🟩⬜⬜", ""
    elif "SELL" in signal:
        header = "🔴 <b>SELL SIGNAL GENERATED</b> 🔴"
        side, theme_color = "SHORT 🔴", "🔴"
        bar, urgency = "🟥🟥🟥⬜⬜", ""
    else:
        header = "⚖️ <b>MARKET NEUTRAL</b> ⚖️"
        side, theme_color = "WAIT ⚪", "⚪"
        bar, urgency = "⬜⬜⬜⬜⬜", ""

    # 2. Formatting & Icons
    fmt = ",.2f" if "JPY" in symbol or "XAU" in symbol or "BTC" in symbol else ",.5f"
    flags = get_flags(symbol)
    trend_icon = "↗️ Bullish Momentum" if "Bullish" in trend else "↘️ Bearish Momentum"
    
    # 3. The V25 Layout
    msg = (
        f"{header}\n"
        f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
        f"┏ {flags} <b>{symbol}</b> 🔸 <b>{side}</b> ┓\n"
        f"┗ 💵 <b>ENTRY:</b> <code>{price:{fmt}}</code> ┛\n\n"
        
        f"📊 <b>MARKET INTEL</b>\n"
        f"• <b>Trend:</b> {trend_icon}\n"
        f"• <b>RSI:</b> <code>{rsi:.0f}</code> {urgency}\n"
        f"• <b>Strength:</b> {bar}\n\n"
        
        f"🎯 <b>PROFIT TARGETS</b>\n"
        f"🥇 <b>TP1:</b> <code>{tp1:{fmt}}</code>\n"
        f"🥈 <b>TP2:</b> <code>{tp2:{fmt}}</code>\n\n"
        
        f"🛡️ <b>RISK MANAGEMENT</b>\n"
        f"🧱 <b>SL:</b> <code>{sl:{fmt}}</code>\n"
        f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
        f"<i>🤖 Nilesh Quant AI • V25 Premium</i>"
    )
    return msg

async def run_analysis_cycle(app_instance):
    print(f"🔄 Scanning 8 Pairs... {datetime.now()}")
    for symbol in WATCHLIST:
        try:
            df = fetch_data(symbol)
            if isinstance(df, str):
                 print(f"Skipping {symbol}: {df}")
                 time.sleep(5)
                 continue

            cpr = calculate_cpr(df)
            if not cpr: continue

            last = df.iloc[-1]
            price = last['close']
            
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
            if price > cpr['PP']: score += 0.5
            else: score -= 0.5

            signal = "WAIT (Neutral)"
            if score >= 2.5: signal = "STRONG BUY"
            elif 1.0 <= score < 2.5: signal = "BUY"
            elif -2.5 < score <= -1.0: signal = "SELL"
            elif score <= -2.5: signal = "STRONG SELL"

            tp1 = cpr['R1'] if score > 0 else cpr['S1']
            tp2 = cpr['R2'] if score > 0 else cpr['S2']
            sl = price - (last['atr'] * 1.5) if score > 0 else price + (last['atr'] * 1.5)
            trend = "Bullish" if price > last['ema_200'] else "Bearish"

            msg = format_premium_card(symbol, signal, price, rsi, trend, tp1, tp2, sl)
            if msg:
                await app_instance.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='HTML')

            # Wait 15s to respect API limits
            time.sleep(15)
            
        except Exception as e:
            print(f"❌ ERROR {symbol}: {e}")

# =========================================================================
# === BOT ENGINE ===
# =========================================================================

async def start_command(update, context):
    await update.message.reply_text("👋 <b>Nilesh V25 Online</b>", parse_mode='HTML')

def start_bot_process():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # Clear Webhooks
    temp_bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try: loop.run_until_complete(temp_bot.delete_webhook(drop_pending_updates=True))
    except: pass

    # App
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))

    try:
        loop.run_until_complete(application.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, 
            text="🚀 <b>SYSTEM RESTORED (V25 ULTRA-PREMIUM)</b>\nMath fixed. Design upgraded. Sending cards...", 
            parse_mode='HTML'
        ))
    except: pass

    # Triggers
    loop.create_task(run_analysis_cycle(application))

    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: asyncio.run_coroutine_threadsafe(run_analysis_cycle(application), loop), 'interval', minutes=30)
    scheduler.start()

    # Poll
    while True:
        try:
            application.run_polling(stop_signals=None, close_loop=False)
        except Conflict:
            time.sleep(15)
        except Exception as e:
            time.sleep(10)

if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
    t = threading.Thread(target=start_bot_process, daemon=True)
    t.start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, threaded=True)
