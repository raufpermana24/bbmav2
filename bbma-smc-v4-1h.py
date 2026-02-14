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
from dotenv import load_dotenv

# ================= 1. KONFIGURASI =================
load_dotenv()

TELEGRAM_TOKEN = os.environ.get('8361349338:AAHOlx4fKz_bp1MHnVg8CxS9MY_pcejxLes')
TELEGRAM_CHAT_ID = os.environ.get('-1003558146379')
BINANCE_API_KEY = os.environ.get('fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
BINANCE_SECRET_KEY = os.environ.get('FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')

# Folder Screenshot
SCREENSHOT_DIR = 'screenshots'
if not os.path.exists(SCREENSHOT_DIR):
    os.makedirs(SCREENSHOT_DIR)

# Konfigurasi Scanner
COIN_LIMIT = 300       # Target 300 Koin
SCAN_INTERVAL = 10     # Sleep 20 detik
MAX_WORKERS = 40       # Thread tinggi untuk handle MTF

# Risk Management
RISK_PER_TRADE = 0.01  # 1%
DEFAULT_BALANCE = 1000

# ================= 2. ENGINE SMC (MULTI-TIMEFRAME) =================

def get_exchange_instance():
    return ccxt.binance({
        'apiKey': BINANCE_API_KEY, 
        'secret': BINANCE_SECRET_KEY, 
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    })

def fetch_data(exchange, symbol, timeframe, limit=50):
    """Fungsi helper untuk ambil data OHLCV"""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        if not ohlcv or len(ohlcv) < limit: return None
        df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        df['time'] = pd.to_datetime(df['time'], unit='ms')
        return df
    except:
        return None

def identify_structure(df):
    """Menentukan Struktur Market (HH/HL)"""
    # Simple Fractal (3 candle)
    df['swing_high'] = df['high'].rolling(window=5, center=True).max()
    df['swing_low'] = df['low'].rolling(window=5, center=True).min()
    
    highs = df[df['high'] == df['swing_high']]['high'].values
    lows = df[df['low'] == df['swing_low']]['low'].values
    
    close = df.iloc[-1]['close']
    trend = 'NEUTRAL'
    
    # Logic Sederhana: Close di atas High terakhir = Bullish
    if len(highs) > 0 and close > highs[-1]:
        trend = 'BULLISH'
    elif len(lows) > 0 and close < lows[-1]:
        trend = 'BEARISH'
        
    return trend

def find_poi(df, trend):
    """Mencari FVG/Imbalance terdekat pada H4"""
    # Deteksi FVG
    fvg_zone = None
    
    if trend == 'BULLISH':
        # Cari Bullish FVG (Low candle i > High candle i-2)
        # Kita loop mundur dari candle terakhir
        for i in range(len(df)-2, len(df)-20, -1): # Cek 20 candle terakhir
            low_curr = df.iloc[i]['low']
            high_prev = df.iloc[i-2]['high']
            if low_curr > high_prev:
                # FVG Ditemukan
                fvg_zone = (high_prev, low_curr) # (Bawah, Atas)
                break
                
    elif trend == 'BEARISH':
        # Cari Bearish FVG (High candle i < Low candle i-2)
        for i in range(len(df)-2, len(df)-20, -1):
            high_curr = df.iloc[i]['high']
            low_prev = df.iloc[i-2]['low']
            if high_curr < low_prev:
                fvg_zone = (high_curr, low_prev) # (Bawah, Atas)
                break
                
    return fvg_zone

# ================= 3. ANALISIS UTAMA (TOP-DOWN) =================

def analyze_mtf(symbol, balance):
    try:
        # Kita butuh instance exchange per thread
        exchange = get_exchange_instance()
        
        # --- TAHAP 1: DAILY (1D) - BIAS ---
        df_daily = fetch_data(exchange, symbol, '1d', limit=50)
        if df_daily is None: return None
        
        daily_trend = identify_structure(df_daily)
        
        # Filter 1: Jika Daily Neutral (Sideways parah), skip.
        if daily_trend == 'NEUTRAL': return None

        # --- TAHAP 2: H4 (4H) - POI ---
        df_h4 = fetch_data(exchange, symbol, '4h', limit=50)
        if df_h4 is None: return None
        
        # Cek apakah struktur H4 selaras dengan Daily (Alignment)
        h4_trend = identify_structure(df_h4)
        if h4_trend != daily_trend: return None # Struktur internal belum valid
        
        # Cari Area Minat (FVG) di H4
        poi_zone = find_poi(df_h4, daily_trend)
        
        # Filter 2: Jika tidak ada FVG jelas di H4 dekat harga, skip
        if poi_zone is None: return None
        
        current_price = df_h4.iloc[-1]['close']
        
        # Cek apakah harga sedang berada di dalam atau dekat POI (Retracement)
        in_zone = False
        poi_bottom, poi_top = poi_zone
        
        if daily_trend == 'BULLISH':
            # Harga pullback ke area FVG Bullish
            if current_price <= poi_top * 1.01 and current_price >= poi_bottom * 0.98:
                in_zone = True
        elif daily_trend == 'BEARISH':
            # Harga pullback ke area FVG Bearish
            if current_price >= poi_bottom * 0.99 and current_price <= poi_top * 1.02:
                in_zone = True
                
        if not in_zone: return None # Harga belum masuk area minat

        # --- TAHAP 3: H1 (1H) - EKSEKUSI ---
        df_h1 = fetch_data(exchange, symbol, '1h', limit=50)
        if df_h1 is None: return None
        
        # Di H1 kita cari konfirmasi final (Swing structure break kecil / Rejection)
        # Simpelnya: Kita pakai validasi candle terakhir menolak area
        
        h1_close = df_h1.iloc[-1]['close']
        h1_low = df_h1.iloc[-5:]['low'].min()
        h1_high = df_h1.iloc[-5:]['high'].max()
        
        setup = None
        
        # ENTRY LOGIC
        if daily_trend == 'BULLISH':
            sl = h1_low * 0.995 # SL di bawah low H1
            tp = h1_close + ((h1_close - sl) * 2) # RR 1:2
            qty = (balance * RISK_PER_TRADE) / abs(h1_close - sl)
            setup = {
                's': symbol, 'side': 'BUY 🟢', 'bias': 'BULLISH',
                'p': h1_close, 'sl': sl, 'tp': tp, 'qty': qty,
                'poi': f"H4 FVG ({poi_bottom:.4f}-{poi_top:.4f})",
                'df': df_h1 # Kirim data H1 untuk chart
            }
            
        elif daily_trend == 'BEARISH':
            sl = h1_high * 1.005 # SL di atas high H1
            tp = h1_close - ((sl - h1_close) * 2) # RR 1:2
            qty = (balance * RISK_PER_TRADE) / abs(sl - h1_close)
            setup = {
                's': symbol, 'side': 'SELL 🔴', 'bias': 'BEARISH',
                'p': h1_close, 'sl': sl, 'tp': tp, 'qty': qty,
                'poi': f"H4 FVG ({poi_bottom:.4f}-{poi_top:.4f})",
                'df': df_h1
            }
            
        return setup

    except Exception:
        return None

# ================= 4. OUTPUT & CHART =================

def save_mtf_chart(data):
    try:
        symbol = data['s'].replace('/', '')
        filename = f"{SCREENSHOT_DIR}/{symbol}_MTF.png"
        
        mc = mpf.make_marketcolors(up='#089981', down='#F23645', inherit=True)
        s  = mpf.make_mpf_style(marketcolors=mc, style='nightclouds', gridstyle=':')
        
        df = data['df'].tail(60).set_index(pd.DatetimeIndex(data['df'].tail(60)['time']))
        
        hlines = dict(hlines=[data['p'], data['sl'], data['tp']], colors=['blue','red','green'], linestyle='-.')
        
        title = f"\n{data['s']} [1H Execution]\nBias 1D: {data['bias']} | POI 4H: Active"
        
        mpf.plot(df, type='candle', style=s, title=title, hlines=hlines,
                 savefig=dict(fname=filename, dpi=100, bbox_inches='tight'))
        return filename
    except: return None

def send_telegram(photo_path, data):
    try:
        caption = (
            f"💎 **SMC MTF SETUP** 💎\n\n"
            f"Symbol: *{data['s']}*\n"
            f"Direction: *{data['side']}*\n"
            f"Bias (1D): *{data['bias']}*\n"
            f"POI (4H): *{data['poi']}*\n\n"
            f"🔵 Entry (1H): `{data['p']}`\n"
            f"🔴 Stop Loss: `{data['sl']:.4f}`\n"
            f"🟢 Take Profit: `{data['tp']:.4f}`\n\n"
            f"⚖️ Size: `{data['qty']:.4f}` (Risk {RISK_PER_TRADE*100}%)"
        )
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        with open(photo_path, 'rb') as f:
            requests.post(url, files={'photo': f}, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'})
    except: pass

def get_top_coins():
    try:
        exchange = get_exchange_instance()
        tickers = exchange.fetch_tickers()
        data = []
        for s, t in tickers.items():
            if '/USDT' in s and 'UP/' not in s and 'DOWN/' not in s:
                if t.get('quoteVolume', 0) > 10000000: data.append(s)
        return data[:COIN_LIMIT]
    except: return []

# ================= 5. MAIN LOOP =================
if __name__ == '__main__':
    print("🚀 **MTF SMC SCANNER (1D -> 4H -> 1H)** Started...")
    
    if not BINANCE_API_KEY: sys.exit("Error: No API Key")
    
    # Ambil Balance Sekali Saja di Awal (Simulasi)
    balance = DEFAULT_BALANCE 
    try:
        exch = get_exchange_instance()
        balance = exch.fetch_balance()['USDT']['free']
    except: pass
    print(f"💰 Balance Reference: ${balance:.2f}")

    cached_coins = []
    last_update = 0

    while True:
        try:
            loop_start = time.time()
            
            # Update Coin List per jam
            if not cached_coins or (loop_start - last_update) > 3600:
                print("Updating Coins...")
                cached_coins = get_top_coins()
                last_update = loop_start
            
            print(f"\nScanning {len(cached_coins)} coins (MTF Analysis)...", end='')
            
            results = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {executor.submit(analyze_mtf, sym, balance): sym for sym in cached_coins}
                for f in concurrent.futures.as_completed(futures):
                    res = f.result()
                    if res: results.append(res)
            
            print(f" Found {len(results)} valid setups.")
            
            for res in results:
                print(f"Processing Signal: {res['s']}")
                img = save_mtf_chart(res)
                if img: send_telegram(img, res)
            
            # Smart Sleep
            elapsed = time.time() - loop_start
            sleep_time = max(0, SCAN_INTERVAL - elapsed)
            print(f"Sleep {sleep_time:.2f}s...")
            time.sleep(sleep_time)
            
        except KeyboardInterrupt: break
        except Exception as e: 
            print(f"Error: {e}")
            time.sleep(20)


