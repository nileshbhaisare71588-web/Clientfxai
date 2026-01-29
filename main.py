# main.py - PREMIER FOREX AI QUANT V3.0 (Advanced Confluence)

import os
import ccxt
import pandas as pd
import numpy as np
import asyncio
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Bot
from flask import Flask, jsonify, render_template_string
import threading
import time

# --- CONFIGURATION ---
from dotenv import load_dotenv 
load_dotenv() 

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# USER REQUESTED PAIRS (Formatted for CCXT/Kraken)
# Note: 'GPBUSD' corrected to 'GBP/USD'
FOREX_PAIRS = [
    "EUR/USD", "GBP/JPY", "AUD/USD", "GBP/USD", 
    "XAU/USD", "AUD/CAD", "AUD/JPY", "BTC/USD"
]

TIMEFRAME_MAIN = "4h"  # Major Trend (Trend Filter)
TIMEFRAME_ENTRY = "1h" # Entry Precision

# Initialize Bot and Exchange 
bot = Bot(token=TELEGRAM_BOT_TOKEN)
exchange = ccxt.kraken({
    'enableRateLimit': True, 
    'rateLimit': 2000,
    'params': {'timeout': 20000} 
})

bot_stats = {
    "status": "initializing",
    "total_analyses": 0,
    "last_analysis": None,
    "monitored_assets": FOREX_PAIRS,
    "uptime_start": datetime.now().isoformat(),
    "version": "V3.0 Advanced Logic"
}

# =========================================================================
# === ADVANCED INDICATOR MATH ===
# =========================================================================

def calculate_indicators(df):
    """Adds EMA, RSI, and ATR without external heavy libraries."""
    # 1. EMAs (Exponential Moving Averages)
    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean() # Major Trend
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()   # Intermediate Trend
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()   # Fast Trend
    
    # 2. RSI (Relative Strength Index - 14 period)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))

    # 3. ATR (Average True Range - 14 period) for Volatility
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    df['atr'] = true_range.rolling(14).mean()
    
    return df

def calculate_cpr_levels(df_daily):
    """Calculates Pivot Points for Institutional Targets."""
    if df_daily.empty or len(df_daily) < 2: return None
    prev_day = df_daily.iloc[-2]
    H, L, C = prev_day['high'], prev_day['low'], prev_day['close']
    PP = (H + L + C) / 3.0
    BC = (H + L) / 2.0
    TC = PP - BC + PP
    return {
        'PP': PP, 'TC': TC, 'BC': BC,
        'R1': 2*PP - L, 'S1': 2*PP - H,
        'R2': PP + (H - L), 'S2': PP - (H - L),
        'R3': H + 2 * (PP - L), 'S3': L - 2 * (H - PP)
    }

def fetch_data_safe(symbol, timeframe, limit=300):
    """Robust fetcher that ensures enough data for 200 EMA."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if not exchange.markets: exchange.load_markets()
            # Normalize symbol if needed (some exchanges use different IDs)
            try:
                market = exchange.market(symbol)
            except:
                # Fallback for some naming variations if needed
                market = exchange.market(symbol.replace("/", ""))
                
            ohlcv = exchange.fetch_ohlcv(market['symbol'], timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            
            # Apply Advanced Math
            df = calculate_indicators(df)
            
            return df.dropna()
        except Exception as e:
            print(f"⚠️ Fetch Error {symbol}: {e}")
            if attempt < max_retries - 1: time.sleep(5)
    return pd.DataFrame()

# =========================================================================
# === MASTER SIGNAL LOGIC ===
# =========================================================================

def generate_and_send_signal(symbol):
    global bot_stats
    try:
        # 1. Fetch Data (4H for Trend, 1H for Entry)
        df_4h = fetch_data_safe(symbol, TIMEFRAME_MAIN)
        df_1h = fetch_data_safe(symbol, TIMEFRAME_ENTRY)
        
        # 2. Daily Data for Targets
        if not exchange.markets: exchange.load_markets()
        ohlcv_d = exchange.fetch_ohlcv(exchange.market(symbol)['symbol'], '1d', limit=5)
        df_d = pd.DataFrame(ohlcv_d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        cpr = calculate_cpr_levels(df_d)

        if df_4h.empty or df_1h.empty or cpr is None: return

        # 3. Current Market State
        current_price = df_1h.iloc[-1]['close']
        
        # --- LOGIC LAYER 1: 4H MAJOR TREND FILTER ---
        # If Price is above 200 EMA, we ONLY look for BUYS.
        # If Price is below 200 EMA, we ONLY look for SELLS.
        trend_4h_bullish = df_4h.iloc[-1]['close'] > df_4h.iloc[-1]['ema200']
        
        # --- LOGIC LAYER 2: 1H ENTRY TRIGGERS ---
        # We use faster EMAs (21 crossing 50) and RSI for entry
        ema21_1h = df_1h.iloc[-1]['ema21']
        ema50_1h = df_1h.iloc[-1]['ema50']
        rsi_1h = df_1h.iloc[-1]['rsi']
        
        # Determine Signal
        signal = "WAIT"
        emoji = "⏳"
        confidence = "LOW"
        
        # === BUY SCENARIO ===
        if trend_4h_bullish:
            # Check 1H Alignment: Price > EMA50 AND RSI has momentum (>50)
            if current_price > ema50_1h and rsi_1h > 50:
                # Additional Check: Price should be above Daily Pivot
                if current_price > cpr['PP']:
                    signal = "BUY NOW"
                    emoji = "🟢"
                    confidence = "HIGH" if rsi_1h < 70 else "MED" # <70 means room to grow

        # === SELL SCENARIO ===
        elif not trend_4h_bullish:
            # Check 1H Alignment: Price < EMA50 AND RSI has momentum (<50)
            if current_price < ema50_1h and rsi_1h < 50:
                # Additional Check: Price should be below Daily Pivot
                if current_
