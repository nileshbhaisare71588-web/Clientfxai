import os, ccxt, asyncio, threading, time, traceback
import pandas as pd
import numpy as np
from datetime import datetime
from dotenv import load_dotenv
from telegram import Bot
from flask import Flask
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler

load_dotenv()

# --- UPDATED CONFIGURATION ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Kraken-specific symbol mapping for your requested pairs
# Note: Kraken uses 'XBT' for Bitcoin and 'XAU' for Gold
ASSET_LIST = [
    "EUR/USD", "GBP/JPY", "AUD/USD", "GBP/USD", 
    "XAU/USD", "AUD/CAD", "AUD/JPY", "XBT/USD"
]

# --- TECHNICAL ENGINE (HMA & ADX) ---
def calculate_hma(series, period):
    wma_half = series.rolling(period // 2).apply(lambda x: np.dot(x, np.arange(1, period // 2 + 1)) / np.arange(1, period // 2 + 1).sum(), raw=True)
    wma_full = series.rolling(period).apply(lambda x: np.dot(x, np.arange(1, period + 1)) / np.arange(1, period + 1).sum(), raw=True)
    diff = 2 * wma_half - wma_full
    return diff.rolling(int(np.sqrt(period))).apply(lambda x: np.dot(x, np.arange(1, int(np.sqrt(period)) + 1)) / np.arange(1, int(np.sqrt(period)) + 1).sum(), raw=True)

def calculate_indicators(df):
    df['hma9'] = calculate_hma(df['close'], 9)
    df['hma21'] = calculate_hma(df['close'], 21)
    
    # ATR for Dynamic Risk
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    df['atr'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(14).mean()
    
    # ADX for Trend Strength
    plus_dm = df['high'].diff().clip(lower=0)
    minus_dm = df['low'].diff().clip(upper=0).abs()
    tr14 = df['atr'] * 14 # Simplified TR sum
    df['adx'] = ((np.abs(plus_dm - minus_dm) / (plus_dm + minus_dm)) * 100).rolling(14).mean()
    
    return df.dropna()

# --- SIGNAL & ML LOGIC ---
exchange = ccxt.kraken({'enableRateLimit': True})
bot = Bot(token=TELEGRAM_BOT_TOKEN)

async def generate_elite_signal(symbol):
    try:
        # Fetch 1H candles (Precision entry)
        ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=100)
        df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df = calculate_indicators(df)
        
        last = df.iloc[-1]
        trend_up = last['hma9'] > last['hma21']
        strong_market = last['adx'] > 20 # Filter out dead markets
        
        # Risk Settings
        decimals = 2 if "JPY" in symbol or "XBT" in symbol or "XAU" in symbol else 5
        sl_val = last['atr'] * 1.5
        tp_val = last['atr'] * 3.0

        signal_type = None
        if trend_up and strong_market: signal_type = "🚀 ELITE BUY"
        elif not trend_up and strong_market: signal_type = "🔻 ELITE SELL"

        if signal_type:
            sl = last['close'] - sl_val if "BUY" in signal_type else last['close'] + sl_val
            tp = last['close'] + tp_val if "BUY" in signal_type else last['close'] - tp_val
            
            category = "CRYPTO" if "XBT" in symbol else ("METAL" if "XAU" in symbol else "FOREX")
            
            message = (
                f"🌟 <b>{signal_type}</b> | <code>{category}</code>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"<b>Asset:</b> {symbol}\n"
                f"<b>Price:</b> <code>{last['close']:.{decimals}f}</code>\n"
                f"<b>Trend Power:</b> {last['adx']:.1f}\n\n"
                f"🎯 <b>Take Profit:</b> <code>{tp:.{decimals}f}</code>\n"
                f"🛑 <b>Stop Loss:</b> <code>{sl:.{decimals}f}</code>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"<i>V3.1 Elite • Sent every 30m</i>"
            )
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML')

    except Exception:
        print(f"Error processing {symbol}: {traceback.format_exc()}")

# --- 30-MINUTE SCHEDULER ---
async def main_loop():
    print("🤖 Bot started. Monitoring 8 assets on 30m intervals...")
    while True:
        # Run all pairs concurrently for speed
        tasks = [generate_elite_signal(pair) for pair in ASSET_LIST]
        await asyncio.gather(*tasks)
        
        # Sleep for 30 minutes
        print(f"Cycle complete at {datetime.now()}. Sleeping 30m...")
        await asyncio.sleep(1800)

app = Flask(__name__)
@app.route('/')
def health(): return "AI Bot V3.1: Active"

def start_services():
    # Start the Flask server in a thread (for Render/Heroku)
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    # Start the async loop
    asyncio.run(main_loop())

if __name__ == "__main__":
    start_services()
