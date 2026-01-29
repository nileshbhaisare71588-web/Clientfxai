# main.py - PREMIER FOREX AI QUANT V2.11 (Fixed Pair List)

import os
import ccxt
import pandas as pd
import numpy as np
import requests
import threading
import time
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template_string

# --- CONFIGURATION ---
from dotenv import load_dotenv 
load_dotenv() 

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# UPDATED: Added GBP/JPY and AUD/CAD specifically for you
DEFAULT_PAIRS = "EUR/USD,GBP/USD,USD/JPY,XAU/USD,BTC/USD,GBP/JPY,AUD/CAD"
FOREX_PAIRS = [p.strip() for p in os.getenv("FOREX_PAIRS", DEFAULT_PAIRS).split(',')]
APP_URL = os.getenv("RENDER_EXTERNAL_URL") 

TIMEFRAME_HTF = "4h"
TIMEFRAME_LTF = "1h"

# Initialize Kraken
exchange = ccxt.kraken({
    'enableRateLimit': True, 
    'rateLimit': 2000,
    'params': {'timeout': 20000}
})

bot_stats = {
    "status": "initializing",
    "total_analyses": 0,
    "last_analysis": None,
    "version": "V2.11 Custom Pairs"
}

# =========================================================================
# === TELEGRAM ENGINE (HTTP Direct) ===
# =========================================================================

def send_telegram_message(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"⚠️ Telegram Send Error: {e}")

def send_startup_message():
    # Lists the actual pairs being monitored so you can verify
    pairs_formatted = "\n".join([f"• {p}" for p in FOREX_PAIRS])
    msg = (
        f"🟢 <b>SYSTEM ONLINE: V2.11</b>\n"
        f"───────────────────\n"
        f"<b>Monitoring {len(FOREX_PAIRS)} Assets:</b>\n"
        f"{pairs_formatted}\n"
        f"───────────────────\n"
        f"<i>Sending initial status report now...</i>"
    )
    send_telegram_message(msg)

def send_heartbeat():
    if bot_stats['last_analysis']:
        last_time = datetime.fromisoformat(bot_stats['last_analysis']).strftime("%H:%M")
    else:
        last_time = "Just Started"
    msg = (
        f"💓 <b>SYSTEM HEARTBEAT</b>\n"
        f"Status: 🟢 Running\n"
        f"Last Scan: {last_time} UTC\n"
    )
    send_telegram_message(msg)

# =========================================================================
# === ANALYTICAL ENGINES ===
# =========================================================================

def calculate_atr(df, period=14):
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(period).mean()

def detect_structure(df):
    df['is_high'] = df['high'][(df['high'].shift(1) < df['high']) & (df['high'].shift(-1) < df['high'])]
    df['is_low'] = df['low'][(df['low'].shift(1) > df['low']) & (df['low'].shift(-1) > df['low'])]
    last_highs = df['is_high'].dropna().tail(2)
    last_lows = df['is_low'].dropna().tail(2)
    
    if len(last_highs) < 2 or len(last_lows) < 2: return "NEUTRAL"
    
    if last_highs.iloc[-1] > last_highs.iloc[-2] and last_lows.iloc[-1] > last_lows.iloc[-2]: return "BULLISH"
    elif last_highs.iloc[-1] < last_highs.iloc[-2] and last_lows.iloc[-1] < last_lows.iloc[-2]: return "BEARISH"
    return "NEUTRAL"

def detect_fvg(df):
    recent_data = df.iloc[-6:-1] 
    fvg_zone = None
    fvg_type = None
    for i in range(len(recent_data) - 2):
        curr_high = float(recent_data.iloc[i]['high'])
        next_low = float(recent_data.iloc[i+2]['low'])
        
        if next_low > curr_high:
            fvg_zone = (curr_high, next_low)
            fvg_type = "BULLISH_FVG"
            
        curr_low = float(recent_data.iloc[i]['low'])
        next_high = float(recent_data.iloc[i+2]['high'])
        
        if next_high < curr_low:
            fvg_zone = (next_high, curr_low)
            fvg_type = "BEARISH_FVG"
            
    return fvg_type, fvg_zone

def fetch_data_safe(symbol, timeframe):
    max_retries = 3
    # Helper to fix symbol names if needed
    check_symbol = symbol
    if "BTC" in symbol: check_symbol = "BTC/USD"
    
    for attempt in range(max_retries):
        try:
            if not exchange.markets: exchange.load_markets()
            
            # Check if symbol exists in Kraken, if not try swapping /
            market_id = check_symbol
            if check_symbol in exchange.markets:
                market_id = exchange.market(check_symbol)['id']
                
            ohlcv = exchange.fetch_ohlcv(market_id, timeframe, limit=100)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            return df.dropna()
        except Exception:
            if attempt < max_retries - 1: time.sleep(5)
    return pd.DataFrame()

# =========================================================================
# === MASTER LOGIC ===
# =========================================================================

def generate_and_send_signal(symbol, force_send=False):
    global bot_stats
    try:
        df_htf = fetch_data_safe(symbol, TIMEFRAME_HTF)
        df_ltf = fetch_data_safe(symbol, TIMEFRAME_LTF)
        if df_htf.empty or df_ltf.empty: return

        current_price = float(df_ltf.iloc[-1]['close'])
        structure_htf = detect_structure(df_htf)
        
        df_ltf['atr'] = calculate_atr(df_ltf)
        current_atr = float(df_ltf.iloc[-1]['atr'])
        fvg_type, fvg_zone = detect_fvg(df_ltf)

        signal = "NEUTRAL (WAIT)"
        signal_color = "⚪️"
        
        # --- LOGIC ---
        if structure_htf == "BULLISH" and fvg_type == "BULLISH_FVG":
            signal = "STRONG BUY"
            signal_color = "🟢"
            stop_loss = current_price - (1.5 * current_atr)
            take_profit_1 = current_price + (2.0 * current_atr)
            take_profit_2 = current_price + (3.5 * current_atr)
            
        elif structure_htf == "BEARISH" and fvg_type == "BEARISH_FVG":
            signal = "STRONG SELL"
            signal_color = "🔴"
            stop_loss = current_price + (1.5 * current_atr)
            take_profit_1 = current_price - (2.0 * current_atr)
            take_profit_2 = current_price - (3.5 * current_atr)
            
        else:
            # If not a strong signal, ONLY send if it's the startup check (force_send)
            if not force_send:
                return 
            
            # Generate estimated levels for the status report
            if structure_htf == "BULLISH":
                stop_loss = current_price - (2.0 * current_atr)
                take_profit_1 = current_price + (2.0 * current_atr)
                take_profit_2 = current_price + (3.0 * current_atr)
            else:
                stop_loss = current_price + (2.0 * current_atr)
                take_profit_1 = current_price - (2.0 * current_atr)
                take_profit_2 = current_price - (3.0 * current_atr)

        # Decimals
        if "JPY" in symbol: dec = 3
        elif "BTC" in symbol or "XAU" in symbol: dec = 2
        else: dec = 5
        
        if fvg_zone:
            zone_txt = f"{fvg_zone[0]:.{dec}f} - {fvg_zone[1]:.{dec}f}"
        else:
            zone_txt = "None"

        message = (
            f"<b>💎 PREMIUM QUANT SIGNAL</b>\n"
            f"──────────────────────\n"
            f"<b>🪙 ASSET:</b> #{symbol.replace('/','')}\n"
            f"<b>💵 PRICE:</b> <code>{current_price:.{dec}f}</code>\n"
            f"──────────────────────\n"
            f"<b>👉 DIRECTION: {signal_color} {signal}</b>\n"
            f"──────────────────────\n"
            f"<b>🎯 TP 1:</b> <code>{take_profit_1:.{dec}f}</code>\n"
            f"<b>🚀 TP 2:</b> <code>{take_profit_2:.{dec}f}</code>\n"
            f"<b>🛑 SL:</b>  <code>{stop_loss:.{dec}f}</code>\n"
            f"──────────────────────\n"
            f"<b>📊 CONFLUENCE:</b>\n"
            f"• <b>Trend:</b> {structure_htf}\n"
            f"• <b>Zone:</b> {zone_txt}\n"
        )
        send_telegram_message(message)
        bot_stats['total_analyses'] += 1
        bot_stats['last_analysis'] = datetime.now().isoformat()

    except Exception as e:
        print(f"❌ Analysis failed for {symbol}: {e}")

# =========================================================================
# === RUNNER ===
# =========================================================================

def keep_alive():
    if APP_URL:
        try: requests.get(f"{APP_URL}/health", timeout=5)
        except: pass

def start_bot():
    print(f"🚀 Initializing {bot_stats['version']}...")
    
    # 1. Notify User (and list pairs)
    threading.Thread(target=send_startup_message).start()

    scheduler = BackgroundScheduler()
    
    # 2. Main Signal Scan
    for s in FOREX_PAIRS:
        scheduler.add_job(generate_and_send_signal, 'cron', minute='0,30', args=[s, False])
        
    scheduler.add_job(send_heartbeat, 'interval', hours=4)
    scheduler.add_job(keep_alive, 'interval', minutes=10)
    scheduler.start()
    
    # 3. FORCE SEND ON STARTUP (So you see all pairs working immediately)
    for s in FOREX_PAIRS:
        threading.Thread(target=generate_and_send_signal, args=(s, True)).start()

start_bot()

app = Flask(__name__)

@app.route('/')
def home():
    return render_template_string("<h3>Bot is Running V2.11</h3>")

@app.route('/health')
def health(): return jsonify({"status": "healthy"}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
