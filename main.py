import os
import requests
import pandas as pd
import numpy as np
import asyncio
import time
import traceback
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Bot
from flask import Flask, render_template_string
import threading

# --- CONFIGURATION ---
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TD_API_KEY = os.getenv("TD_API_KEY", "YOUR_API_KEY_HERE")

# --- ASSETS ---
WATCHLIST = [
    "EUR/USD", "GBP/JPY", "AUD/USD", "GBP/USD",
    "XAU/USD", "AUD/CAD", "AUD/JPY", "BTC/USD"
]

TIMEFRAME = "1h"

# Initialize Bot
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# GLOBAL STORAGE FOR WEBSITE DATA
bot_stats = {
    "status": "Initializing...",
    "last_run": "Waiting for first cycle...",
    "version": "V10.0 Pro Interface"
}
latest_signals = {} # Stores the latest analysis for the dashboard

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
        df = df.iloc[::-1] # Oldest first
        df[['open', 'high', 'low', 'close']] = df[['open', 'high', 'low', 'close']].astype(float)
        return add_indicators(df)
    except: return pd.DataFrame()

def calculate_cpr(df):
    try:
        df_daily = df.resample('D').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
        if len(df_daily) < 2: return None
        prev = df_daily.iloc[-2]
        PP = (prev['high'] + prev['low'] + prev['close']) / 3.0
        return {'PP': PP, 'R1': 2*PP - prev['low'], 'S1': 2*PP - prev['high']}
    except: return None

def add_indicators(df):
    if df.empty: return df
    df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
    delta = df['close'].diff()
    up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
    rs = up.ewm(com=13, adjust=False).mean() / down.ewm(com=13, adjust=False).mean()
    df['rsi'] = 100 - (100 / (1 + rs))
    exp1 = df['close'].ewm(span=12, adjust=False).mean()
    exp2 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = exp1 - exp2
    df['signal_line'] = df['macd'].ewm(span=9, adjust=False).mean()
    return df.dropna()

# =========================================================================
# === CORE ANALYSIS LOOP ===
# =========================================================================

async def send_telegram(symbol, msg):
    try: await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='HTML')
    except: pass

def run_analysis_cycle():
    global latest_signals, bot_stats
    print(f"🔄 Cycle Start: {datetime.now().strftime('%H:%M:%S')}")
    bot_stats['last_run'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bot_stats['status'] = "Scanning Markets..."
    
    for symbol in WATCHLIST:
        try:
            df = fetch_data(symbol)
            if df.empty:
                time.sleep(8) 
                continue

            cpr = calculate_cpr(df)
            last = df.iloc[-1]
            price = last['close']
            
            # Logic
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

            # Determine Signal
            signal = "WAIT"
            color = "secondary" # Grey
            if score >= 2.5: signal, color = "STRONG BUY", "success" # Green
            elif 1.0 <= score < 2.5: signal, color = "BUY", "info" # Blue
            elif -2.5 < score <= -1.0: signal, color = "SELL", "warning" # Orange
            elif score <= -2.5: signal, color = "STRONG SELL", "danger" # Red

            # Save to Global Dictionary for Website
            latest_signals[symbol] = {
                "price": f"{price:,.2f}" if "JPY" in symbol or "XAU" in symbol else f"{price:,.4f}",
                "signal": signal,
                "score": score,
                "color": color,
                "trend": "Bullish" if price > last['ema_200'] else "Bearish",
                "rsi": f"{rsi:.1f}",
                "updated": datetime.now().strftime("%H:%M")
            }

            # Only send Telegram if it's a STRONG signal
            if "STRONG" in signal:
                msg = f"🚨 <b>{signal} {symbol}</b>\nPrice: {price}\nRSI: {rsi:.1f}"
                asyncio.run(send_telegram(symbol, msg))

            time.sleep(8) # Respect API limits

        except Exception as e:
            print(f"Error {symbol}: {e}")

    bot_stats['status'] = "Idle (Next Scan in 30m)"

# =========================================================================
# === PROFESSIONAL DASHBOARD UI (HTML/CSS) ===
# =========================================================================

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Nilesh Pro Trader</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
    <style>
        body {
            background-color: #0f172a; /* Dark Navy */
            color: #e2e8f0;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        .navbar {
            background-color: #1e293b;
            border-bottom: 1px solid #334155;
        }
        .card {
            background-color: #1e293b; /* Card BG */
            border: 1px solid #334155;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.5);
            transition: transform 0.2s;
        }
        .card:hover {
            transform: translateY(-5px);
            border-color: #64748b;
        }
        .status-badge {
            font-size: 0.8em;
            padding: 5px 10px;
            border-radius: 20px;
        }
        .price-text {
            font-size: 1.5em;
            font-weight: bold;
            color: #f8fafc;
        }
        .signal-box {
            text-align: center;
            padding: 10px;
            border-radius: 5px;
            font-weight: bold;
            margin-top: 10px;
        }
        /* Custom Colors based on Bootstrap classes */
        .bg-success-subtle { background-color: rgba(22, 163, 74, 0.2) !important; color: #4ade80; }
        .bg-danger-subtle { background-color: rgba(220, 38, 38, 0.2) !important; color: #f87171; }
        .bg-warning-subtle { background-color: rgba(234, 179, 8, 0.2) !important; color: #facc15; }
        
        .refresh-timer {
            font-size: 0.8rem;
            color: #94a3b8;
        }
    </style>
    <script>
        // Auto-refresh page every 30 seconds to show new data
        setTimeout(function(){
           window.location.reload(1);
        }, 30000);
    </script>
</head>
<body>

    <nav class="navbar navbar-dark mb-4">
        <div class="container-fluid">
            <span class="navbar-brand mb-0 h1">
                <i class="fa-solid fa-robot me-2"></i> Nilesh AI Trader <span class="badge bg-primary ms-2">PRO</span>
            </span>
            <span class="text-light small">
                Status: <span class="text-success">{{ stats.status }}</span> | 
                Last Update: {{ stats.last_run }}
            </span>
        </div>
    </nav>

    <div class="container">
        
        <div class="row mb-4">
            <div class="col-md-12 text-center">
                <h4 class="text-light">Live Market Scanner</h4>
                <p class="refresh-timer"><i class="fa-solid fa-sync fa-spin me-1"></i> Auto-refreshing every 30s</p>
            </div>
        </div>

        <div class="row g-4">
            {% for symbol, data in signals.items() %}
            <div class="col-12 col-md-6 col-lg-3">
                <div class="card h-100">
                    <div class="card-header d-flex justify-content-between align-items-center">
                        <span class="fw-bold">{{ symbol }}</span>
                        <span class="badge bg-dark">{{ data.updated }}</span>
                    </div>
                    <div class="card-body">
                        <div class="d-flex justify-content-between align-items-end mb-3">
                            <div>
                                <small class="text-muted">Current Price</small>
                                <div class="price-text">{{ data.price }}</div>
                            </div>
                            <div class="text-end">
                                <small class="text-muted">RSI</small>
                                <div class="fw-bold {{ 'text-danger' if data.rsi|float > 70 else 'text-success' if data.rsi|float < 30 else 'text-light' }}">
                                    {{ data.rsi }}
                                </div>
                            </div>
                        </div>

                        <div class="signal-box bg-{{ data.color }} text-dark bg-opacity-75">
                            {{ data.signal }}
                        </div>

                        <div class="mt-3 d-flex justify-content-between small text-muted">
                            <span>Trend: 
                                <span class="{{ 'text-success' if data.trend == 'Bullish' else 'text-danger' }}">
                                    {{ data.trend }}
                                </span>
                            </span>
                            <span>Score: {{ data.score }}</span>
                        </div>
                    </div>
                </div>
            </div>
            {% else %}
            <div class="col-12 text-center py-5">
                <div class="spinner-border text-primary" role="status"></div>
                <p class="mt-3">Collecting market data... Please wait 30 seconds.</p>
            </div>
            {% endfor %}
        </div>
        
        <div class="row mt-5">
            <div class="col-12 text-center text-muted small">
                <p>Powered by Nilesh System V10 | TwelveData API</p>
            </div>
        </div>

    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

app = Flask(__name__)

@app.route('/')
def home():
    # Pass the global data to the HTML template
    return render_template_string(DASHBOARD_HTML, signals=latest_signals, stats=bot_stats)

def startup():
    asyncio.run(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="🚀 <b>WEB DASHBOARD ONLINE</b>"))
    run_analysis_cycle()

def start_bot():
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_analysis_cycle, 'interval', minutes=30)
    scheduler.start()
    threading.Thread(target=startup).start()

start_bot()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
