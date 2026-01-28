import os, ccxt, asyncio, time
from telegram import Bot

# --- CONFIG ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHAT_ID")

# All 8 of your pairs
ASSET_LIST = [
    "EUR/USD", "GBP/JPY", "AUD/USD", "GBP/USD", 
    "XAU/USD", "AUD/CAD", "AUD/JPY", "BTC/USD"
]

bot = Bot(token=TOKEN)
exchange = ccxt.kraken({'enableRateLimit': True})

async def send_signals():
    print(f"🚀 Starting Scan for {len(ASSET_LIST)} pairs...")
    for symbol in ASSET_LIST:
        try:
            # Simple logic to ensure it doesn't crash
            ohlcv = exchange.fetch_ohlcv(symbol, '1h', limit=25)
            closes = [x[4] for x in ohlcv]
            sma9 = sum(closes[-9:])/9
            sma21 = sum(closes[-21:])/21
            
            signal = "BUY 🚀" if sma9 > sma21 else "SELL 🔻"
            msg = f"<b>{symbol}</b>: {signal}\nPrice: <code>{closes[-1]}</code>"
            
            await bot.send_message(chat_id=CHANNEL_ID, text=msg, parse_mode='HTML')
            print(f"✅ Sent: {symbol}")
            await asyncio.sleep(1) # Small delay
        except Exception as e:
            print(f"❌ Error {symbol}: {e}")

async def main():
    while True:
        await send_signals()
        print("Waiting 30 minutes for next scan...")
        await asyncio.sleep(1800)

if __name__ == "__main__":
    asyncio.run(main())
