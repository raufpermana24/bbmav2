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
import gc
import matplotlib
from datetime import datetime

# ==========================================
# KONFIGURASI PENTING (ANTI CRASH)
# ==========================================
# Set backend matplotlib ke 'Agg' agar tidak error di VPS/Terminal tanpa layar
matplotlib.use('Agg')

# ==========================================
# BAGIAN 1: KONFIGURASI PENGGUNA (ISI DI SINI)
# ==========================================

# Masukkan Token & ID Telegram Anda di dalam tanda kutip
TELEGRAM_TOKEN = '8361349338:AAHOlx4fKz_bp1MHnVg8CxS9MY_pcejxLes'    # Contoh: '123456789:ABCdef...'
TELEGRAM_CHAT_ID = '-1003558146379'  # Contoh: '987654321'

# Masukkan API Key Binance Anda
BINANCE_API_KEY = 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97'
BINANCE_SECRET_KEY = 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB'

# Pengaturan Scanner (Disesuaikan agar lebih hemat RAM)
SCAN_INTERVAL = 30      # Interval diperlambat sedikit (30s) agar CPU adem
COIN_LIMIT = 300        # Jumlah koin
MAX_WORKERS = 15        # TURUNKAN DARI 40 KE 15 UNTUK MENCEGAH CRASH/OOM

# Manajemen Risiko
RISK_PER_TRADE = 0.01   
DEFAULT_BALANCE = 1000  

# ==========================================
# BAGIAN 2: LOGIKA SISTEM
# ==========================================

# Coba load .env
try:
    from dotenv import load_dotenv
    load_dotenv()
    if not TELEGRAM_TOKEN: TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    if not TELEGRAM_CHAT_ID: TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
    if not BINANCE_API_KEY: BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
    if not BINANCE_SECRET_KEY: BINANCE_SECRET_KEY = os.getenv('BINANCE_SECRET_KEY')
except ImportError:
    pass

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
    try:
        # Tambahkan timeout agar thread tidak menggantung selamanya
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        if not ohlcv or len(ohlcv) < limit: return None
        df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        df['time'] = pd.to_datetime(df['time'], unit='ms')
        return df
    except Exception:
        return None

def send_telegram_photo(photo_path, caption):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        with open(photo_path, 'rb') as f:
            requests.post(url, files={'photo': f}, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'}, timeout=15)
    except Exception as e:
        print(f"⚠️ Gagal kirim Telegram: {e}")

# --- LOGIKA SMC ---

def identify_structure(df):
    try:
        df['swing_high'] = df['high'].rolling(window=5, center=True).max()
        df['swing_low'] = df['low'].rolling(window=5, center=True).min()
        
        highs = df[df['high'] == df['swing_high']]['high'].values
        lows = df[df['low'] == df['swing_low']]['low'].values
        
        close = df.iloc[-1]['close']
        trend = 'NEUTRAL'
        
        if len(highs) > 0 and close > highs[-1]:
            trend = 'BULLISH'
        elif len(lows) > 0 and close < lows[-1]:
            trend = 'BEARISH'
        return trend
    except:
        return 'NEUTRAL'

def find_poi(df, trend):
    try:
        poi_zone = None 
        for i in range(len(df)-2, len(df)-20, -1):
            if trend == 'BULLISH':
                curr_low = df.iloc[i]['low']
                prev_high = df.iloc[i-2]['high']
                if curr_low > prev_high:
                    poi_zone = (prev_high, curr_low)
                    break
            elif trend == 'BEARISH':
                curr_high = df.iloc[i]['high']
                prev_low = df.iloc[i-2]['low']
                if curr_high < prev_low:
                    poi_zone = (curr_high, prev_low)
                    break
        return poi_zone
    except:
        return None

# --- ANALISIS CORE ---

def analyze_mtf_setup(symbol, balance):
    try:
        # Gunakan try-except per koin agar 1 error tidak mematikan semua thread
        exchange = get_exchange_instance()
        
        # 1. DAILY
        df_d1 = fetch_data(exchange, symbol, '1d', limit=60)
        if df_d1 is None: return None
        daily_bias = identify_structure(df_d1)
        if daily_bias == 'NEUTRAL': return None

        # 2. H4
        df_h4 = fetch_data(exchange, symbol, '4h', limit=60)
        if df_h4 is None: return None
        h4_trend = identify_structure(df_h4)
        if h4_trend != daily_bias: return None
        
        poi = find_poi(df_h4, daily_bias)
        if poi is None: return None
        
        curr_price = df_h4.iloc[-1]['close']
        poi_bot, poi_top = poi
        is_in_zone = False
        
        if daily_bias == 'BULLISH':
            if curr_price <= poi_top * 1.01 and curr_price >= poi_bot * 0.98: is_in_zone = True
        elif daily_bias == 'BEARISH':
            if curr_price >= poi_bot * 0.99 and curr_price <= poi_top * 1.02: is_in_zone = True
                
        if not is_in_zone: return None

        # 3. H1
        df_h1 = fetch_data(exchange, symbol, '1h', limit=60)
        if df_h1 is None: return None
        
        h1_close = df_h1.iloc[-1]['close']
        h1_low_recent = df_h1.iloc[-5:]['low'].min()
        h1_high_recent = df_h1.iloc[-5:]['high'].max()
        
        setup = None
        
        if daily_bias == 'BULLISH':
            sl = h1_low_recent * 0.995 
            risk = abs(h1_close - sl)
            tp = h1_close + (risk * 2) 
            qty_usd = (balance * RISK_PER_TRADE) / (risk / h1_close) if risk > 0 else 0
            qty_coin = qty_usd / h1_close
            
            setup = {
                's': symbol, 'side': 'BUY 🟢', 'bias': daily_bias,
                'p': h1_close, 'sl': sl, 'tp': tp, 
                'qty': qty_coin, 'risk_usd': balance * RISK_PER_TRADE,
                'poi_txt': f"H4 FVG ({poi_bot:.4f}-{poi_top:.4f})",
                'df': df_h1 
            }

        elif daily_bias == 'BEARISH':
            sl = h1_high_recent * 1.005
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

# --- CHART ---

def generate_chart(data):
    try:
        symbol = data['s'].replace('/', '')
        filename = f"{SCREENSHOT_DIR}/{symbol}_MTF.png"
        
        mc = mpf.make_marketcolors(up='#089981', down='#F23645', inherit=True)
        # PERBAIKAN: Ganti 'style' menjadi 'base_mpf_style'
        s  = mpf.make_mpf_style(marketcolors=mc, base_mpf_style='nightclouds', gridstyle=':')
        
        df = data['df'].tail(50).set_index(pd.DatetimeIndex(data['df'].tail(50)['time']))
        
        lines = dict(
            hlines=[data['p'], data['sl'], data['tp']], 
            colors=['blue', 'red', 'green'], 
            linewidths=[1, 1.5, 1.5],
            linestyle='-.'
        )
        
        title = f"\n{data['s']} Setup [1D Bias: {data['bias']}]"
        
        # Penting: Close figure setelah save untuk hemat RAM
        mpf.plot(df, type='candle', style=s, title=title, hlines=lines,
                 savefig=dict(fname=filename, dpi=100, bbox_inches='tight'))
        plt = matplotlib.pyplot
        plt.close('all') # Wajib untuk mencegah Memory Leak di Matplotlib
        
        return filename
    except Exception as e:
        print(f"Chart Error: {e}")
        return None

# --- MAIN LOOP ---

def get_top_coins():
    try:
        exchange = get_exchange_instance()
        tickers = exchange.fetch_tickers()
        data = []
        for s, t in tickers.items():
            if '/USDT' in s and 'UP/' not in s and 'DOWN/' not in s:
                vol = t.get('quoteVolume', 0)
                if vol > 10000000: 
                    data.append(s)
        data.sort(key=lambda x: tickers[x]['quoteVolume'], reverse=True)
        return data[:COIN_LIMIT]
    except:
        return []

if __name__ == '__main__':
    print(f"🚀 **SMC MTF BOT STARTED (STABLE VERSION)**")
    print(f"🎯 Workers: {MAX_WORKERS} | Interval: {SCAN_INTERVAL}s")
    
    if not BINANCE_API_KEY or not TELEGRAM_TOKEN:
        print("❌ ERROR: API Key / Token Kosong! Program berhenti.")
        print("-> Edit file ini dan isi variabel di bagian atas.")
        sys.exit(1)

    current_balance = DEFAULT_BALANCE
    try:
        exch = get_exchange_instance()
        bal = exch.fetch_balance()
        current_balance = bal['USDT']['free']
        print(f"💰 Saldo: ${current_balance:.2f} USDT")
    except:
        print(f"⚠️ Gagal cek saldo, default ${DEFAULT_BALANCE}")

    cached_coins = []
    last_coin_update = 0

    while True:
        try:
            loop_start = time.time()
            
            # Garbage Collection (Membersihkan RAM)
            gc.collect() 

            if not cached_coins or (loop_start - last_coin_update) > 3600:
                print("🔄 Updating Coins...")
                cached_coins = get_top_coins()
                last_coin_update = loop_start
            
            print(f"\nScanning {len(cached_coins)} coins...", end='')
            
            results = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {executor.submit(analyze_mtf_setup, sym, current_balance): sym for sym in cached_coins}
                for f in concurrent.futures.as_completed(futures):
                    res = f.result()
                    if res: results.append(res)
            
            print(f" Done. {len(results)} Signals.")
            
            for setup in results:
                print(f"⚡ Signal: {setup['s']}")
                img_path = generate_chart(setup)
                
                if img_path:
                    caption = (
                        f"💎 **SMC MTF SETUP**\n"
                        f"*{setup['s']}* ({setup['side']})\n"
                        f"Bias: {setup['bias']} | POI: Active\n\n"
                        f"Entry: `{setup['p']}`\n"
                        f"SL: `{setup['sl']:.4f}`\n"
                        f"TP: `{setup['tp']:.4f}`\n\n"
                        f"Risk: ${setup['risk_usd']:.2f}"
                    )
                    send_telegram_photo(img_path, caption)
            
            elapsed = time.time() - loop_start
            sleep_time = max(0, SCAN_INTERVAL - elapsed)
            print(f"💤 Sleep {sleep_time:.1f}s...")
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\nBot Stopped User.")
            break
        except Exception as e:
            print(f"⚠️ Error Loop: {e}")
            time.sleep(10)




