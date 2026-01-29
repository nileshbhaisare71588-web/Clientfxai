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

# =========================================================================
# === FLASK APP (For Render) ===
# =========================================================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Nilesh Bot V22 (Instant Trigger)"

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
        df = df.iloc[::-1]
        df[['open', 'high', 'low', 'close']] = df[['open', 'high', 'low', 'close']].astype(float)
        
        # Add Indicators
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
    except: return pd.DataFrame()

def calculate_cpr(df):
    try:
        df_daily = df.resample('D').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
        if len(df_daily) < 2: return None
        prev = df_daily.iloc[-2]
        PP = (prev['high'] + prev['low'] + prev['close']) / 3.0
        return {'PP': PP, 'R1': 2*PP - prev['low'], 'S1': 2*PP - prev['high']}
    except: return None

# =========================================================================
# === CARD DESIGNER ===
# =========================================================================

def format_premium_card(symbol, signal, price, rsi, trend, tp1, tp2, sl):
    if "STRONG BUY" in signal:
        header, theme, bar = "⚡ <b>INSTITUTIONAL BUY</b>", "🟢", "🟩🟩🟩🟩🟩"
    elif "STRONG SELL" in signal:
        header, theme, bar = "⚡ <b>INSTITUTIONAL SELL</b>", "🔴", "🟥🟥🟥🟥🟥"
    elif "BUY" in signal:
        header, theme, bar = "🟢 <b>BUY SIGNAL</b>", "🟢", "🟩🟩🟩⬜⬜"
    elif "SELL" in signal:
        header, theme, bar = "🔴 <b>SELL SIGNAL</b>", "🔴", "🟥🟥🟥⬜⬜"
    else:
        header, theme, bar = "⚖️ <b>MARKET NEUTRAL</b>", "⚪", "⬜⬜⬜⬜⬜"

    fmt = ",.2f" if "JPY" in symbol or "XAU" in symbol or "BTC" in symbol else ",.5f"
    trend_icon = "↗️ Bullish" if "Bullish" in trend else "↘️ Bearish"
    rsi_status = "Overbought ⚠️" if rsi > 70 else "Oversold 💎" if rsi < 30 else "Neutral"

    return (
        f"{header}\n▬▬▬▬▬▬▬▬▬▬▬▬▬▬\n"
        f"<b>Asset:</b> #{symbol.replace('/', '')}  {theme}\n"
        f"<b>Price:</b> <code>{price:{fmt}}</code>\n\n"
        f"📊 <b>MARKET CONTEXT</b>\n├ <b>Trend:</b> {trend_icon}\n├ <b>RSI ({rsi:.0f}):</b> {rsi_status}\n└ <b>Power:</b> {bar}\n\n"
        f"🎯 <b>KEY LEVELS</b>\n├ <b>TP 1:</b> <code>{tp1:{fmt}}</code>\n├ <b>TP 2:</b> <code>{tp2:{fmt}}</code>\n└ <b>SL:</b>   <code>{sl:{fmt}}</code>\n\n"
        f"<i>Nilesh Quant V22</i>"
    )

async def run_analysis_cycle(app_instance):
    print(f"🔄 Scanning 8 Pairs... {datetime.now()}")
    for symbol in WATCHLIST:
        try:
            df = fetch_data(symbol)
            if df.empty:
                print(f"⚠️ No data for {symbol}, skipping...")
                time.sleep(2)
                continue

            cpr = calculate_cpr(df)
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
            if cpr and price > cpr['PP']: score += 0.5
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
                print(f"📤 Sending {symbol}...")
                await app_instance.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='HTML')

            # Wait 8s to prevent API ban
            time.sleep(8)
            
        except Exception as e:
            print(f"❌ Error {symbol}: {e}")

# =========================================================================
# === BOT ENGINE ===
# =========================================================================

async def start_command(update, context):
    await update.message.reply_text("👋 <b>Nilesh V22 Online</b>", parse_mode='HTML')

def start_bot_process():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # 1. Clear Webhooks
    print("🧹 Clearing Webhooks...")
    temp_bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try: loop.run_until_complete(temp_bot.delete_webhook(drop_pending_updates=True))
    except: pass

    # 2. Setup App
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))

    # 3. Send Startup Msg
    try:
        loop.run_until_complete(application.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, 
            text="🚀 <b>SYSTEM RESTORED</b>\nV22 Instant Trigger.\nSending 8 cards immediately...", 
            parse_mode='HTML'
        ))
    except: pass

    # 4. INSTANT TRIGGER (Run loop immediately in background)
    print("⚡ Triggering First Scan Now...")
    loop.create_task(run_analysis_cycle(application))

    # 5. Schedule Future Scans (Every 30 mins)
    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: asyncio.run_coroutine_threadsafe(run_analysis_cycle(application), loop), 'interval', minutes=30)
    scheduler.start()

    # 6. Start Polling
    print("✅ Bot Polling Started")
    while True:
        try:
            application.run_polling(stop_signals=None, close_loop=False)
        except Conflict:
            print("⚠️ CONFLICT: Waiting 15s...")
            time.sleep(15)
        except Exception as e:
            print(f"⚠️ Error: {e}")
            time.sleep(10)

if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
    t = threading.Thread(target=start_bot_process, daemon=True)
    t.start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, threaded=True)
