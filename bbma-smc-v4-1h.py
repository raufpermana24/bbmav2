import ccxt
import pandas as pd
import pandas_ta as ta
import requests
import os
import time
import sys
import concurrent.futures
import mplfinance as mpf
import numpy as np
from datetime import datetime

# ==========================================
# BAGIAN 1: KONFIGURASI PENGGUNA (ISI DI SINI)
# ==========================================

# Masukkan Token & ID Telegram Anda di dalam tanda kutip
TELEGRAM_TOKEN = '8361349338:AAHOlx4fKz_bp1MHnVg8CxS9MY_pcejxLes'    # Contoh: '123456789:ABCdef...'
TELEGRAM_CHAT_ID = '-1003558146379'  # Contoh: '987654321'

# Masukkan API Key Binance Anda
BINANCE_API_KEY = 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97'
BINANCE_SECRET_KEY = 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB'

# Pengaturan Scanner
SCAN_INTERVAL = 10      # Jeda waktu scan (detik)
COIN_LIMIT = 300        # Jumlah koin teratas yang discan
MAX_WORKERS = 40        # Kecepatan scan (Threads)

# Manajemen Risiko
RISK_PER_TRADE = 0.01   # Risiko 1% per trade
DEFAULT_BALANCE = 1000  # Saldo asumsi (jika gagal baca saldo akun)

# ==========================================
# BAGIAN 2: LOGIKA SISTEM (JANGAN UBAH DI BAWAH INI)
# ==========================================

# Coba load .env jika variabel di atas kosong
try:
    from dotenv import load_dotenv
    load_dotenv()
    if not TELEGRAM_TOKEN: TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    if not TELEGRAM_CHAT_ID: TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
    if not BINANCE_API_KEY: BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
    if not BINANCE_SECRET_KEY: BINANCE_SECRET_KEY = os.getenv('BINANCE_SECRET_KEY')
except ImportError:
    pass # Jika library dotenv tidak ada, abaikan

# Folder Screenshot
SCREENSHOT_DIR = 'screenshots'
if not os.path.exists(SCREENSHOT_DIR):
    os.makedirs(SCREENSHOT_DIR)

# --- FUNGSI UTILITIES ---

def get_exchange_instance():
    """Membuat koneksi ke Binance"""
    return ccxt.binance({
        'apiKey': BINANCE_API_KEY, 
        'secret': BINANCE_SECRET_KEY, 
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    })

def fetch_data(exchange, symbol, timeframe, limit=50):
    """Mengambil data Candle (OHLCV)"""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        if not ohlcv or len(ohlcv) < limit: return None
        df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        df['time'] = pd.to_datetime(df['time'], unit='ms')
        return df
    except:
        return None

def send_telegram_photo(photo_path, caption):
    """Mengirim Notifikasi + Gambar ke Telegram"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        with open(photo_path, 'rb') as f:
            requests.post(url, files={'photo': f}, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'})
    except Exception as e:
        print(f"Gagal kirim Telegram: {e}")

# --- LOGIKA SMC (SMART MONEY CONCEPTS) ---

def identify_structure(df):
    """
    Menentukan Bias Market (1D/H4)
    Menggunakan Fractal High/Low sederhana
    """
    # Fractal 5 candle (Swing High/Low)
    df['swing_high'] = df['high'].rolling(window=5, center=True).max()
    df['swing_low'] = df['low'].rolling(window=5, center=True).min()
    
    # Ambil swing points
    highs = df[df['high'] == df['swing_high']]['high'].values
    lows = df[df['low'] == df['swing_low']]['low'].values
    
    close = df.iloc[-1]['close']
    trend = 'NEUTRAL'
    
    # Logika Structure Break sederhana
    if len(highs) > 0 and close > highs[-1]:
        trend = 'BULLISH' # Harga break High terakhir
    elif len(lows) > 0 and close < lows[-1]:
        trend = 'BEARISH' # Harga break Low terakhir
        
    return trend

def find_poi(df, trend):
    """
    Mencari Area Minat (FVG - Fair Value Gap) di H4
    """
    poi_zone = None # (Harga Bawah, Harga Atas)
    
    # Loop mundur dari candle terakhir untuk cari Imbalance terdekat
    for i in range(len(df)-2, len(df)-20, -1):
        if trend == 'BULLISH':
            # Bullish FVG: Low candle[i] > High candle[i-2]
            curr_low = df.iloc[i]['low']
            prev_high = df.iloc[i-2]['high']
            if curr_low > prev_high:
                poi_zone = (prev_high, curr_low) # Zone FVG
                break
                
        elif trend == 'BEARISH':
            # Bearish FVG: High candle[i] < Low candle[i-2]
            curr_high = df.iloc[i]['high']
            prev_low = df.iloc[i-2]['low']
            if curr_high < prev_low:
                poi_zone = (curr_high, prev_low)
                break
                
    return poi_zone

# --- ANALISIS MULTI-TIMEFRAME (CORE ENGINE) ---

def analyze_mtf_setup(symbol, balance):
    try:
        exchange = get_exchange_instance()
        
        # 1. CEK TIMEFRAME DAILY (1D) - BIAS UTAMA
        df_d1 = fetch_data(exchange, symbol, '1d', limit=60)
        if df_d1 is None: return None
        
        daily_bias = identify_structure(df_d1)
        if daily_bias == 'NEUTRAL': return None # Skip jika market sideways

        # 2. CEK TIMEFRAME H4 (4H) - POI / RETRACEMENT
        df_h4 = fetch_data(exchange, symbol, '4h', limit=60)
        if df_h4 is None: return None
        
        # Pastikan struktur H4 searah dengan Daily
        h4_trend = identify_structure(df_h4)
        if h4_trend != daily_bias: return None
        
        # Cari POI (FVG) di H4
        poi = find_poi(df_h4, daily_bias)
        if poi is None: return None # Tidak ada area menarik
        
        # Cek apakah harga sedang di area Diskon/Premium (Pullback ke POI)
        curr_price = df_h4.iloc[-1]['close']
        poi_bot, poi_top = poi
        is_in_zone = False
        
        if daily_bias == 'BULLISH':
            # Harga pullback turun ke area FVG Bullish
            if curr_price <= poi_top * 1.01 and curr_price >= poi_bot * 0.98:
                is_in_zone = True
        elif daily_bias == 'BEARISH':
            # Harga pullback naik ke area FVG Bearish
            if curr_price >= poi_bot * 0.99 and curr_price <= poi_top * 1.02:
                is_in_zone = True
                
        if not is_in_zone: return None

        # 3. CEK TIMEFRAME H1 (1H) - EKSEKUSI / TRIGGER
        df_h1 = fetch_data(exchange, symbol, '1h', limit=60)
        if df_h1 is None: return None
        
        # Hitung SL & TP berdasarkan struktur H1
        h1_close = df_h1.iloc[-1]['close']
        h1_low_recent = df_h1.iloc[-5:]['low'].min()
        h1_high_recent = df_h1.iloc[-5:]['high'].max()
        
        setup = None
        
        if daily_bias == 'BULLISH':
            sl = h1_low_recent * 0.995 # SL sedikit di bawah low H1
            risk = abs(h1_close - sl)
            tp = h1_close + (risk * 2) # Reward minimal 1:2
            
            qty_usd = (balance * RISK_PER_TRADE) / (risk / h1_close) if risk > 0 else 0
            qty_coin = qty_usd / h1_close
            
            setup = {
                's': symbol, 'side': 'BUY 🟢', 'bias': daily_bias,
                'p': h1_close, 'sl': sl, 'tp': tp, 
                'qty': qty_coin, 'risk_usd': balance * RISK_PER_TRADE,
                'poi_txt': f"H4 FVG ({poi_bot:.4f}-{poi_top:.4f})",
                'df': df_h1 # Data chart pakai H1
            }

        elif daily_bias == 'BEARISH':
            sl = h1_high_recent * 1.005 # SL sedikit di atas high H1
            risk = abs(sl - h1_close)
            tp = h1_close - (risk * 2)
            
            qty_usd = (balance * RISK_PER_TRADE) / (risk / h1_close) if risk > 0 else 0
            qty_coin = qty_usd / h1_close
            
            setup = {
                's': symbol, 'side': 'SELL 🔴', 'bias': daily_bias,
                'p': h1_close, 'sl': sl, 'tp': tp,
                'qty': qty_coin, 'risk_usd': balance * RISK_PER_TRADE,
                'poi_txt': f"H4 FVG ({poi_bot:.4f}-{poi_top:.4f})",
                'df': df_h1
            }
            
        return setup

    except Exception:
        return None

# --- CHART GENERATOR ---

def generate_chart(data):
    try:
        symbol = data['s'].replace('/', '')
        filename = f"{SCREENSHOT_DIR}/{symbol}_MTF.png"
        
        # Setup Tampilan Chart
        mc = mpf.make_marketcolors(up='#089981', down='#F23645', inherit=True)
        s  = mpf.make_mpf_style(marketcolors=mc, style='nightclouds', gridstyle=':')
        
        # Ambil 50 candle terakhir H1
        df = data['df'].tail(50).set_index(pd.DatetimeIndex(data['df'].tail(50)['time']))
        
        # Garis Entry, SL, TP
        lines = dict(
            hlines=[data['p'], data['sl'], data['tp']], 
            colors=['blue', 'red', 'green'], 
            linewidths=[1, 1.5, 1.5],
            linestyle='-.'
        )
        
        title = f"\n{data['s']} Setup [1D Bias: {data['bias']}]"
        
        mpf.plot(df, type='candle', style=s, title=title, hlines=lines,
                 savefig=dict(fname=filename, dpi=100, bbox_inches='tight'))
        return filename
    except Exception as e:
        print(f"Gagal generate chart {data['s']}: {e}")
        return None

# --- FUNGSI UTAMA (MAIN LOOP) ---

def get_top_coins():
    """Ambil daftar koin dengan volume tertinggi"""
    try:
        exchange = get_exchange_instance()
        tickers = exchange.fetch_tickers()
        data = []
        for s, t in tickers.items():
            if '/USDT' in s and 'UP/' not in s and 'DOWN/' not in s:
                vol = t.get('quoteVolume', 0)
                if vol > 10000000: # Filter Volume > $10 Juta
                    data.append(s)
        
        # Sort by volume descending
        data.sort(key=lambda x: tickers[x]['quoteVolume'], reverse=True)
        return data[:COIN_LIMIT]
    except:
        return []

if __name__ == '__main__':
    print(f"🚀 **SMC MTF BOT STARTED**")
    print(f"🎯 Target: {COIN_LIMIT} Coins | Interval: {SCAN_INTERVAL}s")
    
    # Cek Kredensial
    if not BINANCE_API_KEY or not TELEGRAM_TOKEN:
        print("❌ ERROR: API Key atau Telegram Token belum diisi di bagian atas script!")
        sys.exit()

    # Cek Saldo Awal
    current_balance = DEFAULT_BALANCE
    try:
        exch = get_exchange_instance()
        bal = exch.fetch_balance()
        current_balance = bal['USDT']['free']
        print(f"💰 Saldo Terdeteksi: ${current_balance:.2f} USDT")
    except Exception as e:
        print(f"⚠️ Gagal cek saldo, menggunakan default ${DEFAULT_BALANCE}")

    cached_coins = []
    last_coin_update = 0

    while True:
        try:
            loop_start = time.time()
            
            # Update daftar koin tiap 1 jam
            if not cached_coins or (loop_start - last_coin_update) > 3600:
                print("🔄 Mengupdate daftar koin...")
                cached_coins = get_top_coins()
                last_coin_update = loop_start
            
            print(f"\nScanning MTF SMC pada {len(cached_coins)} koin...", end='')
            
            results = []
            # Scan Paralel
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {executor.submit(analyze_mtf_setup, sym, current_balance): sym for sym in cached_coins}
                for f in concurrent.futures.as_completed(futures):
                    res = f.result()
                    if res: results.append(res)
            
            print(f" Selesai. Ditemukan {len(results)} setup valid.")
            
            # Proses Hasil
            for setup in results:
                print(f"⚡ Mengirim Sinyal: {setup['s']}")
                
                # Buat Chart
                img_path = generate_chart(setup)
                
                if img_path:
                    # Caption Telegram
                    caption = (
                        f"💎 **SMC MTF SETUP (1D-4H-1H)**\n\n"
                        f"Symbol: *{setup['s']}*\n"
                        f"Side: *{setup['side']}*\n"
                        f"Bias: *{setup['bias']}*\n"
                        f"POI: *{setup['poi_txt']}*\n\n"
                        f"🔵 Entry: `{setup['p']}`\n"
                        f"🔴 Stop Loss: `{setup['sl']:.4f}`\n"
                        f"🟢 Take Profit: `{setup['tp']:.4f}`\n\n"
                        f"⚖️ **Risk Management**:\n"
                        f"Risk Amount: ${setup['risk_usd']:.2f}\n"
                        f"Size: `{setup['qty']:.4f} coin`"
                    )
                    send_telegram_photo(img_path, caption)
            
            # Sleep Presisi
            elapsed = time.time() - loop_start
            sleep_time = max(0, SCAN_INTERVAL - elapsed)
            print(f"💤 Sleep {sleep_time:.2f} detik...")
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\nBot Dimatikan.")
            break
        except Exception as e:
            print(f"Error Loop Utama: {e}")
            time.sleep(20)
