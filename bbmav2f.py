import sys
import os
import time
from datetime import datetime
import concurrent.futures
import warnings

# Filter warning
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
    sys.exit(f"Library Error: {e}. Install: pip install ccxt pandas pandas_ta mplfinance requests")

# ==========================================
# 2. KONFIGURASI
# ==========================================
API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
API_SECRET = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')

# Telegram Config (Isi manual jika tidak pakai env var)
# Contoh: '123456789:ABCdefGhIJKlmNoPQRstUvwxyz'
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8361349338:AAHOlx4fKz_bp1MHnVg8CxS9MY_pcejxLes') 
# Contoh: '987654321'
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '6018760579')

# STRATEGI: RE-ENTRY (4H) + EXTREME (1H) + MHV (15M)
TF_BIG = '4h'
TF_MID = '1h'
TF_SMALL = '15m'

LIMIT = 100             # Data Candle
TOP_VOL_COUNT = 150     # Hanya scan 150 koin dengan Volume 24H Terbesar
MAX_THREADS = 10        # Kecepatan scan

OUTPUT_FOLDER = 'sniper_results'
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
# 4. FITUR TELEGRAM
# ==========================================
def send_telegram_alert(symbol, vol_24h, data_4h, data_1h, data_15m, image_path):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    
    icon = "🟢" if data_15m['tipe'] == "BUY" else "🔴"
    # Format Volume ke Juta/Miliar
    vol_str = f"${vol_24h / 1_000_000:.2f}M"
    
    caption = (
        f"🎯 <b>BBMA SNIPER PRO (Vol + MTF)</b>\n"
        f"💎 <b>{symbol}</b>\n"
        f"📊 <b>Vol 24H:</b> {vol_str}\n"
        f"──────────────────────\n"
        f"1️⃣ <b>4H (Trend):</b> {data_4h['setup']} {icon}\n"
        f"2️⃣ <b>1H (Confirm):</b> {data_1h['setup']} ✅\n"
        f"3️⃣ <b>15M (Entry):</b> {data_15m['setup']} 🚀\n"
        f"──────────────────────\n"
        f"🏷 <b>Tipe:</b> {data_15m['tipe']} STRONG\n"
        f"💰 <b>Harga:</b> {data_15m['price']}\n"
        f"📝 <b>Logika:</b>\n"
        f"Re-Entry 4H divalidasi Extreme 1H, entry di MHV 15M.\n"
        f"──────────────────────\n"
        f"<i>High Probability Setup ✅</i>"
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
        if 'EMA_50' in plot_df.columns:
            adds.append(mpf.make_addplot(plot_df['EMA_50'], color='yellow', width=1.5))

        mpf.plot(plot_df, type='candle', style=style, addplot=adds, 
                 title=f"{symbol} [15M] - MHV ENTRY", 
                 savefig=dict(fname=filename, bbox_inches='tight'), volume=False)
        return filename
    except: return None

# ==========================================
# 5. DATA ENGINE (VOLUME FILTER)
# ==========================================
def get_high_volume_symbols(limit=150):
    """
    Mengambil koin dengan Volume USDT 24 Jam tertinggi.
    """
    try:
        print("📊 Mengambil data Volume 24 Jam...")
        exchange.load_markets()
        tickers = exchange.fetch_tickers()
        
        # Filter: Hanya USDT, Tidak ada UP/DOWN token
        valid_tickers = []
        for symbol, data in tickers.items():
            if '/USDT' in symbol and 'UP/' not in symbol and 'DOWN/' not in symbol:
                valid_tickers.append({
                    'symbol': symbol,
                    'vol': data['quoteVolume'] if data['quoteVolume'] else 0
                })
        
        # Urutkan dari Volume Terbesar ke Terkecil
        sorted_tickers = sorted(valid_tickers, key=lambda x: x['vol'], reverse=True)
        
        # Ambil Top N
        top_coins = sorted_tickers[:limit]
        return top_coins # Mengembalikan list of dict [{'symbol': 'BTC/USDT', 'vol': 12345}, ...]
    except Exception as e:
        print(f"Gagal ambil data volume: {e}")
        return []

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
# 6. LOGIKA INTI (SPECIFIC BBMA RULES)
# ==========================================

# 1. CEK 4H: HARUS RE-ENTRY (Searah EMA50)
def check_4h_reentry(df):
    if df is None or len(df) < 55: return None
    c = df.iloc[-2] # Close Candle
    
    # Tren Bullish (Harga > EMA50) & Harga > Mid BB
    if c['close'] > c.get('EMA_50', 0) and c['close'] > c['BB_Mid']:
        # Koreksi: Low menyentuh MA5 Low
        if c['low'] <= c['MA5_Lo']:
            return {"setup": "RE-ENTRY BUY", "tipe": "BUY"}
            
    # Tren Bearish (Harga < EMA50) & Harga < Mid BB
    elif c['close'] < c.get('EMA_50', 9999999) and c['close'] < c['BB_Mid']:
        # Koreksi: High menyentuh MA5 High
        if c['high'] >= c['MA5_Hi']:
            return {"setup": "RE-ENTRY SELL", "tipe": "SELL"}
            
    return None

# 2. CEK 1H: HARUS EXTREME (Validasi Re-Entry 4H)
def check_1h_extreme(df, trend_type):
    if df is None or len(df) < 55: return None
    c = df.iloc[-2]

    # Jika 4H Buy, 1H harus Extreme Buy (MA Keluar BB Bawah)
    if trend_type == "BUY":
        if c['MA5_Lo'] < c['BB_Low']:
            return {"setup": "EXTREME BUY", "tipe": "BUY"}
            
    # Jika 4H Sell, 1H harus Extreme Sell (MA Keluar BB Atas)
    elif trend_type == "SELL":
        if c['MA5_Hi'] > c['BB_Up']:
            return {"setup": "EXTREME SELL", "tipe": "SELL"}
            
    return None

# 3. CEK 15M: HARUS MHV (Sniper Entry)
def check_15m_mhv(df, trend_type):
    if df is None or len(df) < 55: return None
    c = df.iloc[-2] # Close Candle

    # MHV BUY
    if trend_type == "BUY":
        # Syarat MHV:
        # 1. Low candle dekat dengan Low BB (misal toleransi 0.5%)
        # 2. Tapi Close Candle di atas Low BB (Gagal Break)
        # 3. MA5 Low sudah masuk kembali ke dalam BB (Tidak Extreme lagi)
        if c['low'] <= c['BB_Low'] * 1.005 and \
           c['close'] > c['BB_Low'] and \
           c['MA5_Lo'] > c['BB_Low']:
            return {"setup": "MHV BUY", "tipe": "BUY", "price": c['close'], "time": c['timestamp']}

    # MHV SELL
    elif trend_type == "SELL":
        # Syarat MHV:
        # 1. High candle dekat dengan Top BB
        # 2. Tapi Close Candle di bawah Top BB
        # 3. MA5 High sudah masuk kembali ke dalam BB
        if c['high'] >= c['BB_Up'] * 0.995 and \
           c['close'] < c['BB_Up'] and \
           c['MA5_Hi'] < c['BB_Up']:
            return {"setup": "MHV SELL", "tipe": "SELL", "price": c['close'], "time": c['timestamp']}

    return None

# ==========================================
# 7. WORKER PROSES
# ==========================================
def worker_scan(coin_data):
    symbol = coin_data['symbol']
    vol_24h = coin_data['vol']
    
    try:
        # TAHAP 1: SCAN 4H (RE-ENTRY)
        df_4h = fetch_ohlcv(symbol, TF_BIG)
        df_4h = add_indicators(df_4h)
        res_4h = check_4h_reentry(df_4h)
        
        if not res_4h: return None # Jika 4H bukan Re-entry, Skip.

        # TAHAP 2: SCAN 1H (EXTREME)
        df_1h = fetch_ohlcv(symbol, TF_MID)
        df_1h = add_indicators(df_1h)
        res_1h = check_1h_extreme(df_1h, res_4h['tipe'])
        
        if not res_1h: return None # Jika 1H bukan Extreme, Skip.

        # TAHAP 3: SCAN 15M (MHV)
        df_15m = fetch_ohlcv(symbol, TF_SMALL)
        df_15m = add_indicators(df_15m)
        res_15m = check_15m_mhv(df_15m, res_4h['tipe'])
        
        if not res_15m: return None # Jika 15M bukan MHV, Skip.

        # JIKA SEMUA LOLOS -> JACKPOT
        return {
            'symbol': symbol,
            'vol': vol_24h,
            '4h': res_4h,
            '1h': res_1h,
            '15m': res_15m,
            'df_chart': df_15m
        }

    except: pass
    return None

# ==========================================
# 8. MAIN LOOP
# ==========================================
def main():
    print(f"=== BBMA SNIPER PRO (Vol + MTF Logic) ===")
    print(f"1. Filter Volume 24H (Top {TOP_VOL_COUNT})")
    print(f"2. Logic: 4H ReEntry -> 1H Extreme -> 15M MHV")
    
    global processed_signals

    while True:
        try:
            # 1. AMBIL TOP VOLUME COINS
            top_coins = get_high_volume_symbols(TOP_VOL_COUNT)
            if not top_coins:
                print("Gagal ambil data volume. Retry...")
                time.sleep(10)
                continue

            print(f"\n🚀 Scanning {len(top_coins)} Koin Paling Liquid...")
            alerts_queue = []
            
            start_t = time.time()
            
            # 2. MULAI SCAN PARALEL
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
                # Kita kirim object coin_data (symbol + volume) ke worker
                futures = {executor.submit(worker_scan, coin): coin['symbol'] for coin in top_coins}
                
                completed = 0
                for future in concurrent.futures.as_completed(futures):
                    res = future.result()
                    if res: alerts_queue.append(res)
                    completed += 1
                    sys.stdout.write(f"\rProgress: {completed}/{len(top_coins)}...")
                    sys.stdout.flush()
            
            duration = time.time() - start_t
            print(f"\n✅ Selesai ({duration:.2f}s). Valid Sinyal: {len(alerts_queue)}")

            # 3. KIRIM HASIL
            for alert in alerts_queue:
                sym = alert['symbol']
                # Filter Duplikasi berdasarkan waktu candle 15m
                if processed_signals.get(sym) != alert['15m']['time']:
                    processed_signals[sym] = alert['15m']['time']
                    
                    print(f"🔥 FOUND: {sym} | Vol: ${alert['vol']/1000000:.1f}M | {alert['15m']['tipe']}")
                    
                    img = generate_chart(alert['df_chart'], sym, alert['15m'])
                    if img: 
                        send_telegram_alert(sym, alert['vol'], alert['4h'], alert['1h'], alert['15m'], img)
            
            print("⏳ Menunggu 1 menit...")
            time.sleep(60)

        except KeyboardInterrupt: break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()


