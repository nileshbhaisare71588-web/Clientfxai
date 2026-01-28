import os, ccxt, asyncio, threading, time
from telegram import Bot
from flask import Flask

# --- CONFIG ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHAT_ID")

ASSET_LIST = [
    "EUR/USD", "GBP/JPY", "AUD/USD", "GBP/USD", 
    "XAU/USD", "AUD/CAD", "AUD/JPY", "BTC/USD"
]

# Initialize
bot = Bot(token=TOKEN)
exchange = ccxt.kraken({'enableRateLimit': True})

app = Flask(__name__)

@app.route('/')
def health(): return "Bot is Alive"

async def send_signal(symbol):
    try:
        # Fetch data (Simplified to avoid timeout)
        ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=30)
        closes = [x[4] for x in ohlcv]
        
        # Fast Moving Average Calculation
        sma9 = sum(closes[-9:]) / 9
        sma21 = sum(closes[-21:]) / 21
        current_price = closes[-1]
        
        signal = "BUY 🚀" if sma9 > sma21 else "SELL 🔻"
        emoji = "🟢" if "BUY" in signal else "🔴"
        
        msg = (
            f"{emoji} <b>SIGNAL: {symbol}</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"<b>Action:</b> {signal}\n"
            f"<b>Price:</b> <code>{current_price}</code>\n"
            f"━━━━━━━━━━━━━━━"
        )
        await bot.send_message(chat_id=CHANNEL_ID, text=msg, parse_mode='HTML')
        print(f"Done: {symbol}")
    except Exception as e:
        print(f"Error {symbol}: {e}")

async def bot_loop():
    # Give the web server 10 seconds to start first so Render is happy
    await asyncio.sleep(10) 
    while True:
        print("Starting Scan...")
        for pair in ASSET_LIST:
            await send_signal(pair)
            await asyncio.sleep(2) # Small delay to avoid Telegram spam limits
        print("Scan finished. Sleeping 30m.")
        await asyncio.sleep(1800)

def run_async_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot_loop())

if __name__ == "__main__":
    # Start the background loop
    threading.Thread(target=run_async_loop, daemon=True).start()
    # Start the Flask web server
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
