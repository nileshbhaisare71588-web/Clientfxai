import os
import requests
import pandas as pd
import numpy as np
import asyncio
import time
import threading
import math
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
# Updated to store 'highest_price' and 'lowest_price' for Trailing Stop
ACTIVE_TRADES = {} 
TRADE_HISTORY = []

app = Flask(__name__)
@app.route('/')
def home(): return "AI Adaptive Bot V4.0 Running"

# =========================================================================
# === DATA ENGINE (ADAPTIVE UPGRADE) ===
# =========================================================================

def calculate_chop_index(df, period=14):
    """Calculates Choppiness Index to detect Ranging Markets"""
    try:
        df['tr0'] = abs(df['high'] - df['low'])
        df['tr1'] = abs(df['high'] - df['close'].shift())
        df['tr2'] = abs(df['low'] - df['close'].shift())
        df['tr'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
        
        # CHOP Formula
        df['atr_sum'] = df['tr'].rolling(period).sum()
        df['hh'] = df['high'].rolling(period).max()
        df['ll'] = df['low'].rolling(period).min()
        
        # Avoid division by zero
        df['chop'] = 100 * np.log10(df['atr_sum'] / (df['hh'] - df['ll'])) / np.log10(period)
        return df['chop']
    except Exception as e:
        print(f"CHOP Error: {e}")
        return pd.Series(50, index=df.index) # Default to neutral

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
        
        # Convert columns to float
        cols = ['open', 'high', 'low', 'close']
        df[cols] = df[cols].astype(float)
        
        # Handle Volume (some pairs might not return volume)
        if 'volume' in df.columns:
            df['volume'] = df['volume'].astype(float)
        else:
            df['volume'] = 0.0

        # --- 1. TREND INDICATORS ---
        df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
        df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
        
        # --- 2. RSI & MOMENTUM ---
        delta = df['close'].diff()
        up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
        rs = up.ewm(com=13, adjust=False).mean() / down.ewm(com=13, adjust=False).mean()
        df['rsi'] = 100 - (100 / (1 + rs))
        
        # --- 3. VOLATILITY (ATR) ---
        df['tr0'] = abs(df['high'] - df['low'])
        df['tr1'] = abs(df['high'] - df['close'].shift())
        df['tr2'] = abs(df['low'] - df['close'].shift())
        df['atr'] = df[['tr0', 'tr1', 'tr2']].max(axis=1).rolling(14).mean()

        # --- 4. CHOPPINESS INDEX (NEW) ---
        df['chop'] = calculate_chop_index(df)

        # --- 5. VOLUME SMA (NEW) ---
        # 20-period Moving Average of Volume to detect spikes
        df['vol_ma'] = df['volume'].rolling(20).mean()

        return df.dropna()
    except Exception as e: return f"ERROR: {str(e)}"

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

def format_signal_card(symbol, signal, price, rsi, trend, tp1, sl, vol_spike, chop):
    if "BUY" in signal:
        header = "🦅 <b>ADAPTIVE SNIPER: BUY</b> 🦅"
        side = "LONG 🟢"
        color = "🟩"
    else:
        header = "🐻 <b>ADAPTIVE SNIPER: SELL</b> 🐻"
        side = "SHORT 🔴"
        color = "🟥"

    fmt = ",.2f" if "JPY" in symbol or "XAU" in symbol or "BTC" in symbol else ",.5f"
    flags = get_flags(symbol)
    
    vol_status = "🔥 HIGH" if vol_spike else "😐 NORMAL"
    market_state = "TRENDING 🌊" if chop < 50 else "CHOPPY 🌪️"

    msg = (
        f"{header}\n"
        f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
        f"┏ {flags} <b>{symbol}</b> 🔸 <b>{side}</b> ┓\n"
        f"┗ 💵 <b>ENTRY:</b> <code>{price:{fmt}}</code> ┛\n\n"
        f"🧠 <b>MARKET LOGIC</b>\n"
        f"• <b>Regime:</b> {market_state} (Chop: {chop:.0f})\n"
        f"• <b>Trend:</b> {trend}\n"
        f"• <b>Volume:</b> {vol_status}\n"
        f"• <b>RSI:</b> <code>{rsi:.0f}</code>\n\n"
        f"🎯 <b>TARGETS</b>\n"
        f"🚀 <b>Target:</b> <code>{tp1:{fmt}}</code> (Open Target)\n"
        f"🛡️ <b>Initial SL:</b> <code>{sl:{fmt}}</code>\n"
        f"<i>*Trailing Stop Activated*</i>\n"
        f"〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
        f"<i>🤖 Adaptive V4.0 Logic</i>"
    )
    return msg

def format_exit_card(symbol, result_type, pips, entry, exit_price, reason):
    flags = get_flags(symbol)
    fmt = ",.2f" if "JPY" in symbol
