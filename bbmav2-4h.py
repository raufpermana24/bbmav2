import sys
import os
import time
from datetime import datetime
import concurrent.futures
import warnings

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
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003618941801')

# SETTING STRATEGI
TIMEFRAME_SCAN = '4h'   # Cek Volume di 1 Jam
VOLUME_THRESHOLD = 2.0  # Min. Volume harus 2x lipat dari rata-rata (200%)
LIMIT = 100             
TOP_COIN_COUNT = 300    
MAX_THREADS = 10        

OUTPUT_FOLDER = 'volume_bbma_results'
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
    vol_percent = (data['vol_spike'] * 100) - 100
    
    caption = (
        f"📊 <b>VOLUME SPIKE + BBMA ALERT</b>\n"
        f"💎 <b>{symbol}</b>\n"
        f"──────────────────────\n"
        f"📈 <b>Vol. Spike:</b> {data['vol_spike']:.2f}x (Avg)\n"
        f"🔥 <b>Lonjakan:</b> +{vol_percent:.0f}% !!\n"
        f"──────────────────────\n"
        f"🛠 <b>Signal BBMA:</b> {data['signal']} {icon}\n"
        f"🏷 <b>Tipe:</b> {data['tipe']}\n"
        f"💰 <b>Harga:</b> {data['price']}\n"
        f"📝 <b>Analisa:</b>\n{data['explanation']}\n"
        f"──────────────────────\n"
        f"<i>Koin sedang trending/pump! 🚀</i>"
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
        plot_df = df.tail(60).set_index('timestamp')
        
        style = mpf.make_mpf_style(base_mpf_style='nightclouds', rc={'font.size': 8})
        adds = [
            mpf.make_addplot(plot_df['BB_Up'], color='green', width=0.8),
            mpf.make_addplot(plot_df['BB_Mid'], color='orange', width=0.8),
            mpf.make_addplot(plot_df['BB_Low'], color='green', width=0.8),
            mpf.make_addplot(plot_df['MA5_Hi'], color='cyan', width=0.6),
            mpf.make_addplot(plot_df['MA5_Lo'], color='magenta', width=0.6),
        ]
        # Tambahkan indikator volume di chart
        # mpf.make_addplot(plot_df['volume'], panel=1, color='white', type='bar', width=0.7),

        mpf.plot(plot_df, type='candle', style=style, addplot=adds, 
                 title=f"{symbol} [1H Vol Spike {signal_info['vol_spike']:.1f}x]", 
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

def fetch_ohlcv(symbol, timeframe):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe, limit=LIMIT)
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
# 6. LOGIKA VOLUME SPIKE & BBMA
# ==========================================

def analyze_volume_spike(df):
    """
    Mengecek apakah volume candle terakhir meledak dibanding rata-rata
    """
    if df is None or len(df) < 25: return 0
    
    # Volume Candle Close Terakhir
    current_vol = df['volume'].iloc[-2]
    
    # Rata-rata volume 24 candle sebelumnya (24 Jam ke belakang)
    avg_vol = df['volume'].iloc[-26:-2].mean()
    
    if avg_vol == 0: return 0
    
    # Rasio Spike (Misal: 2.5x rata-rata)
    spike_ratio = current_vol / avg_vol
    return spike_ratio

def analyze_bbma_setup(df):
    if df is None or len(df) < 55: return None
    c = df.iloc[-2] # Close Candle
    prev = df.iloc[-3]

    ema_val = c.get('EMA_50', 0)
    is_uptrend = c['close'] > ema_val
    is_downtrend = c['close'] < ema_val
    
    signal_data = None
    tipe = "NONE"

    # --- SETUP BUY ---
    if is_uptrend:
        tipe = "BUY"
        if c['MA5_Lo'] < c['BB_Low']:
            signal_data = {"signal": "EXTREME", "explanation": "Volume Tinggi + Extreme Buy (Reversal)."}
        elif c['close'] > c['BB_Mid'] and c['low'] <= c['MA5_Lo']:
            signal_data = {"signal": "RE-ENTRY", "explanation": "Volume Tinggi + Re-Entry Buy (Diskon)."}
        elif c['close'] > c['BB_Up']:
            signal_data = {"signal": "MOMENTUM", "explanation": "Volume Tinggi + Breakout Momentum."}
        elif prev['close'] < c['BB_Mid'] and c['close'] > c['BB_Mid']:
            signal_data = {"signal": "CSA", "explanation": "Volume Tinggi + Break Mid BB."}

    # --- SETUP SELL ---
    elif is_downtrend:
        tipe = "SELL"
        if c['MA5_Hi'] > c['BB_Up']:
            signal_data = {"signal": "EXTREME", "explanation": "Volume Tinggi + Extreme Sell."}
        elif c['close'] < c['BB_Mid'] and c['high'] >= c['MA5_Hi']:
            signal_data = {"signal": "RE-ENTRY", "explanation": "Volume Tinggi + Re-Entry Sell."}
        elif c['close'] < c['BB_Low']:
            signal_data = {"signal": "MOMENTUM", "explanation": "Volume Tinggi + Breakdown Momentum."}
        elif prev['close'] > c['BB_Mid'] and c['close'] < c['BB_Mid']:
            signal_data = {"signal": "CSA", "explanation": "Volume Tinggi + Break Mid BB."}

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
        # Ambil data 1 Jam
        df = fetch_ohlcv(symbol, TIMEFRAME_SCAN)
        if df is None: return None

        # 1. CEK VOLUME DULU (Filter Awal)
        spike = analyze_volume_spike(df)
        
        # Jika Volume TIDAK meledak, langsung skip (Hemat waktu)
        if spike < VOLUME_THRESHOLD: 
            return None 

        # 2. Jika Volume Bagus, baru CEK BBMA
        df = add_indicators(df)
        res = analyze_bbma_setup(df)
        
        if res:
            res['symbol'] = symbol
            res['vol_spike'] = spike
            res['df'] = df
            return res

    except: pass
    return None

# ==========================================
# 8. MAIN LOOP
# ==========================================
def main():
    print(f"=== VOLUME SPIKE + BBMA BOT ===")
    print(f"Strategi: Cari koin dgn Volume {VOLUME_THRESHOLD}x rata-rata 24H -> Cek BBMA")
    print(f"Timeframe: {TIMEFRAME_SCAN} | Target: {TOP_COIN_COUNT} Koin")
    
    global processed_signals

    while True:
        try:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scan Volume & BBMA...")
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
            print(f"\n✅ Selesai ({duration:.2f}s). Ditemukan: {len(alerts_queue)}")

            # Urutkan hasil berdasarkan lonjakan volume tertinggi
            alerts_queue.sort(key=lambda x: x['vol_spike'], reverse=True)

            for alert in alerts_queue:
                sym = alert['symbol']
                # Cek Memory
                if processed_signals.get(sym) != alert['time']:
                    processed_signals[sym] = alert['time']
                    
                    print(f"🔥 HOT: {sym} (Vol {alert['vol_spike']:.1f}x) -> {alert['signal']}")
                    
                    img = generate_chart(alert['df'], sym, alert)
                    if img: send_telegram_alert(sym, alert, img)
            
            print("⏳ Menunggu 1 menit...")
            time.sleep(60)

        except KeyboardInterrupt: break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()


