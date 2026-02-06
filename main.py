import os
import requests
import pandas as pd
import numpy as np
import asyncio
import time
import threading
from datetime import datetime, timedelta
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

# --- STATE MANAGEMENT ---
ACTIVE_TRADES = {}
TRADE_HISTORY = []

app = Flask(__name__)
@app.route('/')
def home(): return "AI Bot V25 Sniper Edition"

# =========================================================================
# === DATA ENGINE (UPGRADED) ===
# =========================================================================

def fetch_data(symbol):
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": TIMEFRAME, "apikey": TD_API_KEY, "outputsize": 100} # Increased for ADX
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
        
        # --- 1. TREND INDICATORS ---
        df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
        df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
        
        # --- 2. RSI ---
        delta = df['close'].diff()
        up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
        rs = up.ewm(com=13, adjust=False).mean() / down.ewm(com=13, adjust=False).mean()
        df['rsi'] = 100 - (100 / (1 + rs))
        
        # --- 3. MACD ---
        exp1 = df['close'].ewm(span=12, adjust=False).mean()
        exp2 = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = exp1 - exp2
        df['signal_line'] = df['macd'].ewm(span=9, adjust=False).mean()
        
        # --- 4. ATR (Volatility) ---
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        df['atr'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(14).mean()

        # --- 5. ADX (Trend Strength - NEW CRITICAL FILTER) ---
        plus_dm = df['high'].diff()
        minus_dm = df['low'].diff()
        plus_dm[plus_dm < 0] = 0
        minus_dm[minus_dm > 0] = 0
        
        tr1 = pd.DataFrame(high_low)
        tr2 = pd.DataFrame(high_close)
        tr3 = pd.DataFrame(low_close)
        frames = [tr1, tr2, tr3]
        tr = pd.concat(frames, axis=1, join='inner').max(axis=1)
        atr = tr.rolling(14).mean()
        
        plus_di = 100 * (plus_dm.ewm(alpha=1/14).mean() / atr)
        minus_di = abs(100 * (minus_dm.ewm(alpha=1/14).mean() / atr))
        dx = (abs(plus_di - minus_di) / abs(plus_di + minus_di)) * 100
        df['adx'] = dx.rolling(14).mean()

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
        R2, S2 = PP + (H - L), PP - (H - L)
        return {'PP': PP, 'R1': R1, 'S1': S1, 'R2': R2, 'S2': S2}
    except: return None

# =========================================================================
# === UTILITIES & FORMATTING ===
# =========================================================================

def get_flags(symbol):
    base, quote = symbol.split('/')
    flags = {
        "EUR": "🇪🇺", "USD": "🇺🇸", "GBP": "🇬🇧", "JPY": "🇯🇵",
        "AUD": "🇦🇺", "CAD": "🇨🇦", "XAU": "🥇", "BTC": "🅱️"
    }
    return f"{flags.get(base, '')}{flags.get(quote, '')}"

def calculate_pips(symbol, entry, current_price):
    diff = current_price - entry
    if "JPY" in symbol: multiplier = 100
    elif "XAU" in symbol: multiplier = 10
    elif "BTC" in symbol: multiplier = 1
    else: multiplier = 10000
    return diff * multiplier

def format_premium_card(symbol, signal, price, rsi, trend, tp1, tp2, sl, adx):
    if "STRONG BUY" in signal:
        header = "🚀💎 <b>SNIPER ENTRY: BUY</b> 💎🚀"
        side = "LONG 🟢"
        bar = "🟩🟩🟩🟩🟩 95% CONFIDENCE"
    elif "STRONG SELL" in signal:
        header = "🔻💎 <b>SNIPER ENTRY: SELL</b> 💎🔻"
        side = "SHORT 🔴"
        bar = "🟥🟥🟥🟥🟥 95% CONFIDENCE"
    else:
        return None # We only trade STRONG signals now

    fmt = ",.2f" if "JPY" in symbol or "XAU" in symbol or "BTC" in symbol else ",.5f"
    flags = get_flags(symbol)
    
    msg = (
        f"{header}\n"
        f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
        f"┏ {flags} <b>{symbol}</b> 🔸 <b>{side}</b> ┓\n"
        f"┗ 💵 <b>ENTRY:</b> <code>{price:{fmt}}</code> ┛\n\n"
        f"📊 <b>HIGH PRECISION INTEL</b>\n"
        f"• <b>Trend:</b> {trend}\n"
        f"• <b>ADX Power:</b> <code>{adx:.0f}</code> (Trend Strength)\n"
        f"• <b>RSI:</b> <code>{rsi:.0f}</code>\n"
        f"• <b>Signal Strength:</b> {bar}\n\n"
        f"🎯 <b>PROFIT TARGETS</b>\n"
        f"🥇 <b>TP1:</b> <code>{tp1:{fmt}}</code>\n"
        f"🥈 <b>TP2:</b> <code>{tp2:{fmt}}</code>\n\n"
        f"🛡️ <b>RISK MANAGEMENT</b>\n"
        f"🧱 <b>SL:</b> <code>{sl:{fmt}}</code>\n"
        f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
        f"<i>🤖 AI BOT • Sniper Edition</i>"
    )
    return msg

def format_result_card(symbol, result_type, pips, entry, exit_price):
    flags = get_flags(symbol)
    fmt = ",.2f" if "JPY" in symbol or "XAU" in symbol or "BTC" in symbol else ",.5f"
    
    if result_type == "WIN":
        header = "🏆 <b>PROFIT SECURED</b> 🏆"
        pip_text = f"+{pips:.1f} Pips 🤑"
    else:
        header = "🛡️ <b>STOP LOSS HIT</b> 🛡️"
        pip_text = f"{pips:.1f} Pips 🩸"

    msg = (
        f"{header}\n"
        f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
        f"┏ {flags} <b>{symbol}</b> 🔸 <b>CLOSED</b> ┓\n"
        f"┗ 💵 <b>RESULT:</b> {pip_text} ┛\n\n"
        f"🚪 <b>Entry:</b> <code>{entry:{fmt}}</code>\n"
        f"🏁 <b>Exit:</b> <code>{exit_price:{fmt}}</code>\n"
        f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
        f"<i>🤖 AI BOT • Trade Result</i>"
    )
    return msg

async def send_12h_report(app_instance):
    if not TRADE_HISTORY: return
    wins = sum(1 for t in TRADE_HISTORY if t['result'] == 'WIN')
    losses = sum(1 for t in TRADE_HISTORY if t['result'] == 'LOSS')
    total_pips = sum(t['pips'] for t in TRADE_HISTORY)
    win_rate = (wins / len(TRADE_HISTORY)) * 100 if len(TRADE_HISTORY) > 0 else 0
    net_color = "🟢" if total_pips > 0 else "🔴"
    
    msg = (
        f"📑 <b>12-HOUR SNIPER REPORT</b>\n"
        f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
        f"🔢 <b>Total Trades:</b> {len(TRADE_HISTORY)}\n"
        f"✅ <b>Wins:</b> {wins}\n"
        f"❌ <b>Losses:</b> {losses}\n"
        f"🎯 <b>Win Rate:</b> {win_rate:.1f}%\n\n"
        f"💰 <b>NET PIPS:</b> {net_color} <b>{total_pips:.1f}</b>\n"
        f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️"
    )
    try:
        await app_instance.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='HTML')
        TRADE_HISTORY.clear()
    except Exception as e: print(f"Report Error: {e}")

# =========================================================================
# === SNIPER ANALYSIS ENGINE (90% LOGIC) ===
# =========================================================================

async def run_analysis_cycle(app_instance):
    print(f"🔄 Sniper Scan... {datetime.now()}")
    for symbol in WATCHLIST:
        try:
            df = fetch_data(symbol)
            if isinstance(df, str):
                 time.sleep(2)
                 continue

            last = df.iloc[-1]
            current_price = last['close']
            
            # --- ACTIVE TRADE MANAGEMENT ---
            if symbol in ACTIVE_TRADES:
                trade = ACTIVE_TRADES[symbol]
                if trade['side'] == "LONG":
                    pips = calculate_pips(symbol, trade['entry'], current_price)
                    if current_price >= trade['tp1']: # WIN
                        await app_instance.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=format_result_card(symbol, "WIN", pips, trade['entry'], current_price), parse_mode='HTML')
                        TRADE_HISTORY.append({'symbol': symbol, 'result': 'WIN', 'pips': pips})
                        del ACTIVE_TRADES[symbol]
                        continue
                    elif current_price <= trade['sl']: # LOSS
                        await app_instance.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=format_result_card(symbol, "LOSS", pips, trade['entry'], current_price), parse_mode='HTML')
                        TRADE_HISTORY.append({'symbol': symbol, 'result': 'LOSS', 'pips': pips})
                        del ACTIVE_TRADES[symbol]
                        continue
                elif trade['side'] == "SHORT":
                    pips = calculate_pips(symbol, trade['entry'], current_price) * -1
                    if current_price <= trade['tp1']: # WIN
                        await app_instance.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=format_result_card(symbol, "WIN", pips, trade['entry'], current_price), parse_mode='HTML')
                        TRADE_HISTORY.append({'symbol': symbol, 'result': 'WIN', 'pips': pips})
                        del ACTIVE_TRADES[symbol]
                        continue
                    elif current_price >= trade['sl']: # LOSS
                        await app_instance.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=format_result_card(symbol, "LOSS", pips, trade['entry'], current_price), parse_mode='HTML')
                        TRADE_HISTORY.append({'symbol': symbol, 'result': 'LOSS', 'pips': pips})
                        del ACTIVE_TRADES[symbol]
                        continue
                continue 

            # --- SNIPER ENTRY LOGIC ---
            cpr = calculate_cpr(df)
            if not cpr: continue

            price = last['close']
            rsi = last['rsi']
            adx = last['adx']
            ema50 = last['ema_50']
            ema200 = last['ema_200']
            
            signal = "WAIT"
            
            # FILTER 1: ADX > 25 (Trend Strength)
            # If ADX is below 25, market is chopping/sideways. WE DO NOT TRADE.
            if adx < 25: 
                continue 

            # STRATEGY 1: GOLDEN TREND BUY
            # Price > EMA50 > EMA200 AND MACD Crossover AND RSI Healthy
            if (price > ema50 > ema200) and (last['macd'] > last['signal_line']) and (50 <= rsi <= 70):
                signal = "STRONG BUY"
            
            # STRATEGY 2: GOLDEN TREND SELL
            # Price < EMA50 < EMA200 AND MACD Crossunder AND RSI Healthy
            elif (price < ema50 < ema200) and (last['macd'] < last['signal_line']) and (30 <= rsi <= 50):
                signal = "STRONG SELL"

            # STRATEGY 3: EXTREME REVERSAL (Oversold/Overbought with ADX exhaustion)
            elif rsi < 30 and price > cpr['S1']: # Oversold Bounce
                signal = "STRONG BUY"
            elif rsi > 70 and price < cpr['R1']: # Overbought Rejection
                signal = "STRONG SELL"

            if "STRONG" in signal:
                # RISK MANAGEMENT
                # TP is conservative (1.5x ATR), SL is tight (1.0x ATR) to ensure R:R
                if "BUY" in signal:
                    tp1 = price + (last['atr'] * 2.0)
                    tp2 = price + (last['atr'] * 3.5)
                    sl = price - (last['atr'] * 1.2) # Tighter SL
                    trend = "Bullish Uptrend"
                    side = "LONG"
                else:
                    tp1 = price - (last['atr'] * 2.0)
                    tp2 = price - (last['atr'] * 3.5)
                    sl = price + (last['atr'] * 1.2) # Tighter SL
                    trend = "Bearish Downtrend"
                    side = "SHORT"

                msg = format_premium_card(symbol, signal, price, rsi, trend, tp1, tp2, sl, adx)
                if msg:
                    ACTIVE_TRADES[symbol] = {'entry': price, 'tp1': tp1, 'sl': sl, 'side': side, 'start_time': datetime.now()}
                    await app_instance.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='HTML')

            time.sleep(15)
            
        except Exception as e:
            print(f"❌ ERROR {symbol}: {e}")

# =========================================================================
# === BOT ENGINE ===
# =========================================================================

async def start_command(update, context):
    await update.message.reply_text("👋 <b>SNIPER BOT V30 Online</b>", parse_mode='HTML')

def start_bot_process():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    temp_bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try: loop.run_until_complete(temp_bot.delete_webhook(drop_pending_updates=True))
    except: pass

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))

    try:
        loop.run_until_complete(application.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, 
            text="🚀 <b>SNIPER MODE ACTIVATED</b>\nHigh Precision Filters: ON\nADX Trend Filter: ON", 
            parse_mode='HTML'
        ))
    except: pass

    loop.create_task(run_analysis_cycle(application))
    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: asyncio.run_coroutine_threadsafe(run_analysis_cycle(application), loop), 'interval', minutes=30)
    scheduler.add_job(lambda: asyncio.run_coroutine_threadsafe(send_12h_report(application), loop), 'interval', hours=12)
    scheduler.start()

    while True:
        try: application.run_polling(stop_signals=None, close_loop=False)
        except Conflict: time.sleep(15)
        except Exception as e: time.sleep(10)

if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
    t = threading.Thread(target=start_bot_process, daemon=True)
    t.start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, threaded=True)
