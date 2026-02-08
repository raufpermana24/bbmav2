import sys
import os
import time
from datetime import datetime
import concurrent.futures
import warnings
import numpy as np # Import numpy untuk perhitungan regresi linear sederhana

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
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003768840240')

# SETTING STRATEGI
TIMEFRAME = '15m'       
VOL_MULTIPLIER = 2.0    # Volume minimal 2x rata-rata 24 jam
LIMIT = 200             
TOP_COIN_COUNT = 300    
MAX_THREADS = 15        

OUTPUT_FOLDER = 'volume_15m_results'
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
processed_signals = {} 

# ==========================================
# 3. KONEKSI EXCHANGE
# ==========================================
exchange = ccxt.binance({
    'apiKey': API_KEY, 'secret': API_SECRET,
    'options': {'defaultType': 'future'},
    'enableRateLimit': True, 
})

# ==========================================
# 4. TELEGRAM & CHART
# ==========================================
def send_telegram_alert(symbol, data, image_path):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    
    icon = "🟢" if data['tipe'] == "BUY" else "🔴"
    spike_pct = (data['spike_ratio'] * 100) - 100
    
    # Format pesan chart pattern jika ada
    pattern_txt = data.get('pattern', 'Tidak terdeteksi')
    
    caption = (
        f"🐋 <b>BBMA OMA ALLY + PATTERN ALERT (15M)</b>\n"
        f"💎 <b>{symbol}</b>\n"
        f"──────────────────────\n"
        f"📊 <b>Vol Spike:</b> {data['spike_ratio']:.2f}x\n"
        f"🔥 <b>Lonjakan:</b> +{spike_pct:.0f}%\n"
        f"──────────────────────\n"
        f"🛠 <b>Setup BBMA:</b> {data['signal']} {icon}\n"
        f"📐 <b>Pola Chart:</b> {pattern_txt}\n"
        f"🏷 <b>Tipe:</b> {data['tipe']}\n"
        f"💰 <b>Harga:</b> {data['price']}\n\n"
        f"📝 <b>Analisa:</b>\n{data['explanation']}\n"
        f"──────────────────────\n"
        f"<i>Signal terdeteksi pada candle tertutup.</i>"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        with open(image_path, "rb") as img:
            requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'HTML'}, files={'photo': img}, timeout=20)
    except Exception as e:
        print(f"Gagal kirim TG: {e}")

def generate_chart(df, symbol, signal_info):
    try:
        pattern_info = signal_info.get('pattern', '')
        title_text = f"{symbol} [15M] - {signal_info['signal']}"
        if pattern_info and pattern_info != 'Tidak terdeteksi':
            title_text += f" | {pattern_info}"

        filename = f"{OUTPUT_FOLDER}/{symbol.replace('/','-')}_{signal_info['signal']}.png"
        plot_df = df.tail(80).copy()
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
def get_top_symbols(limit=300):
    try:
        exchange.load_markets()
        tickers = exchange.fetch_tickers()
        valid_tickers = [t for t in tickers.values() if '/USDT' in t['symbol'] and 'USDC' not in t['symbol'] and 'UP/' not in t['symbol'] and 'DOWN/' not in t['symbol']]
        sorted_tickers = sorted(valid_tickers, key=lambda x: x['quoteVolume'] if x['quoteVolume'] else 0, reverse=True)
        return [t['symbol'] for t in sorted_tickers[:limit]]
    except: return []

def fetch_ohlcv(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=LIMIT)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except: return None

def add_indicators(df):
    # Bollinger Bands
    bb = df.ta.bbands(length=20, std=2)
    df['BB_Up'] = bb.iloc[:, 2]
    df['BB_Mid'] = bb.iloc[:, 1]
    df['BB_Low'] = bb.iloc[:, 0]
    
    # MA 5 High/Low
    df['MA5_Hi'] = df['high'].rolling(5).mean()
    df['MA5_Lo'] = df['low'].rolling(5).mean()
    
    # MA 10 High/Low
    df['MA10_Hi'] = df['high'].rolling(10).mean()
    df['MA10_Lo'] = df['low'].rolling(10).mean()
    
    # EMA 50 untuk Trend
    df['EMA_50'] = df.ta.ema(length=50)
    
    return df

# ==========================================
# 6. LOGIKA BBMA OMA ALLY & CHART PATTERN
# ==========================================

# --- METODE CHART PATTERN (LOGIKA BARU) ---
# 1. Metode Bullish Chart Pattern (Sinyal Beli):
#    - Bullish Flag/Pennant, Double Bottom, Inv. Head & Shoulders, Falling Wedge, Bullish Rectangle
# 2. Metode Bearish Chart Pattern (Sinyal Jual):
#    - Bearish Flag/Pennant, Double Top, Head & Shoulders, Rising Wedge, Descending Triangle
# 3. Konfirmasi Breakout & Volume

def analyze_chart_pattern(df):
    """
    Mendeteksi pola grafik sederhana berdasarkan 30-50 candle terakhir.
    Menggunakan logika local peaks/troughs dan slope trendline.
    """
    if df is None or len(df) < 50: return None
    
    # Ambil data 50 candle terakhir
    subset = df.iloc[-50:]
    closes = subset['close'].values
    highs = subset['high'].values
    lows = subset['low'].values
    
    pattern_found = []
    
    # --- A. DETEKSI DOUBLE TOP / DOUBLE BOTTOM (Reversal) ---
    # Mencari 2 puncak atau 2 lembah yang sejajar
    # Menggunakan window rolling sederhana untuk mencari lokal ekstrim
    
    # Cari 2 High tertinggi dalam 30 periode terakhir
    peaks_idx = subset['high'].rolling(window=5, center=True).max() == subset['high']
    peaks = subset[peaks_idx]
    
    troughs_idx = subset['low'].rolling(window=5, center=True).min() == subset['low']
    troughs = subset[troughs_idx]
    
    if len(peaks) >= 2:
        last_peak = peaks.iloc[-1]
        prev_peak = peaks.iloc[-2]
        # Cek jika tinggi puncak mirip (toleransi 0.5%) dan jaraknya > 5 candle
        if abs(last_peak['high'] - prev_peak['high']) / prev_peak['high'] < 0.005:
             if (last_peak.name - prev_peak.name) > 5:
                 pattern_found.append("Double Top (Potensi Bearish)")

    if len(troughs) >= 2:
        last_trough = troughs.iloc[-1]
        prev_trough = troughs.iloc[-2]
        # Cek jika dalam lembah mirip (toleransi 0.5%)
        if abs(last_trough['low'] - prev_trough['low']) / prev_trough['low'] < 0.005:
            if (last_trough.name - prev_trough.name) > 5:
                pattern_found.append("Double Bottom (Potensi Bullish)")

    # --- B. DETEKSI WEDGE / TRIANGLE (Konsolidasi) ---
    # Menggunakan slope (kemiringan) dari Highs dan Lows 20 candle terakhir
    # Kita pakai numpy polyfit untuk cari kemiringan garis regresi
    
    y_high = highs[-20:]
    y_low = lows[-20:]
    x = np.arange(len(y_high))
    
    slope_high, _ = np.polyfit(x, y_high, 1) # Kemiringan Resistance
    slope_low, _ = np.polyfit(x, y_low, 1)   # Kemiringan Support
    
    # Toleransi slope untuk dianggap "Datar"
    flat_threshold = 0.0005 * closes[-1] 

    # 1. Falling Wedge (Bullish): High turun tajam, Low turun landai/menyempit
    if slope_high < -flat_threshold and slope_low < 0 and slope_high < slope_low:
        pattern_found.append("Falling Wedge (Potensi Bullish)")
        
    # 2. Rising Wedge (Bearish): High naik landai, Low naik tajam/menyempit
    elif slope_high > 0 and slope_low > flat_threshold and slope_low > slope_high:
        pattern_found.append("Rising Wedge (Potensi Bearish)")
        
    # 3. Bullish Flag/Rectangle (Konsolidasi setelah naik)
    # Cek trend sebelumnya (candle -30 sampai -10 harus naik)
    prev_trend = closes[-10] - closes[-30]
    if prev_trend > 0 and abs(slope_high) < flat_threshold and abs(slope_low) < flat_threshold:
        pattern_found.append("Bullish Rectangle/Flag")
        
    # 4. Bearish Flag/Rectangle (Konsolidasi setelah turun)
    elif prev_trend < 0 and abs(slope_high) < flat_threshold and abs(slope_low) < flat_threshold:
        pattern_found.append("Bearish Rectangle/Flag")

    if not pattern_found:
        return None
        
    return ", ".join(pattern_found)

def analyze_bbma_setup(df):
    if df is None or len(df) < 30: return None
    
    # Kita cek Candle Terakhir yang sudah Close (-2)
    # Candle -1 adalah candle yang sedang berjalan (running)
    c = df.iloc[-2] 
    prev = df.iloc[-3]
    prev2 = df.iloc[-4]
    
    # Data tambahan untuk deteksi momentum/re-entry
    # Mengecek apakah 10 candle terakhir ada momentum
    recent_data = df.iloc[-15:-2] 
    
    ema_val = c.get('EMA_50', 0)
    
    res = None

    # --- 1. EXTREME ---
    # MA5 keluar dari BB
    extreme_buy = c['MA5_Lo'] < c['BB_Low']
    extreme_sell = c['MA5_Hi'] > c['BB_Up']

    # --- 2. MHV (Market Hilang Volume) ---
    # Terjadi setelah Extreme. Harga mencoba retest Top/Low tapi gagal menembus/close di luar BB
    mhv_buy = (prev['MA5_Lo'] < prev['BB_Low']) and (c['close'] > c['BB_Low']) and (c['close'] < c['BB_Mid'])
    mhv_sell = (prev['MA5_Hi'] > prev['BB_Up']) and (c['close'] < c['BB_Up']) and (c['close'] > c['BB_Mid'])

    # --- 3. CSAK (Candle Arah Kukuh) ---
    # Candle menembus Mid BB dan MA5/10 sekaligus
    csak_buy = (c['close'] > c['BB_Mid']) and (prev['close'] < c['BB_Mid']) and (c['close'] > c['MA5_Hi']) and (c['close'] > c['MA10_Hi'])
    csak_sell = (c['close'] < c['BB_Mid']) and (prev['close'] > c['BB_Mid']) and (c['close'] < c['MA5_Lo']) and (c['close'] < c['MA10_Lo'])

    # --- 4. RE-ENTRY ---
    # Harus ada Momentum sebelumnya (Close di luar BB)
    had_momentum_buy = any(recent_data['close'] > recent_data['BB_Up'])
    had_momentum_sell = any(recent_data['close'] < recent_data['BB_Low'])
    
    # Harga kembali ke MA5/MA10
    reentry_buy = had_momentum_buy and (c['low'] <= c['MA5_Lo'] or c['low'] <= c['MA10_Lo']) and (c['close'] > c['MA5_Lo'])
    reentry_sell = had_momentum_sell and (c['high'] >= c['MA5_Hi'] or c['high'] >= c['MA10_Hi']) and (c['close'] < c['MA5_Hi'])

    # --- PEMILIHAN SINYAL PRIORITAS ---
    if extreme_buy:
        res = {"signal": "EXTREME BUY", "tipe": "BUY", "explanation": "MA5 Low keluar dari Low BB. Potensi awal reversal."}
    elif extreme_sell:
        res = {"signal": "EXTREME SELL", "tipe": "SELL", "explanation": "MA5 High keluar dari Top BB. Potensi awal reversal."}
    
    elif mhv_buy:
        res = {"signal": "MHV BUY", "tipe": "BUY", "explanation": "Market Hilang Volume. Harga gagal menembus kembali Low BB setelah Extreme."}
    elif mhv_sell:
        res = {"signal": "MHV SELL", "tipe": "SELL", "explanation": "Market Hilang Volume. Harga gagal menembus kembali Top BB setelah Extreme."}
        
    elif csak_buy:
        res = {"signal": "CSAK BUY", "tipe": "BUY", "explanation": "Candle Arah Kukuh. Candle menembus Mid BB dan MA5/10 High."}
    elif csak_sell:
        res = {"signal": "CSAK SELL", "tipe": "SELL", "explanation": "Candle Arah Kukuh. Candle menembus Mid BB dan MA5/10 Low."}
        
    elif reentry_buy:
        res = {"signal": "RE-ENTRY BUY", "tipe": "BUY", "explanation": "Setup aman. Harga kembali ke MA5/10 Low setelah adanya Momentum Buy."}
    elif reentry_sell:
        res = {"signal": "RE-ENTRY SELL", "tipe": "SELL", "explanation": "Setup aman. Harga kembali ke MA5/10 High setelah adanya Momentum Sell."}

    if res:
        res['price'] = c['close']
        res['time'] = c['timestamp']
        return res
    
    return None

def analyze_volume_anomaly(df):
    if df is None or len(df) < 100: return 0
    current_vol = df['volume'].iloc[-2]
    avg_vol_24h = df['volume'].iloc[-98:-2].mean()
    if avg_vol_24h == 0: return 0
    return current_vol / avg_vol_24h

# ==========================================
# 7. WORKER SCAN
# ==========================================
def worker_scan(symbol):
    try:
        df = fetch_ohlcv(symbol)
        if df is None: return None

        # Filter Volume Spike dulu
        spike_ratio = analyze_volume_anomaly(df)
        if spike_ratio < VOL_MULTIPLIER:
            return None

        # Jika volume ok, hitung BBMA
        df = add_indicators(df)
        res = analyze_bbma_setup(df)
        
        if res:
            # Analisa Pola Grafik (Chart Pattern) sebagai tambahan
            pattern_data = analyze_chart_pattern(df)
            
            res['symbol'] = symbol
            res['spike_ratio'] = spike_ratio
            res['df'] = df
            res['pattern'] = pattern_data if pattern_data else "Tidak terdeteksi"
            return res
    except: pass
    return None

# ==========================================
# 8. MAIN LOOP
# ==========================================
def main():
    print(f"🚀 BBMA OMA ALLY BOT v2 STARTED")
    print(f"Filter: Volume > {VOL_MULTIPLIER}x | Timeframe: {TIMEFRAME}")
    
    global processed_signals

    while True:
        try:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Memulai pemindaian {TOP_COIN_COUNT} koin...")
            symbols = get_top_symbols(TOP_COIN_COUNT)
            alerts_queue = []
            
            completed = 0
            start_t = time.time()
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
                futures = {executor.submit(worker_scan, sym): sym for sym in symbols}
                for future in concurrent.futures.as_completed(futures):
                    res = future.result()
                    if res: alerts_queue.append(res)
                    completed += 1
                    if completed % 25 == 0:
                        sys.stdout.write(f"\rProgress: {completed}/{len(symbols)} koin checked...")
                        sys.stdout.flush()
            
            duration = time.time() - start_t
            print(f"\n✅ Scan Selesai dalam {duration:.1f}s. Terdeteksi {len(alerts_queue)} setup.")

            # Urutkan berdasarkan lonjakan volume tertinggi
            alerts_queue.sort(key=lambda x: x['spike_ratio'], reverse=True)

            for alert in alerts_queue:
                sym = alert['symbol']
                # Mencegah double alert untuk candle yang sama
                sig_key = f"{sym}_{alert['signal']}"
                if processed_signals.get(sig_key) != alert['time']:
                    processed_signals[sig_key] = alert['time']
                    
                    pattern_text = f" | {alert['pattern']}" if alert['pattern'] != "Tidak terdeteksi" else ""
                    print(f"🔥 Sinyal: {sym} -> {alert['signal']} (Vol {alert['spike_ratio']:.1f}x){pattern_text}")
                    
                    img = generate_chart(alert['df'], sym, alert)
                    if img: 
                        send_telegram_alert(sym, alert, img)
            
            print("⏳ Standby... Menunggu siklus berikutnya.")
            time.sleep(45) # Cek setiap 45 detik

        except KeyboardInterrupt: 
            print("\nBot dimatikan oleh pengguna.")
            break
        except Exception as e:
            print(f"Error Global: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
