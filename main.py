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
TIMEFRAME = "30min"

app = Flask(__name__)
@app.route('/')
def home(): return "AI Adaptive Bot V4.1 Running"

# =========================================================================
# === DATA ENGINE & LOGIC ===
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
        return 100 * np.log10(df['atr_sum'] / (df['hh'] - df['ll'])) / np.log10(period)
    except:
        return pd.Series(50, index=df.index)

def fetch_data(symbol):
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": TIMEFRAME, "apikey": TD_API_KEY, "outputsize": 100}
    try:
        response = requests.get(url, params=params).json()
        if "values" not in response: return "NO_DATA"
            
        df = pd.DataFrame(response["values"])
        df['datetime'] = pd.to_datetime(df['datetime'])
        df.set_index('datetime', inplace=True)
        df = df.iloc[::-1]
        
        cols = ['open', 'high', 'low', 'close']
        df[cols] = df[cols].astype(float)
        df['volume'] = df['volume'].astype(float) if 'volume' in df.columns else 0.0

        df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
        
        delta = df['close'].diff()
        up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
        rs = up.ewm(com=13, adjust=False).mean() / down.ewm(com=13, adjust=False).mean()
        df['rsi'] = 100 - (100 / (1 + rs))
        
        df['tr0'] = abs(df['high'] - df['low'])
        df['tr1'] = abs(df['high'] - df['close'].shift())
        df['tr2'] = abs(df['low'] - df['close'].shift())
        df['atr'] = df[['tr0', 'tr1', 'tr2']].max(axis=1).rolling(14).mean()

        exp1 = df['close'].ewm(span=12, adjust=False).mean()
        exp2 = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = exp1 - exp2
        df['signal_line'] = df['macd'].ewm(span=9, adjust=False).mean()

        df['chop'] = calculate_chop_index(df)
        df['vol_ma'] = df['volume'].rolling(20).mean()

        return df.dropna()
    except Exception as e: return f"ERROR: {str(e)}"

def generate_signal(df, symbol):
    if len(df) < 20: return None
        
    current, previous = df.iloc[-1], df.iloc[-2]
    trend_up = current['close'] > current['ema_200']
    trend_down = current['close'] < current['ema_200']
    is_trending = current['chop'] < 50 
    
    macd_bull = (current['macd'] > current['signal_line']) and (previous['macd'] <= previous['signal_line'])
    macd_bear = (current['macd'] < current['signal_line']) and (previous['macd'] >= previous['signal_line'])
    vol_spike = current['volume'] > current['vol_ma']
    
    signal, sl, tp1, tp2 = None, 0.0, 0.0, 0.0
    
    if trend_up and is_trending and macd_bull and current['rsi'] < 70:
        signal = "BUY"
        sl, tp1, tp2 = current['close'] - (current['atr'] * 1.5), current['close'] + (current['atr'] * 2.0), current['close'] + (current['atr'] * 3.5)
    elif trend_down and is_trending and macd_bear and current['rsi'] > 30:
        signal = "SELL"
        sl, tp1, tp2 = current['close'] + (current['atr'] * 1.5), current['close'] - (current['atr'] * 2.0), current['close'] - (current['atr'] * 3.5)
        
    if signal:
        return {"symbol": symbol, "signal": signal, "price": current['close'], "rsi": current['rsi'], "trend": "UP" if trend_up else "DOWN", "tp1": tp1, "tp2": tp2, "sl": sl, "vol_spike": vol_spike, "chop": current['chop']}
    return None

# =========================================================================
# === TELEGRAM & EXECUTION ===
# =========================================================================

def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: Tokens missing.")
        return
    url
