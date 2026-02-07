import sys
import os
import time
from datetime import datetime
import concurrent.futures
import warnings

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
    sys.exit(f"Library Error: {e}. Install dulu: pip install ccxt pandas pandas_ta mplfinance requests")

# ==========================================
# 2. KONFIGURASI
# ==========================================
API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
API_SECRET = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')

# Telegram Config (Isi manual jika tidak pakai env var)
# Contoh: '123456789:ABCdefGhIJKlmNoPQRstUvwxyz'
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8361349338:AAHOlx4fKz_bp1MHnVg8CxS9MY_pcejxLes') 
# Contoh: '987654321'
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003768840240')


# SETTING STRATEGI
TIMEFRAME = '15m'       # Scan di Timeframe 15 Menit
VOL_MULTIPLIER = 2.0    # Ambang Batas: Volume 2x (200%) dari rata-rata
LIMIT = 200             # Ambil 200 candle (Agar cukup hitung rata-rata 96 candle ke belakang)
TOP_COIN_COUNT = 300    # Scan 300 Koin
MAX_THREADS = 10        # Kecepatan scan

OUTPUT_FOLDER = 'volume_15m_results'
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
    spike_pct = (data['spike_ratio'] * 100) - 100
    
    caption = (
        f"🐋 <b>VOLUME SPIKE 15M ALERT</b>\n"
        f"💎 <b>{symbol}</b>\n"
        f"──────────────────────\n"
        f"📊 <b>Vol Spike:</b> {data['spike_ratio']:.2f}x (Avg 24H)\n"
        f"🔥 <b>Lonjakan:</b> +{spike_pct:.0f}% !!\n"
        f"──────────────────────\n"
        f"🛠 <b>Setup BBMA:</b> {data['signal']} {icon}\n"
        f"🏷 <b>Tipe:</b> {data['tipe']}\n"
        f"💰 <b>Harga:</b> {data['price']}\n"
        f"📝 <b>Analisa:</b>\n{data['explanation']}\n"
        f"──────────────────────\n"
        f"<i>Deteksi Paus Masuk (15m) ⚠️</i>"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        with open(image_path, "rb") as img:
            requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'HTML'}, files={'photo': img}, timeout=20)
    except Exception as e:
        print(f"Gagal kirim TG: {e}")

def generate_chart(df, symbol, signal_info):
    try:
        filename = f"{OUTPUT_FOLDER}/{symbol.replace('/','-')}_{signal_info['tipe']}.png"
        plot_df = df.tail(80).set_index('timestamp') # Gambar 80 candle terakhir
        
        style = mpf.make_mpf_style(base_mpf_style='nightclouds', rc={'font.size': 8})
        adds = [
            mpf.make_addplot(plot_df['BB_Up'], color='green', width=0.8),
            mpf.make_addplot(plot_df['BB_Mid'], color='orange', width=0.8),
            mpf.make_addplot(plot_df['BB_Low'], color='green', width=0.8),
            mpf.make_addplot(plot_df['MA5_Hi'], color='cyan', width=0.6),
            mpf.make_addplot(plot_df['MA5_Lo'], color='magenta', width=0.6),
        ]
        
        # Tambahkan Panel Volume
        # mpf.make_addplot(plot_df['volume'], type='bar', panel=1, color='white', alpha=0.5, width=0.8),

        mpf.plot(plot_df, type='candle', style=style, addplot=adds, 
                 title=f"{symbol} [15M] - Vol Spike {signal_info['spike_ratio']:.1f}x", 
                 savefig=dict(fname=filename, bbox_inches='tight'), volume=True)
        return filename
    except: return None

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
        # Kita butuh LIMIT 200 agar bisa hitung rata-rata 96 candle ke belakang
        bars = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=LIMIT)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except: return None

def add_indicators(df):
    bb = df.ta.bbands(length=20, std=2)
    if bb is not None:
        df['BB_Up'] = bb.iloc[:, 2]; df['BB_Mid'] = bb.iloc[:, 1]; df['BB_Low'] = bb.iloc[:, 0]
    df['MA5_Hi'] = df['high'].rolling(5).mean()
    df['MA5_Lo'] = df['low'].rolling(5).mean()
    df['EMA_50'] = df.ta.ema(length=50)
    return df

# ==========================================
# 6. LOGIKA VOLUME & BBMA
# ==========================================

def analyze_volume_anomaly(df):
    """
    Mengecek apakah Volume 15M terakhir > 2x Rata-rata Volume 24H (96 candle terakhir)
    """
    if df is None or len(df) < 100: return 0
    
    # Volume Candle Close Terakhir (Indeks -2)
    current_vol = df['volume'].iloc[-2]
    
    # Rata-rata 96 candle sebelumnya (24 Jam x 4 candle/jam = 96)
    # Kita ambil slice dari -98 sampai -2
    avg_vol_24h = df['volume'].iloc[-98:-2].mean()
    
    if avg_vol_24h == 0: return 0
    
    ratio = current_vol / avg_vol_24h
    return ratio

def analyze_bbma_setup(df):
    if df is None or len(df) < 55: return None
    c = df.iloc[-2] # Close Candle
    prev = df.iloc[-3]
    
    ema_val = c.get('EMA_50', 0)
    trend = "BULLISH" if c['close'] > ema_val else "BEARISH"
    
    signal_data = None
    tipe = "NONE"

    # --- SETUP BUY ---
    if trend == "BULLISH":
        tipe = "BUY"
        # 1. Extreme Buy
        if c['MA5_Lo'] < c['BB_Low']:
            signal_data = {"signal": "EXTREME", "explanation": "Volume Paus + Extreme Buy (Reversal)."}
        # 2. Re-Entry Buy
        elif c['close'] > c['BB_Mid'] and c['low'] <= c['MA5_Lo']:
            signal_data = {"signal": "RE-ENTRY", "explanation": "Volume Paus + Re-Entry Buy (Diskon)."}
        # 3. Momentum Buy
        elif c['close'] > c['BB_Up']:
            signal_data = {"signal": "MOMENTUM", "explanation": "Volume Paus + Breakout Kuat."}
        # 4. CSA Buy
        elif prev['close'] < c['BB_Mid'] and c['close'] > c['BB_Mid']:
            signal_data = {"signal": "CSA", "explanation": "Volume Paus + Break Mid BB."}

    # --- SETUP SELL ---
    elif trend == "BEARISH":
        tipe = "SELL"
        # 1. Extreme Sell
        if c['MA5_Hi'] > c['BB_Up']:
            signal_data = {"signal": "EXTREME", "explanation": "Volume Paus + Extreme Sell."}
        # 2. Re-Entry Sell
        elif c['close'] < c['BB_Mid'] and c['high'] >= c['MA5_Hi']:
            signal_data = {"signal": "RE-ENTRY", "explanation": "Volume Paus + Re-Entry Sell."}
        # 3. Momentum Sell
        elif c['close'] < c['BB_Low']:
            signal_data = {"signal": "MOMENTUM", "explanation": "Volume Paus + Breakdown Kuat."}
        # 4. CSA Sell
        elif prev['close'] > c['BB_Mid'] and c['close'] < c['BB_Mid']:
            signal_data = {"signal": "CSA", "explanation": "Volume Paus + Break Mid BB."}

    if signal_data:
        signal_data['tipe'] = tipe
        signal_data['price'] = c['close']
        signal_data['time'] = c['timestamp']
        return signal_data
    
    return None

# ==========================================
# 7. WORKER SCAN
# ==========================================
def worker_scan(symbol):
    try:
        # 1. Ambil Data 15M (Limit 200)
        df = fetch_ohlcv(symbol)
        if df is None: return None

        # 2. FILTER VOLUME (ANOMALI CHECK)
        # Apakah volume 15 menit terakhir > 2x rata-rata 24 jam terakhir?
        spike_ratio = analyze_volume_anomaly(df)
        
        # Jika Volume TIDAK meledak, langsung skip
        if spike_ratio < VOL_MULTIPLIER:
            return None

        # 3. CEK BBMA SETUP
        # Jika Volume meledak, baru kita hitung indikator BBMA
        df = add_indicators(df)
        res = analyze_bbma_setup(df)
        
        if res:
            res['symbol'] = symbol
            res['spike_ratio'] = spike_ratio
            res['df'] = df
            return res

    except: pass
    return None

# ==========================================
# 8. MAIN LOOP
# ==========================================
def main():
    print(f"=== VOLUME HUNTER 15M + BBMA BOT ===")
    print(f"Strategi: Volume Spike > {VOL_MULTIPLIER}x (vs Avg 24H) di TF 15M.")
    print(f"Target: Top {TOP_COIN_COUNT} Koin.")
    
    global processed_signals

    while True:
        try:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scan Volume 15M & BBMA...")
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
                    if completed % 20 == 0:
                        sys.stdout.write(f"\rScanning: {completed}/{len(symbols)}...")
                        sys.stdout.flush()
            
            duration = time.time() - start_t
            print(f"\n✅ Selesai ({duration:.2f}s). Ditemukan: {len(alerts_queue)} Koin Hot.")

            # Urutkan berdasarkan lonjakan volume tertinggi
            alerts_queue.sort(key=lambda x: x['spike_ratio'], reverse=True)

            for alert in alerts_queue:
                sym = alert['symbol']
                # Cek Memory berdasarkan waktu candle 15m
                if processed_signals.get(sym) != alert['time']:
                    processed_signals[sym] = alert['time']
                    
                    print(f"🔥 HOT: {sym} (Vol {alert['spike_ratio']:.1f}x) -> {alert['signal']}")
                    
                    img = generate_chart(alert['df'], sym, alert)
                    if img: send_telegram_alert(sym, alert, img)
            
            # Scan 15 Menit bisa dilakukan lebih sering
            print("⏳ Menunggu 30 detik...")
            time.sleep(30)

        except KeyboardInterrupt: break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()


