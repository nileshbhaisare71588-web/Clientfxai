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
from telegram.error import Conflict, NetworkError
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
    trend_icon = "↗️ Bullish" if "Bullish" in trend
