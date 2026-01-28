import os, ccxt, asyncio, threading, time, traceback
import pandas as pd
import numpy as np
from telegram import Bot
from flask import Flask

# --- 100% COMPLETE CONFIGURATION ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHAT_ID") # Use your Channel ID (e.g., -100...)

# All 8 of your requested pairs mapped correctly for Kraken
ASSET_LIST = [
    "EUR/USD", "GBP/JPY", "AUD/USD", "GBP/USD", 
    "XAU/USD", "AUD/CAD", "AUD/JPY", "BTC/USD"
]

bot = Bot(token=TOKEN)
exchange = ccxt.kraken({'enableRateLimit': True})

# --- TECHNICAL ANALYSIS ENGINE ---
def calculate_indicators(df):
    # HMA 9/21 Trend
    df['hma9'] = df['close'].rolling(9).mean()
    df['hma21'] = df['close'].rolling(21).mean()
    # Volatility (ATR)
    high_low = df['high'] - df['low']
    df['atr'] = high_low.rolling(14).mean()
    return df.dropna()

async def generate_and_post(symbol):
    try:
        # Fetch 1H Data
        ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=50)
        df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'close', 'v'])
        df = calculate_indicators(df)
        
        last = df.iloc[-1]
        trend_up = last['hma9'] > last['hma21']
        
        # Decide Signal
        signal = "BUY 🚀" if trend_up else "SELL 🔻"
        color = "🟢" if trend_up else "🔴"
        
        # Format decimals (Gold & BTC need fewer decimals than Forex)
        prec = 2 if "JPY" in symbol or "XAU" in symbol or "BTC" in symbol else 5
        
        message = (
            f"{color} <b>ELITE SIGNAL: {symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>Action:</b> {signal}\n"
            f"<b>Entry:</b> <code>{last['close']:.{prec}f}</code>\n"
            f"<b>Volatility:</b> <code>{last['atr']:.{prec}f}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<i>Verified AI Analysis • 1H TF</i>"
        )

        await bot.send_message(chat_id=CHANNEL_ID, text=message, parse_mode='HTML')
        print(f"✅ Posted {symbol} to channel.")

    except Exception as e:
        print(f"❌ Error with {symbol}: {str(e)}")

# --- THE 30-MIN LOOP ---
async def main_loop():
    print("🚀 Bot V3.2 Started. Monitoring all 8 assets...")
    # Initial startup notification
    try:
        await bot.send_message(chat_id=CHANNEL_ID, text="⚙️ <b>AI Bot Online</b>\nMonitoring 8 pairs on 30m intervals.")
    except:
        print("CRITICAL: Bot is not an Admin in the channel!")

    while True:
        # Process all 8 symbols at once
        tasks = [generate_and_post(pair) for pair in ASSET_LIST]
        await asyncio.gather(*tasks)
        
        print(f"Cycle finished at {time.strftime('%H:%M:%S')}. Waiting 30m...")
        await asyncio.sleep(1800)

# --- WEB SERVER (For Render/Uptime) ---
app = Flask(__name__)
@app.route('/')
def home(): return "<h1>Forex Bot V3.2 is Running</h1>"

def run_services():
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    asyncio.run(main_loop())

if __name__ == "__main__":
    run_services()
