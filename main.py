import os
import ccxt
import pandas as pd
import numpy as np
import asyncio
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Bot
from flask import Flask, jsonify, render_template_string
import threading
import time
import traceback
import warnings
warnings.filterwarnings('ignore')

# --- ML Imports ---
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
import joblib

# --- Technical Analysis ---
import talib

# --- CONFIGURATION ---
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Your specified pairs
FOREX_PAIRS = [
    "EUR/USD", "GBP/JPY", "AUD/USD", "GBP/USD", 
    "XAU/USD", "AUD/CAD", "AUD/JPY", "BTC/USD"
]

TIMEFRAME_MAIN = "4h"  # Major Trend
TIMEFRAME_ENTRY = "1h"  # Entry Precision
TIMEFRAME_DAILY = "1d"  # CPR Calculation

# Signal Thresholds
SIGNAL_THRESHOLD_STRONG = 75  # Strong signal confidence
SIGNAL_THRESHOLD_MEDIUM = 60  # Medium signal confidence
MIN_RISK_REWARD = 2.0  # Minimum R:R ratio

# Initialize Bot and Exchange
bot = Bot(token=TELEGRAM_BOT_TOKEN)
exchange = ccxt.kraken({
    'enableRateLimit': True,
    'rateLimit': 2000,
    'params': {'timeout': 20000}
})

# Performance Tracking
performance_tracker = {pair: {
    'total_signals': 0,
    'win_count': 0,
    'loss_count': 0,
    'win_rate': 0.0,
    'last_signal': None,
    'signal_history': []
} for pair in FOREX_PAIRS}

bot_stats = {
    "status": "initializing",
    "total_analyses": 0,
    "last_analysis": None,
    "monitored_assets": FOREX_PAIRS,
    "uptime_start": datetime.now().isoformat(),
    "version": "V3.0 Elite Quant Edition"
}

# ML Model Storage
ml_models = {}
scalers = {}

# =========================================================================
# === ADVANCED TECHNICAL ANALYSIS ENGINE ===
# =========================================================================

def calculate_advanced_indicators(df):
    """Calculate comprehensive technical indicators using TA-Lib"""
    if len(df) < 50:
        return df
    
    try:
        close = df['close'].values
        high = df['high'].values
        low = df['low'].values
        volume = df['volume'].values
        
        # Moving Averages
        df['sma9'] = talib.SMA(close, timeperiod=9)
        df['sma20'] = talib.SMA(close, timeperiod=20)
        df['sma50'] = talib.SMA(close, timeperiod=50)
        df['ema9'] = talib.EMA(close, timeperiod=9)
        df['ema21'] = talib.EMA(close, timeperiod=21)
        
        # RSI
        df['rsi'] = talib.RSI(close, timeperiod=14)
        
        # MACD
        df['macd'], df['macd_signal'], df['macd_hist'] = talib.MACD(
            close, fastperiod=12, slowperiod=26, signalperiod=9
        )
        
        # Bollinger Bands
        df['bb_upper'], df['bb_middle'], df['bb_lower'] = talib.BBANDS(
            close, timeperiod=20, nbdevup=2, nbdevdn=2
        )
        
        # ATR (Volatility)
        df['atr'] = talib.ATR(high, low, close, timeperiod=14)
        
        # Stochastic
        df['stoch_k'], df['stoch_d'] = talib.STOCH(
            high, low, close, 
            fastk_period=14, slowk_period=3, slowd_period=3
        )
        
        # ADX (Trend Strength)
        df['adx'] = talib.ADX(high, low, close, timeperiod=14)
        
        # OBV (Volume)
        df['obv'] = talib.OBV(close, volume)
        
        # CCI (Commodity Channel Index)
        df['cci'] = talib.CCI(high, low, close, timeperiod=14)
        
        # Williams %R
        df['willr'] = talib.WILLR(high, low, close, timeperiod=14)
        
        # Parabolic SAR
        df['sar'] = talib.SAR(high, low, acceleration=0.02, maximum=0.2)
        
        return df.dropna()
        
    except Exception as e:
        print(f"⚠️ Indicator calculation error: {e}")
        return df

def calculate_cpr_levels(df_daily):
    """Enhanced CPR with additional pivot levels"""
    if df_daily.empty or len(df_daily) < 2:
        return None
    
    try:
        prev_day = df_daily.iloc[-2]
        H, L, C = prev_day['high'], prev_day['low'], prev_day['close']
        
        # Standard Pivot
        PP = (H + L + C) / 3.0
        BC = (H + L) / 2.0
        TC = PP - BC + PP
        
        # Support and Resistance
        R1 = 2 * PP - L
        S1 = 2 * PP - H
        R2 = PP + (H - L)
        S2 = PP - (H - L)
        R3 = H + 2 * (PP - L)
        S3 = L - 2 * (H - PP)
        
        return {
            'PP': PP, 'TC': TC, 'BC': BC,
            'R1': R1, 'S1': S1,
            'R2': R2, 'S2': S2,
            'R3': R3, 'S3': S3
        }
    except Exception as e:
        print(f"⚠️ CPR calculation error: {e}")
        return None

def fetch_data_safe(symbol, timeframe, limit=200):
    """Enhanced data fetcher with better error handling"""
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            if not exchange.markets:
                exchange.load_markets()
            
            market_id = exchange.market(symbol)['id']
            ohlcv = exchange.fetch_ohlcv(market_id, timeframe, limit=limit)
            
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            
            # Add all advanced indicators
            df = calculate_advanced_indicators(df)
            
            return df
            
        except Exception as e:
            print(f"⚠️ Data fetch attempt {attempt + 1}/{max_retries} failed for {symbol}: {e}")
            if attempt < max_retries - 1:
                time.sleep(5)
    
    return pd.DataFrame()

# =========================================================================
# === MULTI-INDICATOR CONFLUENCE SCORING ===
# =========================================================================

def calculate_signal_score(df_4h, df_1h, cpr, price, symbol):
    """
    Advanced scoring system (0-100) based on multiple indicator confluence
    Higher score = Higher confidence signal
    """
    score = 0
    signals = {
        'trend': 0,
        'momentum': 0,
        'volatility': 0,
        'volume': 0,
        'support_resistance': 0
    }
    
    try:
        row_4h = df_4h.iloc[-1]
        row_1h = df_1h.iloc[-1]
        
        # === 1. TREND ANALYSIS (25 points) ===
        # Multi-timeframe moving average alignment
        if row_4h['ema9'] > row_4h['ema21'] > row_4h['sma50'] and row_1h['ema9'] > row_1h['ema21']:
            signals['trend'] = 25  # Strong bullish
        elif row_4h['ema9'] < row_4h['ema21'] < row_4h['sma50'] and row_1h['ema9'] < row_1h['ema21']:
            signals['trend'] = -25  # Strong bearish
        elif row_4h['ema9'] > row_4h['ema21'] and row_1h['ema9'] > row_1h['ema21']:
            signals['trend'] = 15  # Medium bullish
        elif row_4h['ema9'] < row_4h['ema21'] and row_1h['ema9'] < row_1h['ema21']:
            signals['trend'] = -15  # Medium bearish
        
        # ADX trend strength confirmation
        if row_4h['adx'] > 25:  # Strong trend
            signals['trend'] = signals['trend'] * 1.2 if signals['trend'] != 0 else signals['trend']
        
        # === 2. MOMENTUM ANALYSIS (25 points) ===
        momentum_score = 0
        
        # RSI analysis
        if 30 < row_1h['rsi'] < 40:  # Oversold but recovering
            momentum_score += 8
        elif 60 < row_1h['rsi'] < 70:  # Overbought but strong
            momentum_score -= 8
        elif row_1h['rsi'] < 30:  # Extremely oversold
            momentum_score += 12
        elif row_1h['rsi'] > 70:  # Extremely overbought
            momentum_score -= 12
        
        # MACD analysis
        if row_1h['macd'] > row_1h['macd_signal'] and row_1h['macd_hist'] > 0:
            momentum_score += 8
        elif row_1h['macd'] < row_1h['macd_signal'] and row_1h['macd_hist'] < 0:
            momentum_score -= 8
        
        # Stochastic analysis
        if row_1h['stoch_k'] > row_1h['stoch_d'] and row_1h['stoch_k'] < 80:
            momentum_score += 5
        elif row_1h['stoch_k'] < row_1h['stoch_d'] and row_1h['stoch_k'] > 20:
            momentum_score -= 5
        
        # CCI analysis
        if -100 < row_1h['cci'] < 0:
            momentum_score += 4
        elif 0 < row_1h['cci'] < 100:
            momentum_score -= 4
        
        signals['momentum'] = momentum_score
        
        # === 3. VOLATILITY & PRICE POSITION (20 points) ===
        volatility_score = 0
        
        # Bollinger Bands position
        bb_position = (price - row_1h['bb_lower']) / (row_1h['bb_upper'] - row_1h['bb_lower'])
        if bb_position < 0.3:  # Near lower band
            volatility_score += 10
        elif bb_position > 0.7:  # Near upper band
            volatility_score -= 10
        
        # Parabolic SAR
        if price > row_1h['sar']:
            volatility_score += 5
        else:
            volatility_score -= 5
        
        # Williams %R
        if row_1h['willr'] > -20:  # Overbought
            volatility_score -= 5
        elif row_1h['willr'] < -80:  # Oversold
            volatility_score += 5
        
        signals['volatility'] = volatility_score
        
        # === 4. VOLUME CONFIRMATION (15 points) ===
        volume_score = 0
        
        # OBV trend
        if len(df_1h) >= 5:
            obv_trend = df_1h['obv'].iloc[-1] > df_1h['obv'].iloc[-5]
            if obv_trend and signals['trend'] > 0:
                volume_score += 15
            elif not obv_trend and signals['trend'] < 0:
                volume_score -= 15
            else:
                volume_score += 5  # Neutral
        
        signals['volume'] = volume_score
        
        # === 5. SUPPORT/RESISTANCE & CPR (15 points) ===
        sr_score = 0
        
        if cpr:
            # Price above pivot = bullish bias
            if price > cpr['PP']:
                sr_score += 5
                if price > cpr['R1']:
                    sr_score += 5
            else:
                sr_score -= 5
                if price < cpr['S1']:
                    sr_score -= 5
            
            # CPR width analysis (narrow CPR = strong trending day expected)
            cpr_width = abs(cpr['TC'] - cpr['BC'])
            avg_range = (df_4h['high'] - df_4h['low']).mean()
            if cpr_width < avg_range * 0.3:  # Narrow CPR
                sr_score += 5
        
        signals['support_resistance'] = sr_score
        
        # === CALCULATE TOTAL SCORE ===
        total_score = sum(signals.values())
        
        # Normalize to 0-100 scale and get direction
        if total_score > 0:
            score = min(100, abs(total_score))
            direction = "BUY"
        else:
            score = min(100, abs(total_score))
            direction = "SELL"
        
        return score, direction, signals
        
    except Exception as e:
        print(f"⚠️ Scoring error for {symbol}: {e}")
        return 0, "HOLD", signals

# =========================================================================
# === DYNAMIC RISK MANAGEMENT ===
# =========================================================================

def calculate_dynamic_targets(df, price, direction, cpr, atr_multiplier=2.0):
    """
    Calculate dynamic SL/TP based on ATR and CPR levels
    """
    try:
        atr = df.iloc[-1]['atr']
        
        if direction == "BUY":
            # Stop Loss: Price - (ATR * multiplier) or CPR support
            sl_atr = price - (atr * atr_multiplier)
            sl_cpr = min(cpr['BC'], cpr['S1']) if cpr else sl_atr
            stop_loss = max(sl_atr, sl_cpr)  # Use tighter stop
            
            # Take Profit: ATR-based + CPR resistance levels
            tp1 = price + (atr * 2)
            tp2 = price + (atr * 3)
            tp1 = min(tp1, cpr['R1']) if cpr else tp1
            tp2 = min(tp2, cpr['R2']) if cpr else tp2
            
        else:  # SELL
            # Stop Loss: Price + (ATR * multiplier) or CPR resistance
            sl_atr = price + (atr * atr_multiplier)
            sl_cpr = max(cpr['TC'], cpr['R1']) if cpr else sl_atr
            stop_loss = min(sl_atr, sl_cpr)  # Use tighter stop
            
            # Take Profit: ATR-based + CPR support levels
            tp1 = price - (atr * 2)
            tp2 = price - (atr * 3)
            tp1 = max(tp1, cpr['S1']) if cpr else tp1
            tp2 = max(tp2, cpr['S2']) if cpr else tp2
        
        # Calculate Risk:Reward Ratio
        risk = abs(price - stop_loss)
        reward1 = abs(tp1 - price)
        reward2 = abs(tp2 - price)
        
        rr_ratio1 = reward1 / risk if risk > 0 else 0
        rr_ratio2 = reward2 / risk if risk > 0 else 0
        
        return {
            'sl': stop_loss,
            'tp1': tp1,
            'tp2': tp2,
            'atr': atr,
            'rr_ratio1': rr_ratio1,
            'rr_ratio2': rr_ratio2,
            'risk_amount': risk
        }
        
    except Exception as e:
        print(f"⚠️ Risk calculation error: {e}")
        return None

# =========================================================================
# === ML-ENHANCED PREDICTION (OPTIONAL UPGRADE) ===
# =========================================================================

def prepare_ml_features(df_4h, df_1h):
    """Prepare features for ML model"""
    try:
        features = []
        
        row_4h = df_4h.iloc[-1]
        row_1h = df_1h.iloc[-1]
        
        # Feature engineering
        features.extend([
            row_1h['rsi'],
            row_1h['macd_hist'],
            row_1h['stoch_k'],
            row_1h['cci'],
            row_1h['adx'],
            row_4h['rsi'],
            row_4h['adx'],
            (row_1h['close'] - row_1h['sma20']) / row_1h['sma20'],  # Price deviation
            (row_1h['ema9'] - row_1h['ema21']) / row_1h['ema21'],  # MA spread
        ])
        
        return np.array(features).reshape(1, -1)
        
    except Exception as e:
        print(f"⚠️ ML feature preparation error: {e}")
        return None

def get_ml_prediction(symbol, df_4h, df_1h):
    """Get ML model prediction (placeholder for now)"""
    try:
        # This is a simplified version - in production you'd train on historical data
        features = prepare_ml_features(df_4h, df_1h)
        
        if features is None:
            return 50  # Neutral confidence
        
        # For now, return moderate confidence
        # In production: return model.predict_proba(features)[0][1] * 100
        return 50
        
    except Exception as e:
        return 50

# =========================================================================
# === MAIN SIGNAL GENERATION LOGIC ===
# =========================================================================

def generate_and_send_signal(symbol):
    """Enhanced signal generation with multi-indicator confluence"""
    global bot_stats, performance_tracker
    
    try:
        print(f"\n🔍 Analyzing {symbol}...")
        
        # 1. Fetch multi-timeframe data
        df_4h = fetch_data_safe(symbol, TIMEFRAME_MAIN, limit=200)
        df_1h = fetch_data_safe(symbol, TIMEFRAME_ENTRY, limit=200)
        
        if df_4h.empty or df_1h.empty or len(df_4h) < 50 or len(df_1h) < 50:
            print(f"⚠️ Insufficient data for {symbol}")
            return
        
        # 2. Get daily data for CPR
        if not exchange.markets:
            exchange.load_markets()
        market_id = exchange.market(symbol)['id']
        ohlcv_d = exchange.fetch_ohlcv(market_id, '1d', limit=10)
        df_d = pd.DataFrame(ohlcv_d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        cpr = calculate_cpr_levels(df_d)
        
        # 3. Get current price
        price = df_1h.iloc[-1]['close']
        
        # 4. Calculate multi-indicator confluence score
        score, direction, indicator_signals = calculate_signal_score(df_4h, df_1h, cpr, price, symbol)
        
        # 5. Calculate dynamic risk management
        risk_data = calculate_dynamic_targets(df_1h, price, direction, cpr)
        
        if risk_data is None:
            print(f"⚠️ Risk calculation failed for {symbol}")
            return
        
        # 6. Determine signal strength
        if score >= SIGNAL_THRESHOLD_STRONG and risk_data['rr_ratio1'] >= MIN_RISK_REWARD:
            signal_strength = "VERY STRONG"
            emoji = "🔥🚀" if direction == "BUY" else "🔥🔻"
        elif score >= SIGNAL_THRESHOLD_MEDIUM and risk_data['rr_ratio1'] >= 1.5:
            signal_strength = "STRONG"
            emoji = "🚀" if direction == "BUY" else "🔻"
        elif score >= 50:
            signal_strength = "MEDIUM"
            emoji = "📊" if direction == "BUY" else "📉"
        else:
            signal_strength = "WEAK - HOLD"
            emoji = "⏳"
            direction = "HOLD"
        
        # 7. Only send strong signals
        if score < 50 or risk_data['rr_ratio1'] < 1.5:
            print(f"❌ Signal too weak for {symbol} (Score: {score:.1f})")
            return
        
        # 8. Format decimal places
        decimals = 5 if 'JPY' not in symbol else 3
        if 'XAU' in symbol or 'BTC' in symbol:
            decimals = 2
        
        # 9. Get additional context
        trend_4h = "BULLISH 📈" if df_4h.iloc[-1]['ema9'] > df_4h.iloc[-1]['ema21'] else "BEARISH 📉"
        trend_1h = "BULLISH 📈" if df_1h.iloc[-1]['ema9'] > df_1h.iloc[-1]['ema21'] else "BEARISH 📉"
        
        # 10. Create premium signal message
        message = (
            f"╔═══════════════════════════════╗\n"
            f"  💎 <b>ELITE FOREX AI SIGNAL</b> 💎\n"
            f"╚═══════════════════════════════╝\n\n"
            f"<b>📌 Pair:</b> <code>{symbol}</code>\n"
            f"<b>💰 Price:</b> <code>{price:.{decimals}f}</code>\n"
            f"<b>⏰ Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"{'━' * 33}\n"
            f"🎯 <b>{emoji} SIGNAL: {direction} - {signal_strength}</b>\n"
            f"{'━' * 33}\n\n"
            f"<b>📊 CONFIDENCE SCORE: {score:.1f}/100</b>\n\n"
            f"<b>🔍 MULTI-TIMEFRAME ANALYSIS:</b>\n"
            f"├ 4H Trend: <code>{trend_4h}</code>\n"
            f"├ 1H Trend: <code>{trend_1h}</code>\n"
            f"├ RSI(14): <code>{df_1h.iloc[-1]['rsi']:.1f}</code>\n"
            f"├ ADX: <code>{df_1h.iloc[-1]['adx']:.1f}</code> (Trend Strength)\n"
            f"└ MACD: <code>{'Bullish ✅' if df_1h.iloc[-1]['macd_hist'] > 0 else 'Bearish ⚠️'}</code>\n\n"
            f"<b>🎯 TRADE SETUP:</b>\n"
            f"├ 🟢 <b>Entry:</b> <code>{price:.{decimals}f}</code>\n"
            f"├ 🛑 <b>Stop Loss:</b> <code>{risk_data['sl']:.{decimals}f}</code>\n"
            f"├ ✅ <b>Take Profit 1:</b> <code>{risk_data['tp1']:.{decimals}f}</code>\n"
            f"└ 🔥 <b>Take Profit 2:</b> <code>{risk_data['tp2']:.{decimals}f}</code>\n\n"
            f"<b>📈 RISK MANAGEMENT:</b>\n"
            f"├ Risk/Reward: <code>1:{risk_data['rr_ratio1']:.2f}</code>\n"
            f"├ ATR: <code>{risk_data['atr']:.{decimals}f}</code>\n"
            f"└ Risk Amount: <code>{risk_data['risk_amount']:.{decimals}f}</code>\n\n"
            f"<b>🏛️ CPR LEVELS:</b>\n"
        )
        
        if cpr:
            message += (
                f"├ Pivot: <code>{cpr['PP']:.{decimals}f}</code>\n"
                f"├ R1: <code>{cpr['R1']:.{decimals}f}</code> | S1: <code>{cpr['S1']:.{decimals}f}</code>\n"
                f"└ R2: <code>{cpr['R2']:.{decimals}f}</code> | S2: <code>{cpr['S2']:.{decimals}f}</code>\n\n"
            )
        
        message += (
            f"{'─' * 33}\n"
            f"<i>⚡ Elite Quant V3.0 | Advanced AI</i>\n"
            f"<i>⚠️ Trade at your own risk | Use SL</i>"
        )
        
        # 11. Send signal via Telegram
        asyncio.run(bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode='HTML'
        ))
        
        # 12. Update stats
        bot_stats['total_analyses'] += 1
        bot_stats['last_analysis'] = datetime.now().isoformat()
        bot_stats['status'] = "operational"
        
        performance_tracker[symbol]['total_signals'] += 1
        performance_tracker[symbol]['last_signal'] = {
            'time': datetime.now().isoformat(),
            'direction': direction,
            'score': score,
            'price': price
        }
        
        print(f"✅ Signal sent for {symbol}: {direction} (Score: {score:.1f})")
        
    except Exception as e:
        print(f"❌ Signal generation failed for {symbol}: {e}")
        traceback.print_exc()

# =========================================================================
# === SCHEDULER & FLASK APP ===
# =========================================================================

def start_bot():
    """Initialize the trading bot with scheduled analysis"""
    print(f"🚀 Initializing {bot_stats['version']}...")
    print(f"📊 Monitoring {len(FOREX_PAIRS)} pairs")
    print(f"⏰ Analysis every 30 minutes\n")
    
    scheduler = BackgroundScheduler()
    
    # Schedule analysis every 30 minutes for each pair
    for pair in FOREX_PAIRS:
        scheduler.add_job(
            generate_and_send_signal,
            'cron',
            minute='0,30',
            args=[pair],
            id=f'analysis_{pair}'
        )
    
    scheduler.start()
    
    # Run immediate baseline analysis
    print("🔄 Running initial analysis for all pairs...\n")
    for pair in FOREX_PAIRS:
        threading.Thread(target=generate_and_send_signal, args=(pair,), daemon=True).start()
        time.sleep(2)  # Stagger requests

# Start bot
start_bot()

# Flask Web Dashboard
app = Flask(__name__)

@app.route('/')
def home():
    """Enhanced dashboard with performance metrics"""
    total_signals = sum(p['total_signals'] for p in performance_tracker.values())
    
    pairs_html = ""
    for pair, stats in performance_tracker.items():
        pairs_html += f"<tr><td>{pair}</td><td>{stats['total_signals']}</td></tr>"
    
    return render_template_string("""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Forex AI Elite Dashboard</title>
            <meta http-equiv="refresh" content="60">
            <style>
                body {
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: #ffffff;
                    margin: 0;
                    padding: 20px;
                }
                .container {
                    max-width: 1000px;
                    margin: 0 auto;
                    background: rgba(0, 0, 0, 0.7);
                    padding: 40px;
                    border-radius: 20px;
                    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
                }
                h1 {
                    text-align: center;
                    color: #ffd700;
                    margin-bottom: 10px;
                    font-size: 2.5em;
                }
                .status {
                    text-align: center;
                    font-size: 1.2em;
                    margin-bottom: 30px;
                    color: #4ade80;
                }
                .stats-grid {
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                    gap: 20px;
                    margin-bottom: 30px;
                }
                .stat-card {
                    background: rgba(255, 255, 255, 0.1);
                    padding: 20px;
                    border-radius: 10px;
                    text-align: center;
                }
                .stat-value {
                    font-size: 2em;
                    font-weight: bold;
                    color: #ffd700;
                }
                .stat-label {
                    color: #a0aec0;
                    margin-top: 5px;
                }
                table {
                    width: 100%;
                    border-collapse: collapse;
                    margin-top: 20px;
                }
                th, td {
                    padding: 12px;
                    text-align: left;
                    border-bottom: 1px solid rgba(255, 255, 255, 0.1);
                }
                th {
                    background: rgba(255, 215, 0, 0.2);
                    color: #ffd700;
                }
                .footer {
                    text-align: center;
                    margin-top: 30px;
                    color: #a0aec0;
                    font-size: 0.9em;
                }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>💎 FOREX AI ELITE 💎</h1>
                <div class="status">
                    🟢 Status: <strong>{{status}}</strong> | Version: {{version}}
                </div>
                
                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-value">{{total}}</div>
                        <div class="stat-label">Total Signals</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value">{{pairs}}</div>
                        <div class="stat-label">Monitored Pairs</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value">30m</div>
                        <div class="stat-label">Analysis Interval</div>
                    </div>
                </div>
                
                <h2 style="color: #ffd700;">📊 Pair Performance</h2>
                <table>
                    <tr>
                        <th>Pair</th>
                        <th>Signals Sent</th>
                    </tr>
                    {{pairs_table}}
                </table>
                
                <div class="footer">
                    Last Analysis: {{last_time}}<br>
                    Uptime: {{uptime}}<br>
                    ⚡ Powered by Advanced AI & Multi-Indicator Confluence
                </div>
            </div>
        </body>
        </html>
    """, 
        status=bot_stats['status'].upper(),
        version=bot_stats['version'],
        total=total_signals,
        pairs=len(FOREX_PAIRS),
        pairs_table=pairs_html,
        last_time=bot_stats.get('last_analysis', 'Pending...'),
        uptime=bot_stats['uptime_start']
    )

@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "version": bot_stats['version'],
        "total_signals": bot_stats['total_analyses']
    }), 200

@app.route('/stats')
def stats():
    """API endpoint for detailed statistics"""
    return jsonify({
        "bot_stats": bot_stats,
        "performance": performance_tracker
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    print(f"\n🌐 Dashboard running on port {port}")
    print(f"🔗 Access at: http://localhost:{port}")
    app.run(host='0.0.0.0', port=port, debug=False)
