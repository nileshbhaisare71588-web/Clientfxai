import os
import requests
import pandas as pd
import numpy as np
import time
import threading
from dotenv import load_dotenv
from flask import Flask

# --- CONFIGURATION ---
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
def home(): return "AI Adaptive Bot V4.1 Running"

# =========================================================================
# === DATA ENGINE ===
# =========================================================================

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
    except Exception as e:
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

        # Indicators
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

        # MACD Calculation
        exp1 = df['close'].ewm(span=12, adjust=False).mean()
        exp2 = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = exp1 - exp2
        df['signal_line'] = df['macd'].ewm(span=9, adjust=False).mean()

        df['chop'] = calculate_chop_index(df)
        df['vol_ma'] = df['volume'].rolling(20).mean()

        return df.dropna()
    except Exception as e: 
        return f"ERROR: {str(e)}"

# =========================================================================
# === SIGNAL LOGIC ===
# =========================================================================

def generate_signal(df, symbol):
    if len(df) < 20:
        return None
        
    current = df.iloc[-1]
    previous = df.iloc[-2]
    
    trend_up = current['close'] > current['ema_200']
    trend_down = current['close'] < current['ema_200']
    is_trending = current['chop'] < 50 
    
    macd_bullish_cross = (current['macd'] > current['signal_line']) and (previous['macd'] <= previous['signal_line'])
    macd_bearish_cross = (current['macd'] < current['signal_line']) and (previous['macd'] >= previous['signal_line'])
    vol_spike = current['volume'] > current['vol_ma']
    
    signal = None
    sl = 0.0
    tp1 = 0.0
    tp2 = 0.0
    
    if trend_up and is_trending and macd_bullish_cross and current['rsi'] < 70:
        signal = "BUY"
        sl = current['close'] - (current['atr'] * 1.5)
        tp1 = current['close'] + (current['atr'] * 2.0)
        tp2 = current['close'] + (current['atr'] * 3.5)

    elif trend_down and is_trending and macd_bearish_cross and current['rsi'] > 30:
        signal = "SELL"
        sl = current['close'] + (current['atr'] * 1.5)
        tp1 = current['close'] - (current['atr'] * 2.0)
        tp2 = current['close'] - (current['atr'] * 3.5)
        
    if signal:
        return {
            "symbol": symbol,
            "signal": signal,
            "price": current['close'],
            "rsi": current['rsi'],
            "trend": "UP" if trend_up else "DOWN",
            "tp1": tp1,
            "tp2": tp2,
            "sl": sl,
            "vol_spike": vol_spike,
            "chop": current['chop']
        }
    return None

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

def format_signal_card(data):
    fmt = ",.2f" if "JPY" in data['symbol'] or "XAU" in data['symbol'] or "BTC" in data['symbol'] else ",.5f"
    flags = get_flags(data['symbol'])
    
    header = "🦅 <b>ADAPTIVE SNIPER: BUY</b> 🦅" if "BUY" in data['signal'] else "🐻 <b>ADAPTIVE SNIPER: SELL</b> 🐻"
    side = "LONG 🟢" if "BUY" in data['signal'] else "SHORT 🔴"
    vol_status = "🔥 HIGH" if data['vol_spike'] else "😐 NORMAL"
    market_state = "TRENDING 🌊" if data['chop'] < 50 else "CHOPPY 🌪️"

    msg = (
        f"{header}\n"
        f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
        f"┏ {flags} <b>{data['symbol']}</b> 🔸 <b>{side}</b> ┓\n"
        f"┗ 💵 <b>ENTRY:</b> <code>{data['price']:{fmt}}</code> ┛\n\n"
        f"🧠 <b>MARKET LOGIC</b>\n"
        f"📊 RSI: {data['rsi']:.1f}\n"
        f"📈 Trend: {data['trend']}\n"
        f"🔊 Vol: {vol_status}\n"
        f"🌊 Market: {market_state}\n\n"
        f"🎯 <b>TP1:</b> <code>{data['tp1']:{fmt}}</code>\n"
        f"🎯 <b>TP2:</b> <code>{data['tp2']:{fmt}}</code>\n"
        f"🛑 <b>SL:</b> <code>{data['sl']:{fmt}}</code>\n"
    )
    return msg

def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    requests.post(url, json=payload)

# =========================================================================
# === MAIN EXECUTION LOOP ===
# =========================================================================

def run_bot():
    print("Bot started scanning...")
    while True:
        for symbol in WATCHLIST:
            df = fetch_data(symbol)
            if isinstance(df, str):
                print(f"Skipping {symbol}: {df}")
                continue
                
            signal_data = generate_signal(df, symbol)
            
            if signal_data:
                msg = format_signal_card(signal_data)
                send_telegram_message(msg)
                print(f"Signal sent for {symbol}")
                
            time.sleep(2) # Prevent API rate limits
        
        print("Cycle complete. Waiting for next hour...")
        time.sleep(3600) # Wait 1 hour based on TIMEFRAME

if __name__ == '__main__':
    # Run bot loop in background
    threading.Thread(target=run_bot, daemon=True).start()
    
    # Run Flask app to keep alive
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
