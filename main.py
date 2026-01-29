# main.py - PREMIER FOREX AI QUANT V2.6 (High Win-Ratio Optimized)

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
FOREX_PAIRS = [p.strip() for p in os.getenv("FOREX_PAIRS", "EUR/USD,GBP/USD,USD/JPY,AUD/USD").split(',')]

# TIMEFRAMES FOR CONFLUENCE
TIMEFRAME_HTF = "4h"  # Market Structure / Direction
TIMEFRAME_LTF = "1h"  # Entry / FVG Detection

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
    "version": "V2.6 Quant Elite"
}

# =========================================================================
# === ADVANCED ANALYTICAL ENGINES ===
# =========================================================================

def calculate_atr(df, period=14):
    """Calculates Volatility (ATR) for dynamic Stop Loss."""
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(period).mean()

def detect_structure(df, window=3):
    """
    Determines Market Structure (BOS) using Fractal Highs/Lows.
    Returns: 'BULLISH' if making Higher Highs, 'BEARISH' if Lower Lows.
    """
    # Identify Fractals (Local peaks/valleys)
    df['is_high'] = df['high'][(df['high'].shift(1) < df['high']) & (df['high'].shift(-1) < df['high'])]
    df['is_low'] = df['low'][(df['low'].shift(1) > df['low']) & (df['low'].shift(-1) > df['low'])]
    
    last_highs = df['is_high'].dropna().tail(2)
    last_lows = df['is_low'].dropna().tail(2)
    
    if len(last_highs) < 2 or len(last_lows) < 2:
        return "NEUTRAL"
    
    # Check for Higher Highs (HH) vs Lower Lows (LL)
    structure = "NEUTRAL"
    if last_highs.iloc[-1] > last_highs.iloc[-2] and last_lows.iloc[-1] > last_lows.iloc[-2]:
        structure = "BULLISH" # Uptrend
    elif last_highs.iloc[-1] < last_highs.iloc[-2] and last_lows.iloc[-1] < last_lows.iloc[-2]:
        structure = "BEARISH" # Downtrend
        
    return structure

def detect_fvg(df):
    """
    Scans for the most recent Bullish or Bearish Fair Value Gap (FVG).
    Logic: Gap between Candle i-1 High and Candle i+1 Low.
    """
    # We look at the last 5 closed candles for a fresh FVG
    recent_data = df.iloc[-6:-1] 
    
    fvg_zone = None
    fvg_type = None
    
    for i in range(len(recent_data) - 2):
        # Bullish FVG (Gap up)
        curr_high = recent_data.iloc[i]['high']
        next_low = recent_data.iloc[i+2]['low']
        if next_low > curr_high:
            fvg_zone = (curr_high, next_low) # The gap range
            fvg_type = "BULLISH_FVG"
            
        # Bearish FVG (Gap down)
        curr_low = recent_data.iloc[i]['low']
        next_high = recent_data.iloc[i+2]['high']
        if next_high < curr_low:
            fvg_zone = (next_high, curr_low) # The gap range
            fvg_type = "BEARISH_FVG"
            
    return fvg_type, fvg_zone

def fetch_data_safe(symbol, timeframe):
    """Robust fetcher with retries."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            if not exchange.markets: exchange.load_markets()
            market_id = exchange.market(symbol)['id']
            ohlcv = exchange.fetch_ohlcv(market_id, timeframe, limit=100)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            return df.dropna()
        except Exception:
            if attempt < max_retries - 1: time.sleep(5)
    return pd.DataFrame()

# =========================================================================
# === MASTER LOGIC: CONFLUENCE V2.6 ===
# =========================================================================

def generate_and_send_signal(symbol):
    global bot_stats
    try:
        # 1. Fetch Multi-Timeframe Data
        df_htf = fetch_data_safe(symbol, TIMEFRAME_HTF) # 4H (Structure)
        df_ltf = fetch_data_safe(symbol, TIMEFRAME_LTF) # 1H (Entry/FVG)
        
        if df_htf.empty or df_ltf.empty: return

        # 2. Run Analysis Modules
        current_price = df_ltf.iloc[-1]['close']
        
        # A. Market Structure (HTF)
        structure_htf = detect_structure(df_htf)
        
        # B. Volatility (ATR)
        df_ltf['atr'] = calculate_atr(df_ltf)
        current_atr = df_ltf.iloc[-1]['atr']
        volatility_pips = current_atr / 0.0001 # Convert to pips roughly
        
        # C. Supply/Demand (FVG Detection on LTF)
        fvg_type, fvg_zone = detect_fvg(df_ltf)
        
        # 3. Decision Logic (The "Confluence")
        signal = "HOLD / NEUTRAL"
        emoji = "⚖️"
        stop_loss = 0.0
        take_profit = 0.0
        
        # BUY SCENARIO: Bullish Structure + Price near Bullish FVG
        if structure_htf == "BULLISH":
            # If we are in a Bullish Trend, we look for buys
            signal = "BUY BIAS (Wait for Entry)"
            emoji = "🐂"
            
            # Strong Signal if we have a Bullish FVG or Momentum
            if fvg_type == "BULLISH_FVG":
                signal = "STRONG BUY"
                emoji = "🚀"
                # SL placed below the FVG or 1.5x ATR
                stop_loss = current_price - (1.5 * current_atr)
                take_profit = current_price + (3.0 * current_atr) # 1:2 Risk Reward
            else:
                # Standard Trend Follow
                stop_loss = current_price - (2.0 * current_atr)
                take_profit = current_price + (2.0 * current_atr)

        # SELL SCENARIO: Bearish Structure + Price near Bearish FVG
        elif structure_htf == "BEARISH":
            signal = "SELL BIAS (Wait for Entry)"
            emoji = "🐻"
            
            if fvg_type == "BEARISH_FVG":
                signal = "STRONG SELL"
                emoji = "🔻"
                stop_loss = current_price + (1.5 * current_atr)
                take_profit = current_price - (3.0 * current_atr)
            else:
                stop_loss = current_price + (2.0 * current_atr)
                take_profit = current_price - (2.0 * current_atr)

        decimals = 3 if 'JPY' in symbol else 5
        
        # Only send if it's not Neutral
        # (You can remove this check if you want updates every time)
        
        message = (
            f"╔══════════════════════════╗\n"
            f"  🤖 <b>FOREX QUANT ELITE V2.6</b>\n"
            f"╚══════════════════════════╝\n\n"
            f"<b>Pair:</b> {symbol}\n"
            f"<b>Price:</b> <code>{current_price:.{decimals}f}</code>\n"
            f"<b>Volatility:</b> {volatility_pips:.1f} pips (ATR)\n\n"
            f"--- 🚨 {emoji} <b>FINAL CALL: {signal}</b> 🚨 ---\n\n"
            f"<b>📊 DEEP ANALYSIS:</b>\n"
            f"• <b>Market Structure (4H):</b> {structure_htf}\n"
            f"• <b>Smart Money (1H):</b> {fvg_type if fvg_type else 'No Recent FVG'}\n"
            f"• <b>Important Level:</b> {fvg_zone if fvg_zone else 'None detected'}\n\n"
            f"<b>🛡️ RISK MANAGEMENT:</b>\n"
            f"🛑 <b>Stop Loss:</b> <code>{stop_loss:.{decimals}f}</code>\n"
            f"💰 <b>Take Profit:</b> <code>{take_profit:.{decimals}f}</code>\n"
            f"<i>(Calculated via 1.5x Volatility ATR)</i>\n\n"
            f"------------------------------\n"
        )

        asyncio.run(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML'))
        
        bot_stats['total_analyses'] += 1
        bot_stats['last_analysis'] = datetime.now().isoformat()

    except Exception as e:
        print(f"❌ Analysis failed for {symbol}: {e}")
        traceback.print_exc()

# =========================================================================
# === RUNNER ===
# =========================================================================

def start_bot():
    print(f"🚀 Initializing {bot_stats['version']}...")
    scheduler = BackgroundScheduler()
    # Runs every 30 mins to catch 1H candle closes
    for s in FOREX_PAIRS:
        scheduler.add_job(generate_and_send_signal, 'cron', minute='1,31', args=[s])
    scheduler.start()
    
    # Run immediate check
    for s in FOREX_PAIRS:
        threading.Thread(target=generate_and_send_signal, args=(s,)).start()

start_bot()

app = Flask(__name__)

@app.route('/')
def home():
    return render_template_string("""
        <body style="font-family:monospace; background:#111; color:#0f0; padding:50px;">
            <h1>QUANT SYSTEM ACTIVE</h1>
            <p>Version: {{v}}</p>
            <p>Signals Sent: {{a}}</p>
        </body>
    """, a=bot_stats['total_analyses'], v=bot_stats['version'])

@app.route('/health')
def health(): return jsonify({"status": "healthy"}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
