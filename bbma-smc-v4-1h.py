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
# KONFIGURASI SISTEM (OPTIMAL)
# ==========================================
# Set backend ke Agg agar jalan di VPS/Background tanpa error display
matplotlib.use('Agg')

# --- KREDENSIAL ---
# Isi di sini atau gunakan file .env
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97') 
BINANCE_SECRET_KEY = os.environ.get('BINANCE_SECRET_KEY', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8361349338:AAHOlx4fKz_bp1MHnVg8CxS9MY_pcejxLes')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003558146379')

# --- PARAMETER SMC DEEP HISTORY ---
# Limit 500 candle memberikan gambaran struktur Major yang jauh lebih akurat
# Daily: 500 hari ke belakang | H1: 20 hari ke belakang
CANDLE_LIMIT = 500      

COIN_LIMIT = 300        # Jumlah koin top volume
SCAN_INTERVAL = 30      # Detik
MAX_WORKERS = 15        # Thread safe

RISK_PER_TRADE = 0.01
DEFAULT_BALANCE = 1000

# Folder
SCREENSHOT_DIR = 'screenshots'
if not os.path.exists(SCREENSHOT_DIR): os.makedirs(SCREENSHOT_DIR)

# ==========================================
# 2. CORE CONNECTION
# ==========================================

def get_exchange_instance():
    return ccxt.binance({
        'apiKey': BINANCE_API_KEY,
        'secret': BINANCE_SECRET_KEY,
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })

def fetch_deep_history(exchange, symbol, timeframe):
    """
    Mengambil data riwayat panjang (500 candle) untuk akurasi SMC.
    """
    try:
        # Limit ditingkatkan ke 500-1000 sesuai konfigurasi
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=CANDLE_LIMIT)
        if not ohlcv or len(ohlcv) < 200: return None # Butuh minimal 200 data
        
        df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        df['time'] = pd.to_datetime(df['time'], unit='ms')
        return df
    except Exception:
        return None

def send_telegram_photo(photo_path, caption):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        with open(photo_path, 'rb') as f:
            requests.post(url, files={'photo': f}, data={
                'chat_id': TELEGRAM_CHAT_ID, 
                'caption': caption, 
                'parse_mode': 'Markdown'
            }, timeout=20)
    except Exception as e:
        print(f"⚠️ Gagal kirim Telegram: {e}")

# ==========================================
# 3. LOGIKA SMC LANJUTAN (DEEP SCAN)
# ==========================================

def identify_major_structure(df):
    """
    Mengidentifikasi Struktur Major menggunakan data historis panjang.
    Menggunakan Rolling Window lebih besar (10) untuk menyaring noise.
    """
    # Fractal Major (10 candle kiri kanan)
    df['swing_high'] = df['high'].rolling(window=10, center=True).max()
    df['swing_low'] = df['low'].rolling(window=10, center=True).min()
    
    # Ambil titik Swing High/Low Major terakhir
    highs = df[df['high'] == df['swing_high']]['high'].dropna().values
    lows = df[df['low'] == df['swing_low']]['low'].dropna().values
    
    if len(highs) == 0 or len(lows) == 0: return 'NEUTRAL'
    
    close = df.iloc[-1]['close']
    trend = 'NEUTRAL'
    
    # BOS Logic (Break of Structure)
    # Jika harga sekarang di atas Swing High Major terakhir -> Bullish
    if close > highs[-1]:
        trend = 'BULLISH'
    # Jika harga sekarang di bawah Swing Low Major terakhir -> Bearish
    elif close < lows[-1]:
        trend = 'BEARISH'
        
    return trend

def scan_deep_fvg(df, trend):
    """
    Memindai 500 candle untuk mencari FVG Valid yang belum terisi (Unmitigated).
    Hanya mengembalikan FVG yang paling dekat dengan harga sekarang.
    """
    valid_poi = None
    curr_price = df.iloc[-1]['close']
    
    # Kita cari dari candle lama (index 0) ke baru (index akhir)
    # Tapi untuk efisiensi kita cari mundur dari baru ke lama
    # Kita batasi scan 200 candle terakhir (karena FVG 500 candle lalu biasanya sudah tidak valid)
    lookback = 200 
    
    for i in range(len(df)-2, len(df)-lookback, -1):
        
        # BULLISH SETUP: Cari FVG Bawah (Demand)
        if trend == 'BULLISH':
            # Rumus FVG Bull: Low[i] > High[i-2]
            candle_now_low = df.iloc[i]['low']
            candle_prev_high = df.iloc[i-2]['high']
            
            if candle_now_low > candle_prev_high:
                fvg_top = candle_now_low
                fvg_bot = candle_prev_high
                fvg_mid = (fvg_top + fvg_bot) / 2
                
                # VALIDASI MITIGASI:
                # Cek apakah candle-candle SETELAH terbentuknya FVG ini sudah menembus ke bawah?
                # Ambil data dari i+1 sampai candle terakhir
                future_candles = df.iloc[i+1:]
                min_future_low = future_candles['low'].min()
                
                # Jika low masa depan pernah tembus di bawah FVG Bottom, berarti FVG sudah basi (Mitigated)
                if min_future_low < fvg_bot:
                    continue # Cari lagi yang lain
                
                # Validasi Jarak: Apakah harga sekarang sedang pullback mendekati FVG ini?
                # Harga harus di atas FVG tapi sedang turun mendekati area
                if curr_price > fvg_bot and curr_price <= fvg_top * 1.05:
                    valid_poi = (fvg_bot, fvg_top)
                    break # Ketemu FVG fresh terdekat!
        
        # BEARISH SETUP: Cari FVG Atas (Supply)
        elif trend == 'BEARISH':
            # Rumus FVG Bear: High[i] < Low[i-2]
            candle_now_high = df.iloc[i]['high']
            candle_prev_low = df.iloc[i-2]['low']
            
            if candle_now_high < candle_prev_low:
                fvg_bot = candle_now_high
                fvg_top = candle_prev_low
                
                # VALIDASI MITIGASI
                future_candles = df.iloc[i+1:]
                max_future_high = future_candles['high'].max()
                
                # Jika high masa depan pernah tembus di atas FVG Top, sudah basi
                if max_future_high > fvg_top:
                    continue
                
                # Validasi Jarak
                if curr_price < fvg_top and curr_price >= fvg_bot * 0.95:
                    valid_poi = (fvg_bot, fvg_top)
                    break
                    
    return valid_poi

# ==========================================
# 4. ANALISIS MTF DENGAN DEEP HISTORY
# ==========================================

def analyze_market_structure(symbol, balance):
    try:
        exchange = get_exchange_instance()
        
        # 1. DAILY (BIG PICTURE) - SCAN 500 CANDLE
        df_d1 = fetch_deep_history(exchange, symbol, '1d')
        if df_d1 is None: return None
        
        daily_bias = identify_major_structure(df_d1)
        if daily_bias == 'NEUTRAL': return None # Market konsolidasi/sideways

        # 2. H4 (INTERMEDIATE) - SCAN POI
        df_h4 = fetch_deep_history(exchange, symbol, '4h')
        if df_h4 is None: return None
        
        # Pastikan tren H4 selaras
        h4_bias = identify_major_structure(df_h4)
        if h4_bias != daily_bias: return None
        
        # Cari Deep FVG di H4
        poi = scan_deep_fvg(df_h4, daily_bias)
        if poi is None: return None
        
        poi_bot, poi_top = poi

        # 3. H1 (EXECUTION) - KONFIRMASI
        df_h1 = fetch_deep_history(exchange, symbol, '1h')
        if df_h1 is None: return None
        
        h1_close = df_h1.iloc[-1]['close']
        
        # Setup Variables
        setup = None
        
        # Swing High/Low lokal H1 (Recent 20 candle) untuk SL
        recent_h1 = df_h1.tail(20)
        h1_low = recent_h1['low'].min()
        h1_high = recent_h1['high'].max()
        
        if daily_bias == 'BULLISH':
            sl = h1_low * 0.995 # SL di bawah low lokal
            risk = abs(h1_close - sl)
            tp = h1_close + (risk * 2.5) # RR 1:2.5
            
            qty_usd = (balance * RISK_PER_TRADE) / (risk / h1_close) if risk > 0 else 0
            qty_coin = qty_usd / h1_close
            
            setup = {
                's': symbol, 'side': 'BUY 🟢', 'bias': daily_bias,
                'p': h1_close, 'sl': sl, 'tp': tp,
                'qty': qty_coin, 'risk_usd': balance * RISK_PER_TRADE,
                'poi_txt': f"H4 Unmitigated FVG\n({poi_bot:.3f} - {poi_top:.3f})",
                'df': df_h1
            }

        elif daily_bias == 'BEARISH':
            sl = h1_high * 1.005 # SL di atas high lokal
            risk = abs(sl - h1_close)
            tp = h1_close - (risk * 2.5)
            
            qty_usd = (balance * RISK_PER_TRADE) / (risk / h1_close) if risk > 0 else 0
            qty_coin = qty_usd / h1_close
            
            setup = {
                's': symbol, 'side': 'SELL 🔴', 'bias': daily_bias,
                'p': h1_close, 'sl': sl, 'tp': tp,
                'qty': qty_coin, 'risk_usd': balance * RISK_PER_TRADE,
                'poi_txt': f"H4 Unmitigated FVG\n({poi_bot:.3f} - {poi_top:.3f})",
                'df': df_h1
            }
            
        return setup

    except Exception:
        return None

# ==========================================
# 5. CHART & OUTPUT
# ==========================================

def save_accurate_chart(data):
    try:
        symbol = data['s'].replace('/', '')
        filename = f"{SCREENSHOT_DIR}/{symbol}_SMC.png"
        
        # FIX ERROR STYLE: Gunakan base_mpf_style
        mc = mpf.make_marketcolors(up='#089981', down='#F23645', inherit=True)
        s  = mpf.make_mpf_style(marketcolors=mc, base_mpf_style='nightclouds', gridstyle=':')
        
        # Gambar 60 candle terakhir saja agar chart jelas terlihat
        df = data['df'].tail(60).set_index(pd.DatetimeIndex(data['df'].tail(60)['time']))
        
        lines = dict(
            hlines=[data['p'], data['sl'], data['tp']],
            colors=['#2962FF', '#FF3B30', '#00E676'],
            linewidths=[1.5, 1.5, 1.5],
            linestyle='-.'
        )
        
        title = f"\n{data['s']} SMC DEEP ANALYSIS\nTrend: {data['bias']} | POI: {data['poi_txt'].splitlines()[0]}"
        
        mpf.plot(df, type='candle', style=s, title=title, hlines=lines, volume=False,
                 savefig=dict(fname=filename, dpi=100, bbox_inches='tight'))
        
        plt = matplotlib.pyplot
        plt.close('all') # Wajib clear memory
        return filename
    except Exception as e:
        print(f"Chart Error: {e}")
        return None

def get_top_coins():
    try:
        exchange = get_exchange_instance()
        tickers = exchange.fetch_tickers()
        data = []
        for s, t in tickers.items():
            if '/USDT' in s and 'UP/' not in s and 'DOWN/' not in s:
                if t.get('quoteVolume', 0) > 8000000: data.append(s)
        data.sort(key=lambda x: tickers[x]['quoteVolume'], reverse=True)
        return data[:COIN_LIMIT]
    except: return []

# ==========================================
# 6. MAIN LOOP
# ==========================================

if __name__ == '__main__':
    print(f"🧠 **SMC ULTIMATE BOT V2 (DEEP HISTORY)**")
    print(f"📚 Candle Lookback: {CANDLE_LIMIT} Bars")
    
    if not BINANCE_API_KEY:
        print("❌ API Key Kosong. Set di environment variables atau .env")
        sys.exit(1)

    bal = DEFAULT_BALANCE
    try:
        ex = get_exchange_instance()
        bal = float(ex.fetch_balance()['USDT']['free'])
        print(f"💰 Balance: ${bal:.2f} USDT")
    except: pass

    cached_coins = []
    last_update = 0

    while True:
        try:
            gc.collect() # Bersihkan RAM
            loop_start = time.time()
            
            if not cached_coins or (loop_start - last_update) > 3600:
                print("🔄 Refreshing Market Data...")
                cached_coins = get_top_coins()
                last_update = loop_start
            
            print(f"\n🔎 Deep Scanning {len(cached_coins)} coins...", end='')
            
            results = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {executor.submit(analyze_market_structure, s, bal): s for s in cached_coins}
                for f in concurrent.futures.as_completed(futures):
                    res = f.result()
                    if res: results.append(res)
            
            print(f" Done. {len(results)} Setups.")
            
            for s in results:
                print(f"🚀 Signal: {s['s']}")
                img = save_accurate_chart(s)
                if img:
                    cap = (
                        f"🧠 **SMC DEEP ANALYSIS**\n"
                        f"*{s['s']}* ({s['side']})\n"
                        f"Trend: {s['bias']} (Confirmed)\n"
                        f"POI: {s['poi_txt']}\n\n"
                        f"Entry: `{s['p']}`\n"
                        f"SL: `{s['sl']:.4f}`\n"
                        f"TP: `{s['tp']:.4f}`\n\n"
                        f"Risk: ${s['risk_usd']:.2f}"
                    )
                    send_telegram_photo(img, cap)
            
            sleep_time = max(0, SCAN_INTERVAL - (time.time() - loop_start))
            print(f"💤 Sleep {sleep_time:.1f}s...")
            time.sleep(sleep_time)

        except KeyboardInterrupt: break
        except Exception as e: 
            print(f"Error: {e}")
            time.sleep(10)


