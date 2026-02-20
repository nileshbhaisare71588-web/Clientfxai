import os
import requests
import pandas as pd
import numpy as np
import time
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask

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

ACTIVE_TRADES = {} 
TRADE_HISTORY = []

app = Flask(__name__)
@app.route('/')
def home(): return "AI Adaptive Bot V4.1 Running"

def calculate_chop_index(df, period=14):
    try:
        df['tr0'] = abs(df['high'] - df['low'])
        df['tr1'] = abs(df['high'] - df['close'].shift())
        df['tr2'] = abs(df['low'] - df['close'].shift())
        df['tr'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
        
        df['atr_sum'] = df['tr'].rolling(period).sum()
        df['hh'] = df['high'].rolling(period).max()
        df['ll'] = df['low'].rolling(period).min()
        
        df['chop'] = 100 * np.log10(df['atr_sum'] / (df['hh'] - df['ll'])) / np.log10(period)
        return df['chop']
    except Exception:
        return pd.Series(50, index=df.index)

def fetch_data(symbol):
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": TIMEFRAME, "apikey": TD_API_KEY, "outputsize": 100}
    try:
        response = requests.get(url, params=params)
        data = response.json()
        
        if "code" in data and data["code"] == 429: return "RATE_LIMIT"
        if "values" not in data: return "NO_DATA"
            
        df = pd.DataFrame(data["values"])
        df['datetime'] = pd.to_datetime(df['datetime'])
        df.set_index('datetime', inplace=True)
        df = df.iloc[::-1]
        
        cols = ['open', 'high', 'low', 'close']
        df[cols] = df[cols].astype(float)
        
        df['volume'] = df['volume'].astype(float) if 'volume' in df.columns else 0.0

        df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
        df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
        
        delta = df['close'].diff()
        up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
        rs = up.ewm(com=13, adjust=False).mean() / down.ewm(com=13, adjust=False).mean()
        df['rsi'] = 100 - (100 / (1 + rs))
        
        df['tr0'] = abs(df['high'] - df['low'])
        df['tr1'] = abs(df['high'] - df['close'].shift())
        df['tr2'] = abs(df['low'] - df['close'].shift())
        df['atr'] = df[['tr0', 'tr1', 'tr2']].max(axis=1).rolling(14).mean()

        df['chop'] = calculate_chop_index(df)
        df['vol_ma'] = df['volume'].rolling(20).mean()

        return df.dropna()
    except Exception as e: 
        print(f"Fetch Error: {e}")
        return "ERROR"

def get_flags(symbol):
    base, quote = symbol.split('/')
    flags = {
        "EUR": "🇪🇺", "USD": "🇺🇸", "GBP": "🇬🇧", "JPY": "🇯🇵",
        "AUD": "🇦🇺", "CAD": "🇨🇦", "XAU": "🥇", "BTC": "🅱️"
    }
    return f"{flags.get(base, '')}{flags.get(quote, '')}"

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Telegram Error: {e}")

def format_signal_card(symbol, signal, price, rsi, ema_trend, tp1, sl, vol_spike, chop):
    header = "🦅 <b>ADAPTIVE SNIPER: BUY</b> 🦅" if signal == "BUY" else "🐻 <b>ADAPTIVE SNIPER: SELL</b> 🐻"
    side = "LONG 🟢" if signal == "BUY" else "SHORT 🔴"
    fmt = ",.2f" if any(x in symbol for x in ["JPY", "XAU", "BTC"]) else ",.5f"
    
    vol_status = "🔥 HIGH" if vol_spike else "😐 NORMAL"
    market_state = "TRENDING 🌊" if chop < 50 else "CHOPPY 🌪️"

    msg = (
        f"{header}\n"
        f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
        f"┏ {get_flags(symbol)} <b>{symbol}</b> 🔸 <b>{side}</b> ┓\n"
        f"┗ 💵 <b>ENTRY:</b> <code>{price:{fmt}}</code> ┛\n\n"
        f"🧠 <b>MARKET LOGIC</b>\n"
        f"• Trend: {ema_trend}\n"
        f"• RSI: {rsi:.1f} | Chop: {chop:.1f} ({market_state})\n"
        f"• Volume: {vol_status}\n\n"
        f"🎯 <b>TARGETS & RISK</b>\n"
        f"🛑 <b>SL:</b> <code>{sl:{fmt}}</code>\n"
        f"✅ <b>TP:</b> <code>{tp1:{fmt}}</code>\n\n"
        f"<i>Disclaimer: High-probability setup based on algorithmic confluence. Manage your risk.</i>"
    )
    return msg

def analyze_markets():
    print("Scanning markets...")
    for symbol in WATCHLIST:
        df = fetch_data(symbol)
        if isinstance(df, str): continue
            
        latest = df.iloc[-1]
        price = latest['close']
        rsi = latest['rsi']
        ema_50 = latest['ema_50']
        ema_200 = latest['ema_200']
        atr = latest['atr']
        chop = latest['chop']
        vol_spike = latest['volume'] > (latest['vol_ma'] * 1.5)

        # Logic: Trending Market (Chop < 50), EMA alignment, RSI not extreme
        if chop < 50:
            if price > ema_50 and ema_50 > ema_200 and rsi < 70 and latest['open'] < latest['close']:
                sl = price - (atr * 1.5)
                tp = price + (atr * 2.0)
                msg = format_signal_card(symbol, "BUY", price, rsi, "Bullish", tp, sl, vol_spike, chop)
                send_telegram_message(msg)
                
            elif price < ema_50 and ema_50 < ema_200 and rsi > 30 and latest['open'] > latest['close']:
                sl = price + (atr * 1.5)
                tp = price - (atr * 2.0)
                msg = format_signal_card(symbol, "SELL", price, rsi, "Bearish", tp, sl, vol_spike, chop)
                send_telegram_message(msg)
        
        time.sleep(1) # Prevent API rate limits

if __name__ == '__main__':
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=analyze_markets, trigger="interval", minutes=60)
    scheduler.start()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
