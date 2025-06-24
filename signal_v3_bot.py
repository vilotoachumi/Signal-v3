import pandas as pd
import numpy as np
import requests
import datetime as dt
import asyncio
import nest_asyncio
import matplotlib.pyplot as plt
import mplfinance as mpf
import ta
import pytz
from telegram import Bot
from apscheduler.schedulers.blocking import BlockingScheduler

nest_asyncio.apply()

TELEGRAM_BOT_TOKEN = "7613620588:AAEui2boeLqJ7ukxmjiiUNF8njOgEUoWRM8"
TELEGRAM_CHAT_ID = "7765972595"
TWELVE_DATA_API_KEY = "d84bb3f43e4740e89e1368a29861d31d"

symbols = [
    "BTC/USD", "XAU/USD", "EUR/USD", "GBP/USD",
    "AUD/USD", "USD/JPY", "USD/CAD", "NZD/USD"]

def fetch_data(symbol):
    url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval=30min&outputsize=100&apikey={TWELVE_DATA_API_KEY}"
    r = requests.get(url).json()
    if "values" not in r:
        raise Exception(f"Error fetching {symbol}: {r.get('message', 'Unknown error')}")
    df = pd.DataFrame(r["values"]).rename(columns={"datetime": "time"}).sort_values("time")
    df["time"] = pd.to_datetime(df["time"])
    df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)
    df["volume"] = pd.to_numeric(df.get("volume", pd.Series([0]*len(df))), errors='coerce').fillna(0.01)
    df.set_index("time", inplace=True)
    return df

def apply_indicators(df):
    df["EMA20"] = ta.trend.ema_indicator(df["close"], window=20)
    df["EMA50"] = ta.trend.ema_indicator(df["close"], window=50)
    df["RSI"] = ta.momentum.rsi(df["close"], window=14)
    df["MACD"] = ta.trend.macd(df["close"])
    df["MACD_Signal"] = ta.trend.macd_signal(df["close"])
    df["MACD_Hist"] = df["MACD"] - df["MACD_Signal"]
    df["ATR"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"])
    df["VOL"] = df["ATR"] > df["ATR"].rolling(10).mean() * 0.8
    return df

candlestick_patterns = {
    "bullish_engulfing": lambda o, c, po, pc: po > pc and c > o and c > po and o < pc,
    "bearish_engulfing": lambda o, c, po, pc: po < pc and c < o and c < po and o > pc,
}

def detect_signal(df):
    last, prev = df.iloc[-1], df.iloc[-2]
    
    ema_buy = last["EMA20"] > last["EMA50"]
    ema_sell = last["EMA20"] < last["EMA50"]
    rsi_buy = 40 < last["RSI"] < 70
    rsi_sell = 30 < last["RSI"] < 60
    macd_buy = last["MACD_Hist"] > 0
    macd_sell = last["MACD_Hist"] < 0
    rejection_buy = last["close"] > last["open"] and last["low"] < prev["low"]
    rejection_sell = last["close"] < last["open"] and last["high"] > prev["high"]
    high = df["high"].iloc[-10:].max()
    low = df["low"].iloc[-10:].min()
    breakout_buy = last["close"] >= high * 0.997
    breakout_sell = last["close"] <= low * 1.003
    vol = last["VOL"]
    
    bull_engulf = candlestick_patterns["bullish_engulfing"](last["open"], last["close"], prev["open"], prev["close"])
    bear_engulf = candlestick_patterns["bearish_engulfing"](last["open"], last["close"], prev["open"], prev["close"])

    score_buy = sum([ema_buy, rsi_buy, macd_buy, rejection_buy, breakout_buy, vol, bull_engulf])
    score_sell = sum([ema_sell, rsi_sell, macd_sell, rejection_sell, breakout_sell, vol, bear_engulf])
    
    if score_buy >= 7:
        return "BUY", score_buy
    elif score_sell >= 7:
        return "SELL", score_sell
    return None, 0

def calculate_tp_sl(df, signal):
    last_close = df["close"].iloc[-1]
    swing_high = df["high"].iloc[-10:].max()
    swing_low = df["low"].iloc[-10:].min()
    diff = swing_high - swing_low

    if signal == "BUY":
        sl = swing_low
        tp = swing_high + diff * 0.618
    elif signal == "SELL":
        sl = swing_high
        tp = swing_low - diff * 0.618
    else:
        return None, None

    if abs(tp - last_close) < 0.2 or abs(sl - last_close) < 0.2:
        return None, None
    return sl, tp

def plot_chart(df, symbol, signal, entry, sl, tp):
    df_plot = df[-30:].copy()
    fib_high = df["high"].iloc[-10:].max()
    fib_low = df["low"].iloc[-10:].min()
    diff = fib_high - fib_low
    fib_levels = [fib_high - diff * r for r in [0, 0.236, 0.382, 0.5, 0.618, 1.0]]

    apds = [
        mpf.make_addplot(df_plot["EMA20"], color='blue'),
        mpf.make_addplot(df_plot["EMA50"], color='red'),
        mpf.make_addplot(df_plot["MACD_Hist"], panel=1, type='bar', color='gray'),
        mpf.make_addplot(df_plot["MACD"], panel=1, color='green'),
        mpf.make_addplot(df_plot["MACD_Signal"], panel=1, color='orange'),
        mpf.make_addplot(df_plot["RSI"], panel=2, color='purple'),
    ]
    hlines = [entry, sl, tp] + fib_levels
    hlcolors = ['blue', 'red', 'green'] + ['gray'] * len(fib_levels)
    fig, _ = mpf.plot(df_plot, type='candle', volume=True, addplot=apds,
                      hlines=dict(hlines=hlines, colors=hlcolors, linestyle='--'),
                      returnfig=True, style='yahoo', figsize=(8, 6),
                      title=f"{symbol} - {signal} Signal")
    file = f"{symbol.replace('/', '')}_{signal}.png"
    fig.savefig(file)
    plt.close(fig)
    return file

async def send_alert(symbol, signal, entry, sl, tp, file, score):
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    msg = f"""📊 {signal} SIGNAL — {symbol}
🕐 {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}
Entry: {entry:.2f}  
TP: {tp:.2f}  
SL: {sl:.2f}  
📌 Triple Indicator + Multi-Candle Confirmed  
📈 Signal Strength: {score}/10 🔥"""
    with open(file, 'rb') as photo:
        await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=photo, caption=msg, parse_mode="Markdown")

def send_alert_sync(*args):
    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(send_alert(*args))
    else:
        loop.run_until_complete(send_alert(*args))

# Global dictionary to store last signals
last_signals = {}

def scan_symbol(symbol):
    print(f"🔍 Scanning {symbol} ...")
    try:
        df = fetch_data(symbol)
        df = apply_indicators(df)
        signal, score = detect_signal(df)

        if signal:
            # ⛔ Skip duplicate signal
            if last_signals.get(symbol) == signal:
                print(f"⚠️ Duplicate {signal} signal for {symbol} — Skipping alert.")
                return
            last_signals[symbol] = signal  # ✅ Update signal memory

            entry = df["close"].iloc[-1]
            sl, tp = calculate_tp_sl(df, signal)
            if sl is None or tp is None:
                print(f"⚠️ Invalid TP/SL - Skipping.")
                return
            chart = plot_chart(df, symbol, signal, entry, sl, tp)
            send_alert_sync(symbol, signal, entry, sl, tp, chart, score)
            print(f"✅ {signal} signal on {symbol} | Strength: {score}/10")
        else:
            print(f"⚠️ No signal for {symbol}")
            last_signals[symbol] = None  # Clear last signal if no signal now

    except Exception as e:
        print(f"❌ Error scanning {symbol}:", e)
      
import pytz
def is_market_open_ist():
    now_ist = dt.datetime.now(pytz.timezone("Asia/Kolkata"))
    return now_ist.weekday() < 5  # Monday (0) to Friday (4)

def scan_all():
    now_ist = dt.datetime.now(pytz.timezone("Asia/Kolkata"))
    print(f"\n🕒 Scan at {now_ist.strftime('%Y-%m-%d %H:%M')} (Asia/Kolkata)")
    for sym in symbols:
        if sym == "BTC/USD" or is_market_open_ist():
            scan_symbol(sym)
        else:
            print(f"⛔ Market closed — Skipping {sym}")


scheduler = BlockingScheduler(timezone="Asia/Kolkata")
scheduler.add_job(scan_all, "interval", minutes=30)

print("✅ Signal v3 is scanning every 15 min (15m TF)...")
print("⏳ Running first scan now...")
scan_all()
scheduler.start()
