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

# Telegram Config
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8361349338:AAHOlx4fKz_bp1MHnVg8CxS9MY_pcejxLes') 
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003618941801')

 
# SETTING STRATEGI
TIMEFRAME = '4h'        # UBAH KE 4 JAM
VOL_MULTIPLIER = 2.0    # Volume harus 2x dari rata-rata
LIMIT = 100             # Ambil 100 candle 
TOP_COIN_COUNT = 300    # Scan 300 Koin
MAX_THREADS = 10        # Kecepatan scan

# FITUR FILTER
CARI_BB_DATAR = True    # True = Hanya cari yang BB Datar

OUTPUT_FOLDER = 'volume_hunter_results'
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
    bb_icon = "🤐" if "SQUEEZE" in data['bb_status'] else "💥"
    spike_pct = (data['spike_ratio'] * 100) - 100
    
    # Menyiapkan teks Divergence jika ada
    div_text = f"🌊 <b>Divergence:</b> {data['divergence']}\n" if data['divergence'] != "None" else ""

    caption = (
        f"🐋 <b>VOLUME + DIVERGENCE ALERT 4H</b>\n"
        f"💎 <b>{symbol}</b>\n"
        f"──────────────────────\n"
        f"📊 <b>Volume Spike:</b> {data['spike_ratio']:.2f}x (Avg 24H)\n"
        f"🔥 <b>Kenaikan Vol:</b> +{spike_pct:.0f}%\n"
        f"📏 <b>Kondisi BB:</b> {data['bb_status']} {bb_icon}\n"
        f"──────────────────────\n"
        f"{div_text}"
        f"🛠 <b>Setup BBMA:</b> {data['signal']} {icon}\n"
        f"🏷 <b>Tipe:</b> {data['tipe']}\n"
        f"💰 <b>Harga:</b> {data['price']}\n"
        f"📝 <b>Analisa:</b>\n{data['explanation']}\n"
        f"──────────────────────\n"
        f"<i>Kombinasi Volume, BB Squeeze & Divergence</i>"
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
        
        # Panel 0: Candle, BB, MA
        # Panel 1: RSI (Baru)
        adds = [
            mpf.make_addplot(plot_df['BB_Up'], color='green', width=0.8),
            mpf.make_addplot(plot_df['BB_Mid'], color='orange', width=0.8),
            mpf.make_addplot(plot_df['BB_Low'], color='green', width=0.8),
            mpf.make_addplot(plot_df['MA5_Hi'], color='cyan', width=0.6),
            mpf.make_addplot(plot_df['MA5_Lo'], color='magenta', width=0.6),
            # Plot RSI di Panel Bawah
            mpf.make_addplot(plot_df['RSI'], panel=1, color='yellow', width=1.5, ylabel='RSI'),
        ]
        
        title_text = f"{symbol} [4H] - Vol {signal_info['spike_ratio']:.1f}x - {signal_info['divergence']}"
        
        mpf.plot(plot_df, type='candle', style=style, addplot=adds, 
                 title=title_text, 
                 savefig=dict(fname=filename, bbox_inches='tight'), 
                 volume=False, # Volume dimatikan agar RSI lebih jelas
                 panel_ratios=(6, 2)) # Rasio panel Chart vs RSI
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
        bars = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=LIMIT)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except: return None

def add_indicators(df):
    # 1. BB Standard
    bb = df.ta.bbands(length=20, std=2)
    if bb is not None:
        df['BB_Up'] = bb.iloc[:, 2]; df['BB_Mid'] = bb.iloc[:, 1]; df['BB_Low'] = bb.iloc[:, 0]
    
    # 2. MA & EMA
    df['MA5_Hi'] = df['high'].rolling(5).mean()
    df['MA5_Lo'] = df['low'].rolling(5).mean()
    df['EMA_50'] = df.ta.ema(length=50)

    # 3. BB WIDTH
    df['BB_Width'] = (df['BB_Up'] - df['BB_Low']) / df['BB_Mid']
    df['BB_Width_Avg'] = df['BB_Width'].rolling(20).mean()

    # 4. RSI & MACD (BARU: Untuk Divergence)
    df['RSI'] = df.ta.rsi(length=14)
    macd = df.ta.macd(fast=12, slow=26, signal=9)
    # Gabungkan kolom MACD ke dataframe utama
    df = pd.concat([df, macd], axis=1)
    
    return df

# ==========================================
# 6. LOGIKA VOLUME, BBMA & DIVERGENCE
# ==========================================

def check_divergence(df):
    """
    Mendeteksi Regular Divergence pada RSI dan MACD Histogram
    """
    if len(df) < 25: return "None"

    # Ambil data saat ini (candle terakhir closed)
    curr = df.iloc[-2]
    
    # Ambil data pembanding (Low/High terendah/tertinggi dalam 15 candle ke belakang)
    window = df.iloc[-15:-3] 
    
    divs = []

    # --- 1. BULLISH DIVERGENCE (Harga Lower Low, Indikator Higher Low) ---
    price_lower_low = curr['close'] < window['close'].min()
    
    if price_lower_low:
        rsi_higher_low = curr['RSI'] > window['RSI'].min()
        hist_name = 'MACDh_12_26_9' # Nama kolom histogram default pandas_ta
        macd_higher_low = curr[hist_name] > window[hist_name].min() if hist_name in df.columns else False

        if rsi_higher_low and curr['RSI'] < 50: # Validasi RSI area bawah
            divs.append("RSI Bullish 🐂")
        if macd_higher_low and curr[hist_name] < 0: # Validasi MACD area negatif
            divs.append("MACD Bullish 🐂")

    # --- 2. BEARISH DIVERGENCE (Harga Higher High, Indikator Lower High) ---
    price_higher_high = curr['close'] > window['close'].max()

    if price_higher_high:
        rsi_lower_high = curr['RSI'] < window['RSI'].max()
        hist_name = 'MACDh_12_26_9'
        macd_lower_high = curr[hist_name] < window[hist_name].max() if hist_name in df.columns else False

        if rsi_lower_high and curr['RSI'] > 50: # Validasi RSI area atas
            divs.append("RSI Bearish 🐻")
        if macd_lower_high and curr[hist_name] > 0: # Validasi MACD area positif
            divs.append("MACD Bearish 🐻")

    return ", ".join(divs) if divs else "None"

def analyze_volume_anomaly(df):
    if df is None or len(df) < 20: return 0
    current_vol = df['volume'].iloc[-2]
    # Di TF 4H, 6 candle sebelumnya = 24 Jam terakhir
    avg_vol_recent = df['volume'].iloc[-8:-2].mean()
    if avg_vol_recent == 0: return 0
    return current_vol / avg_vol_recent

def analyze_bbma_setup(df):
    if df is None or len(df) < 55: return None
    c = df.iloc[-2]
    
    is_squeeze = c['BB_Width'] < c['BB_Width_Avg']
    if CARI_BB_DATAR and not is_squeeze: return None

    bb_status = "SQUEEZE (Datar)" if is_squeeze else "EXPANDING (Mengembang)"
    ema_val = c.get('EMA_50', 0)
    trend = "BULLISH" if c['close'] > ema_val else "BEARISH"
    
    # CEK DIVERGENCE
    divergence_status = check_divergence(df)
    
    signal_data = None
    tipe = "NONE"

    # --- SETUP BUY ---
    if trend == "BULLISH":
        tipe = "BUY"
        div_note = f" + {divergence_status}" if "Bullish" in divergence_status else ""
        
        if c['MA5_Lo'] < c['BB_Low']:
            signal_data = {"signal": "EXTREME", "explanation": f"Extreme Buy (Harga Murah){div_note}."}
        elif c['close'] > c['BB_Mid'] and c['low'] <= c['MA5_Lo']:
            signal_data = {"signal": "RE-ENTRY", "explanation": f"Re-Entry Buy (Diskon){div_note}."}
        elif c['close'] > c['BB_Up']:
            signal_data = {"signal": "MOMENTUM", "explanation": f"Breakout Kuat{div_note}."}
        elif is_squeeze and c['close'] > c['BB_Mid']:
             signal_data = {"signal": "SIDEWAYS ACCUMULATION", "explanation": f"Akumulasi BB Datar{div_note}."}

    # --- SETUP SELL ---
    elif trend == "BEARISH":
        tipe = "SELL"
        div_note = f" + {divergence_status}" if "Bearish" in divergence_status else ""

        if c['MA5_Hi'] > c['BB_Up']:
            signal_data = {"signal": "EXTREME", "explanation": f"Extreme Sell (Harga Mahal){div_note}."}
        elif c['close'] < c['BB_Mid'] and c['high'] >= c['MA5_Hi']:
            signal_data = {"signal": "RE-ENTRY", "explanation": f"Re-Entry Sell (Pantulan){div_note}."}
        elif c['close'] < c['BB_Low']:
            signal_data = {"signal": "MOMENTUM", "explanation": f"Breakdown Kuat{div_note}."}
        elif is_squeeze and c['close'] < c['BB_Mid']:
             signal_data = {"signal": "SIDEWAYS DISTRIBUTION", "explanation": f"Distribusi BB Datar{div_note}."}

    if signal_data:
        signal_data['tipe'] = tipe
        signal_data['price'] = c['close']
        signal_data['time'] = c['timestamp']
        signal_data['bb_status'] = bb_status
        signal_data['divergence'] = divergence_status
        return signal_data
    
    return None

# ==========================================
# 7. WORKER PROSES
# ==========================================
def worker_scan(symbol):
    try:
        df = fetch_ohlcv(symbol)
        if df is None: return None

        spike_ratio = analyze_volume_anomaly(df)
        if spike_ratio < VOL_MULTIPLIER:
            return None

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
    print(f"=== VOLUME + DIVERGENCE HUNTER 4H (v3) ===")
    print(f"Strategi: Volume > {VOL_MULTIPLIER}x | RSI/MACD Divergence | BB Squeeze")
    print(f"Target: Top {TOP_COIN_COUNT} Koin.")
    
    global processed_signals

    while True:
        try:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scanning 4H (Vol + Div)...")
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
            print(f"\n✅ Selesai ({duration:.2f}s). Ditemukan: {len(alerts_queue)} Koin.")

            alerts_queue.sort(key=lambda x: x['spike_ratio'], reverse=True)

            for alert in alerts_queue:
                sym = alert['symbol']
                div_log = f"[{alert['divergence']}]" if alert['divergence'] != "None" else ""
                
                if processed_signals.get(sym) != alert['time']:
                    processed_signals[sym] = alert['time']
                    
                    print(f"🔥 {sym}: Vol {alert['spike_ratio']:.1f}x {div_log} -> {alert['signal']}")
                    
                    img = generate_chart(alert['df'], sym, alert)
                    if img: send_telegram_alert(sym, alert, img)
            
            print("⏳ Menunggu 5 menit...")
            time.sleep(300)

        except KeyboardInterrupt: break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()


