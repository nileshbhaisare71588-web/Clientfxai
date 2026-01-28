import os, ccxt, asyncio, time, threading
from telegram import Bot
from flask import Flask

# --- CONFIG ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHAT_ID")
ASSET_LIST = ["EUR/USD", "GBP/JPY", "AUD/USD", "GBP/USD", "XAU/USD", "AUD/CAD", "AUD/JPY", "BTC/USD"]

bot = Bot(token=TOKEN)
exchange = ccxt.kraken({'enableRateLimit': True})
app = Flask(__name__)

@app.route('/')
def home(): 
    return "🚀 Forex Bot is LIVE. Pinging this URL keeps it awake."

async def run_signals():
    print("Checking markets...")
    for symbol in ASSET_LIST:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=25)
            closes = [x[4] for x in ohlcv]
            sma9, sma21 = sum(closes[-9:])/9, sum(closes[-21:])/21
            
            signal = "BUY 🚀" if sma9 > sma21 else "SELL 🔻"
            msg = f"<b>{symbol}</b>: {signal}\nPrice: <code>{closes[-1]}</code>"
            
            await bot.send_message(chat_id=CHANNEL_ID, text=msg, parse_mode='HTML')
            await asyncio.sleep(2) # Prevent Telegram flood
        except Exception as e:
            print(f"Error {symbol}: {e}")

def start_loop():
    """Run the loop in a dedicated async thread."""
    while True:
        asyncio.run(run_signals())
        time.sleep(1800) # 30 Minute Wait

if __name__ == "__main__":
    # 1. Start the trading loop in the background
    threading.Thread(target=start_loop, daemon=True).start()
    
    # 2. Start the web server (Render uses this to keep the app 'Active')
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
