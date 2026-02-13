import sys
import os
import time
from datetime import datetime
import concurrent.futures
import warnings
import numpy as np 

# Filter warning agar log bersih
warnings.filterwarnings("ignore", category=UserWarning)

# ==========================================
# 1. CEK LIBRARY
# ==========================================
try:
    import ccxt
    import pandas as pd
    import pandas_ta as ta
    import mplfinance as mpf
    import requests
except ImportError as e:
    sys.exit(f"Library Error: {e}. Install dulu: pip install ccxt pandas pandas_ta mplfinance requests numpy")

# ==========================================
# 2. KONFIGURASI
# ==========================================
API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
API_SECRET = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')

# Telegram Config
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8361349338:AAHOlx4fKz_bp1MHnVg8CxS9MY_pcejxLes') 
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003559364460')

# SETTING STRATEGI
TIMEFRAME_MAIN = '5m'   # Timeframe Utama untuk Entry (BBMA/SMC)
TIMEFRAME_NATR = '1m'   # Timeframe Khusus Volatilitas (NATR)
NATR_PERIOD = 30        # Periode NATR

VOL_MULTIPLIER = 2.0    # Volume minimal 2x rata-rata 
LIMIT = 400             # Data candle
TOP_COIN_COUNT = 100    # Top 100 Gainers
MAX_THREADS = 15        

OUTPUT_FOLDER = 'gainer_5m_natr1m_results'
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
processed_signals = {} 

# ==========================================
# 3. KONEKSI EXCHANGE
# ==========================================
exchange = ccxt.binance({
    'apiKey': API_KEY, 'secret': API_SECRET,
    'options': {'defaultType': 'spot'},
    'enableRateLimit': True, 
})

# ==========================================
# 4. TELEGRAM & CHART
# ==========================================
def send_telegram_alert(symbol, data, image_path):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    
    icon = "🟢" if data['tipe'] == "BUY" else "🔴"
    
    # Kategorisasi NATR 1m/30
    # Karena timeframe 1m rentangnya lebih kecil, threshold persentasenya disesuaikan
    natr_val = data['natr']
    volatility_status = "SQUEEZE/TENANG 💤"
    if natr_val > 0.15: volatility_status = "NORMAL ✅"
    if natr_val > 0.40: volatility_status = "VOLATILE ⚡"
    if natr_val > 0.80: volatility_status = "EXTREME ⚠️"
    
    # Format pesan SMC
    smc_txt = data.get('smc_context', '-')
    pattern_txt = data.get('pattern', '-')
    
    caption = (
        f"🚀 <b>TOP GAINER / VIRAL (5M)</b>\n"
        f"💎 <b>{symbol}</b>\n"
        f"📈 <b>24h Change:</b> +{data['24h_change']:.2f}%\n"
        f"──────────────────────\n"
        f"⚡ <b>NATR (1m/30):</b> {natr_val:.3f}%\n"
        f"ℹ️ <b>Status:</b> {volatility_status}\n"
        f"──────────────────────\n"
        f"📊 <b>Vol Spike:</b> {data['spike_ratio']:.2f}x\n"
        f"🛠 <b>Setup BBMA:</b> {data['signal']} {icon}\n"
        f"🧠 <b>SMC Vol:</b> {smc_txt}\n"
        f"📐 <b>Pola:</b> {pattern_txt}\n"
        f"🏷 <b>Tipe:</b> {data['tipe']}\n"
        f"💰 <b>Harga:</b> {data['price']}\n\n"
        f"📝 <b>Analisa:</b>\n{data['explanation']}\n"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        with open(image_path, "rb") as img:
            requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'HTML'}, files={'photo': img}, timeout=20)
    except Exception as e:
        print(f"Gagal kirim TG: {e}")

def generate_chart(df, symbol, signal_info):
    try:
        title_text = f"{symbol} [5M] - {signal_info['signal']}"
        title_text += f" | NATR(1m): {signal_info['natr']:.3f}%"

        filename = f"{OUTPUT_FOLDER}/{symbol.replace('/','-')}_{signal_info['signal']}.png"
        plot_df = df.tail(100).copy() 
        plot_df.set_index('timestamp', inplace=True)
        
        style = mpf.make_mpf_style(base_mpf_style='nightclouds', rc={'font.size': 8})
        adds = [
            mpf.make_addplot(plot_df['BB_Up'], color='green', width=1),
            mpf.make_addplot(plot_df['BB_Mid'], color='orange', width=1, linestyle='--'),
            mpf.make_addplot(plot_df['BB_Low'], color='green', width=1),
            mpf.make_addplot(plot_df['MA5_Hi'], color='cyan', width=0.7),
            mpf.make_addplot(plot_df['MA5_Lo'], color='magenta', width=0.7),
            mpf.make_addplot(plot_df['MA10_Hi'], color='yellow', width=0.7),
            mpf.make_addplot(plot_df['MA10_Lo'], color='white', width=0.7),
        ]
        
        mpf.plot(plot_df, type='candle', style=style, addplot=adds, 
                 title=title_text, 
                 savefig=dict(fname=filename, bbox_inches='tight'), volume=True)
    
        return filename
    except Exception as e:
        print(f"Chart Error: {e}")
        return None

# ==========================================
# 5. DATA ENGINE
# ==========================================
def get_viral_symbols(limit=100):
    try:
        exchange.load_markets()
        tickers = exchange.fetch_tickers()
        valid_tickers = []
        
        for s, t in tickers.items():
            if '/USDT' in s and 'UP/' not in s and 'DOWN/' not in s and 'BEAR/' not in s and 'BULL/' not in s:
                if t['quoteVolume'] and t['quoteVolume'] > 2000000:
                    valid_tickers.append(t)
        
        # Sort by Top Gainers
        sorted_tickers = sorted(valid_tickers, key=lambda x: x['percentage'] if x['percentage'] else -999, reverse=True)
        return [{'symbol': t['symbol'], 'change': t['percentage']} for t in sorted_tickers[:limit]]
    except Exception as e: 
        print(f"Error fetch tickers: {e}")
        return []

# Fungsi Fetch diperbarui agar bisa terima parameter Timeframe custom
def fetch_ohlcv(symbol, timeframe=TIMEFRAME_MAIN, limit=LIMIT):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except: return None

def add_indicators(df):
    # Indikator untuk Timeframe Utama (5m)
    bb = df.ta.bbands(length=20, std=2)
    df['BB_Up'] = bb.iloc[:, 2]
    df['BB_Mid'] = bb.iloc[:, 1]
    df['BB_Low'] = bb.iloc[:, 0]
    
    df['MA5_Hi'] = df['high'].rolling(5).mean()
    df['MA5_Lo'] = df['low'].rolling(5).mean()
    df['MA10_Hi'] = df['high'].rolling(10).mean()
    df['MA10_Lo'] = df['low'].rolling(10).mean()
    df['EMA_50'] = df.ta.ema(length=50)
    
    return df

# ==========================================
# 6. LOGIKA ANALISIS
# ==========================================

def calculate_natr_1m(symbol):
    """
    Mengambil data 1 MENIT khusus untuk menghitung NATR 30.
    """
    try:
        # Kita butuh setidaknya 50 candle untuk menghitung EMA/ATR 30 dengan akurat
        df_1m = fetch_ohlcv(symbol, timeframe=TIMEFRAME_NATR, limit=60)
        if df_1m is not None and len(df_1m) > NATR_PERIOD:
            # Hitung NATR periode 30
            natr_series = df_1m.ta.natr(length=NATR_PERIOD)
            # Ambil nilai terakhir yang closed (iloc[-2])
            return natr_series.iloc[-2]
    except: pass
    return 0

def analyze_smc_divergence(df):
    if df is None or len(df) < 30: return "Data Kurang"
    subset = df.iloc[-20:-2]
    closes = subset['close'].values; volumes = subset['volume'].values
    x = np.arange(len(closes))
    slope_price, _ = np.polyfit(x, closes, 1)
    slope_vol, _ = np.polyfit(x, volumes, 1)
    price_threshold = closes[0] * 0.0001 
    result = "NORMAL"
    if slope_price > price_threshold and slope_vol < 0: result = "BEARISH DIVERGENCE (Weak Buyers)"
    elif slope_price < -price_threshold and slope_vol < 0: result = "BULLISH DIVERGENCE (Weak Sellers)"
    return result

def analyze_chart_pattern(df):
    if df is None or len(df) < 50: return None
    subset = df.iloc[-50:]
    closes = subset['close'].values
    highs = subset['high'].values
    lows = subset['low'].values
    pattern_found = []
    
    peaks_idx = subset['high'].rolling(window=5, center=True).max() == subset['high']
    peaks = subset[peaks_idx]
    
    if len(peaks) >= 2:
        if abs(peaks.iloc[-1]['high'] - peaks.iloc[-2]['high']) / peaks.iloc[-2]['high'] < 0.003:
             if (peaks.iloc[-1].name - peaks.iloc[-2].name) > 5:
                 pattern_found.append("Double Top")

    y_high = highs[-20:]; y_low = lows[-20:]; x = np.arange(len(y_high))
    slope_high, _ = np.polyfit(x, y_high, 1)
    slope_low, _ = np.polyfit(x, y_low, 1)
    flat_threshold = 0.0003 * closes[-1] 

    if slope_high > 0 and slope_low > flat_threshold and slope_low > slope_high:
        pattern_found.append("Rising Wedge")
    elif (closes[-10] - closes[-30]) > 0 and abs(slope_high) < flat_threshold and abs(slope_low) < flat_threshold:
        pattern_found.append("Bullish Rectangle")

    return ", ".join(pattern_found) if pattern_found else None

def analyze_bbma_setup(df):
    if df is None or len(df) < 30: return None
    c = df.iloc[-2]; prev = df.iloc[-3]
    recent_data = df.iloc[-20:-2] 
    res = None

    extreme_buy = c['MA5_Lo'] < c['BB_Low']
    extreme_sell = c['MA5_Hi'] > c['BB_Up']
    
    mhv_buy = (prev['MA5_Lo'] < prev['BB_Low']) and (c['close'] > c['BB_Low']) and (c['close'] < c['BB_Mid'])
    mhv_sell = (prev['MA5_Hi'] > prev['BB_Up']) and (c['close'] < c['BB_Up']) and (c['close'] > c['BB_Mid'])

    csak_buy = (c['close'] > c['BB_Mid']) and (prev['close'] < c['BB_Mid']) and (c['close'] > c['MA5_Hi']) and (c['close'] > c['MA10_Hi'])
    csak_sell = (c['close'] < c['BB_Mid']) and (prev['close'] > c['BB_Mid']) and (c['close'] < c['MA5_Lo']) and (c['close'] < c['MA10_Lo'])

    had_momentum_buy = any(recent_data['close'] > recent_data['BB_Up'])
    had_momentum_sell = any(recent_data['close'] < recent_data['BB_Low'])
    reentry_buy = had_momentum_buy and (c['low'] <= c['MA5_Lo'] or c['low'] <= c['MA10_Lo']) and (c['close'] > c['MA5_Lo'])
    reentry_sell = had_momentum_sell and (c['high'] >= c['MA5_Hi'] or c['high'] >= c['MA10_Hi']) and (c['close'] < c['MA5_Hi'])

    if extreme_buy: res = {"signal": "EXTREME BUY", "tipe": "BUY", "explanation": "Reversal Awal (MA5 < Low BB)."}
    elif extreme_sell: res = {"signal": "EXTREME SELL", "tipe": "SELL", "explanation": "Reversal Awal (MA5 > Top BB)."}
    elif mhv_buy: res = {"signal": "MHV BUY", "tipe": "BUY", "explanation": "Gagal tembus Low BB pasca Extreme."}
    elif mhv_sell: res = {"signal": "MHV SELL", "tipe": "SELL", "explanation": "Gagal tembus Top BB pasca Extreme."}
    elif csak_buy: res = {"signal": "CSAK BUY", "tipe": "BUY", "explanation": "Break Mid BB + MA5/10 High."}
    elif csak_sell: res = {"signal": "CSAK SELL", "tipe": "SELL", "explanation": "Break Mid BB + MA5/10 Low."}
    elif reentry_buy: res = {"signal": "RE-ENTRY BUY", "tipe": "BUY", "explanation": "Pullback ke MA5/10 Low pasca Momentum."}
    elif reentry_sell: res = {"signal": "RE-ENTRY SELL", "tipe": "SELL", "explanation": "Pullback ke MA5/10 High pasca Momentum."}

    if res:
        res['price'] = c['close']; res['time'] = c['timestamp']
        return res
    return None

def analyze_volume_anomaly(df):
    if df is None or len(df) < 290: return 0
    current_vol = df['volume'].iloc[-2]
    avg_vol_24h = df['volume'].iloc[-290:-2].mean()
    return current_vol / avg_vol_24h if avg_vol_24h > 0 else 0

# ==========================================
# 7. WORKER SCAN
# ==========================================
def worker_scan(coin_data):
    try:
        symbol = coin_data['symbol']
        change_24h = coin_data['change']
        
        # 1. Ambil Data 5m (Utama)
        df = fetch_ohlcv(symbol, timeframe=TIMEFRAME_MAIN)
        if df is None: return None

        # 2. Cek Volume Spike di 5m
        spike_ratio = analyze_volume_anomaly(df)
        if spike_ratio < VOL_MULTIPLIER:
            return None

        df = add_indicators(df)

        # 3. Cek BBMA Setup di 5m
        res = analyze_bbma_setup(df)
        
        if res:
            # 4. Jika ada Setup, Ambil Data 1m untuk hitung NATR 30
            natr_1m_30 = calculate_natr_1m(symbol)
            
            # Analisa SMC & Pattern
            smc_context = analyze_smc_divergence(df)
            pattern_data = analyze_chart_pattern(df)
            
            res['symbol'] = symbol
            res['24h_change'] = change_24h
            res['spike_ratio'] = spike_ratio
            res['natr'] = natr_1m_30 # Nilai NATR 1m/30
            res['df'] = df
            res['smc_context'] = smc_context
            res['pattern'] = pattern_data if pattern_data else "-"
            
            return res
    except: pass
    return None

# ==========================================
# 8. MAIN LOOP
# ==========================================
def main():
    print(f"🚀 BBMA + SMC + NATR (1m/30) BOT")
    print(f"Main TF: {TIMEFRAME_MAIN} | Volatility Check: NATR 1m/30")
    print(f"Target: Top {TOP_COIN_COUNT} Gainers")
    
    global processed_signals

    while True:
        try:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Mengambil daftar Top Gainers...")
            viral_coins = get_viral_symbols(TOP_COIN_COUNT)
            
            if not viral_coins:
                print("Gagal mengambil data market.")
                time.sleep(10)
                continue
                
            print(f"-> Memindai {len(viral_coins)} koin...")
            
            alerts_queue = []
            completed = 0
            start_t = time.time()
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
                futures = {executor.submit(worker_scan, coin): coin['symbol'] for coin in viral_coins}
                
                for future in concurrent.futures.as_completed(futures):
                    res = future.result()
                    if res: alerts_queue.append(res)
                    completed += 1
                    if completed % 20 == 0:
                        sys.stdout.write(f"\rProgress: {completed}/{len(viral_coins)}...")
                        sys.stdout.flush()
            
            duration = time.time() - start_t
            print(f"\n✅ Selesai ({duration:.1f}s). Ditemukan: {len(alerts_queue)} setup.")

            # Sort by NATR (Volatilitas) atau Spike Ratio
            alerts_queue.sort(key=lambda x: x['spike_ratio'], reverse=True)

            for alert in alerts_queue:
                sym = alert['symbol']
                sig_key = f"{sym}_{alert['signal']}"
                
                if processed_signals.get(sig_key) != alert['time']:
                    processed_signals[sig_key] = alert['time']
                    
                    smc_tag = f"[SMC: {alert['smc_context']}]" if "DIVERGENCE" in alert['smc_context'] else ""
                    print(f"🔥 {sym} [NATR 1m: {alert['natr']:.3f}%] -> {alert['signal']} {smc_tag}")
                    
                    img = generate_chart(alert['df'], sym, alert)
                    if img: 
                        send_telegram_alert(sym, alert, img)
            
            print("⏳ Menunggu 20 detik...")
            time.sleep(20)

        except KeyboardInterrupt: 
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()


