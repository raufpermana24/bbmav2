import sys
import os
import time
import concurrent.futures
import warnings
import numpy as np
from datetime import datetime

warnings.filterwarnings("ignore", category=UserWarning)

# ==========================================
# 1. CEK LIBRARY
# ==========================================
try:
    import ccxt
    import pandas as pd
    import mplfinance as mpf
    import requests
except ImportError as e:
    sys.exit(
        f"Library Error: {e}.\n"
        f"Install dulu: pip install ccxt pandas mplfinance requests numpy"
    )

# ==========================================
# 2. KONFIGURASI
# ==========================================
API_KEY    = os.environ.get('BINANCE_API_KEY',    'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
API_SECRET = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN',   '8361349338:AAHOlx4fKz_bp1MHnVg8CxS9MY_pcejxLes')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003559364460')

# ── Timeframe yang discan setiap siklus ─────────────────────
TIMEFRAMES = ['1h', '4h', '1d', '1w']

# Limit candle per TF (sesuaikan agar cukup untuk indikator)
TF_LIMIT = {
    '1h':  300,   # ~12 hari
    '4h':  200,   # ~33 hari
    '1d':  200,   # ~200 hari
    '1w':  100,   # ~2 tahun
}

# Interval jeda scan (detik) per TF — hindari re-scan terlalu sering
TF_SCAN_INTERVAL = {
    '1h':  300,    # scan ulang 1h tiap 5 menit
    '4h':  900,    # scan ulang 4h tiap 15 menit
    '1d':  3600,   # scan ulang 1d tiap 1 jam
    '1w':  14400,  # scan ulang 1w tiap 4 jam
}

# Bobot prioritas TF untuk sorting hasil (TF lebih tinggi = lebih penting)
TF_WEIGHT = {'1h': 1, '4h': 2, '1d': 3, '1w': 4}

TOP_COIN_COUNT = 100
MAX_THREADS    = 15

OUTPUT_FOLDER = 'bbma_results'
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# State anti-spam: key = "SYMBOL_SIGNAL_TF", value = timestamp candle
processed_signals: dict = {}
# State kapan terakhir TF di-scan: key = TF, value = time.time()
last_scan_time: dict = {tf: 0.0 for tf in TIMEFRAMES}

# ==========================================
# 3. KONEKSI EXCHANGE
# ==========================================
exchange = ccxt.binance({
    'apiKey': API_KEY, 'secret': API_SECRET,
    'options': {'defaultType': 'spot'},
    'enableRateLimit': True,
})

# ==========================================
# 4. DATA ENGINE
# ==========================================
def get_viral_symbols(limit: int = 100) -> list:
    """Top koin berdasarkan 24h % kenaikan tertinggi, volume > $2 juta."""
    try:
        exchange.load_markets()
        tickers = exchange.fetch_tickers()
        valid = [
            t for s, t in tickers.items()
            if ('/USDT' in s
                and 'UP/' not in s and 'DOWN/' not in s
                and 'BEAR/' not in s and 'BULL/' not in s
                and t.get('quoteVolume', 0) and t['quoteVolume'] > 2_000_000)
        ]
        valid.sort(key=lambda x: x['percentage'] if x['percentage'] else -999, reverse=True)
        return [{'symbol': t['symbol'], 'change': t['percentage']} for t in valid[:limit]]
    except Exception as e:
        print(f"  [Tickers Error] {e}")
        return []

def fetch_ohlcv(symbol: str, timeframe: str) -> pd.DataFrame | None:
    """Ambil OHLCV untuk satu simbol dan satu timeframe."""
    try:
        limit = TF_LIMIT.get(timeframe, 200)
        bars  = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df    = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except:
        return None

# ==========================================
# 5. INDIKATOR  (terjemahan Pine Script 1:1)
# ==========================================
def _wma(series: pd.Series, length: int) -> pd.Series:
    """Weighted Moving Average — identik wma() Pine Script."""
    weights = np.arange(1, length + 1, dtype=float)
    return series.rolling(length).apply(
        lambda x: np.dot(x, weights) / weights.sum(), raw=True
    )

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    # Bollinger Bands
    df['midBB'] = df['close'].rolling(20).mean()
    df['BBdev'] = 2.0 * df['close'].rolling(20).std(ddof=0)
    df['topBB'] = df['midBB'].shift(1) + df['BBdev']   # pakai midBB[1] seperti Pine
    df['lowBB'] = df['midBB'].shift(1) - df['BBdev']

    # MA High & Low (WMA)
    df['mahi5']  = _wma(df['high'], 5)
    df['mahi10'] = _wma(df['high'], 10)
    df['malo5']  = _wma(df['low'],  5)
    df['malo10'] = _wma(df['low'],  10)

    # Nilai bar sebelumnya (untuk sinyal EXTREME)
    df['mahi5_p'] = df['mahi5'].shift(1)
    df['malo5_p'] = df['malo5'].shift(1)
    df['topBB_p'] = df['topBB'].shift(1)
    df['lowBB_p'] = df['lowBB'].shift(1)

    # EMA 50
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()

    return df

# ==========================================
# 6. SEMUA SINYAL BBMA (Pine Script → Python)
# ==========================================
def compute_signals(df: pd.DataFrame) -> dict:
    """
    Hitung semua sinyal BBMA untuk bar closed terbaru.
      iloc[-1] = candle running (diabaikan)
      iloc[-2] = candle closed terbaru  (= bar saat ini di Pine)
      iloc[-3] = candle sebelumnya      (= bar[1] di Pine)
    Return: dict { nama_sinyal: {tipe, explanation, price, time} }
    """
    if df is None or len(df) < 30:
        return {}

    c    = df.iloc[-2]
    prev = df.iloc[-3]

    csz_c    = abs(c['close']    - c['open'])
    csz_prev = abs(prev['close'] - prev['open'])

    # ── REENTRY ──────────────────────────────────────────────
    reject_mahi = (c['high']  > c['mahi5']  and c['close'] < c['mahi5']
                   and c['close'] < c['mahi10'] and c['close'] < c['midBB']
                   and c['mahi5'] < c['midBB'])
    reject_malo = (c['low']   < c['malo5']  and c['close'] > c['malo5']
                   and c['close'] > c['malo10'] and c['close'] > c['midBB']
                   and c['malo5'] > c['midBB'])

    # ── CSAK ─────────────────────────────────────────────────
    csak_sell = (c['open'] > c['midBB']) and (c['close'] < c['midBB'])
    csak_buy  = (c['open'] < c['midBB']) and (c['close'] > c['midBB'])

    # ── EXTREME (engulfing type) ──────────────────────────────
    ext_sell = (prev['close'] > prev['open'] and c['close'] < c['topBB']
                and c['close'] < c['open']
                and (c['mahi5_p'] > c['topBB_p'] or c['mahi5'] > c['topBB'])
                and csz_c > csz_prev / 2)
    ext_buy  = (prev['close'] < prev['open'] and c['close'] > c['lowBB']
                and c['close'] > c['open']
                and (c['malo5_p'] < c['lowBB_p'] or c['malo5'] < c['lowBB'])
                and csz_c > csz_prev / 2)

    # ── MMT / CSM (Momentum) ─────────────────────────────────
    mmt_sell = (c['close'] < c['lowBB']) and (c['open'] > c['lowBB'])
    mmt_buy  = (c['close'] > c['topBB']) and (c['open'] < c['topBB'])

    # ── ALIEN ────────────────────────────────────────────────
    ali_sell = (c['open'] < c['lowBB']) and (c['close'] < c['lowBB'])
    ali_buy  = (c['open'] > c['topBB']) and (c['close'] > c['topBB'])

    # ── MAHI / MALO area ─────────────────────────────────────
    signal_mahi = c['close'] > c['mahi5']
    signal_malo = c['close'] < c['malo5']

    # ── CSAA ─────────────────────────────────────────────────
    csaa = (c['close'] < c['malo10']) or (c['close'] > c['mahi10'])

    signals = {}
    if reject_mahi: signals['REENTRY SELL'] = {'tipe': 'SELL',
        'explanation': 'Harga ditolak dari MAHI5 — potensi turun.'}
    if reject_malo: signals['REENTRY BUY']  = {'tipe': 'BUY',
        'explanation': 'Harga ditolak dari MALO5 — potensi naik.'}
    if csak_sell:   signals['CSAK SELL']    = {'tipe': 'SELL',
        'explanation': 'Candle close tembus ke bawah MidBB.'}
    if csak_buy:    signals['CSAK BUY']     = {'tipe': 'BUY',
        'explanation': 'Candle close tembus ke atas MidBB.'}
    if ext_sell:    signals['EXTREME SELL'] = {'tipe': 'SELL',
        'explanation': 'Engulfing bearish — MAHI5 di atas TopBB, reversal turun.'}
    if ext_buy:     signals['EXTREME BUY']  = {'tipe': 'BUY',
        'explanation': 'Engulfing bullish — MALO5 di bawah LowBB, reversal naik.'}
    if mmt_sell:    signals['MMT SELL']     = {'tipe': 'SELL',
        'explanation': 'Momentum — close tembus ke bawah LowBB (CSM Sell).'}
    if mmt_buy:     signals['MMT BUY']      = {'tipe': 'BUY',
        'explanation': 'Momentum — close tembus ke atas TopBB (CSM Buy).'}
    if ali_sell:    signals['ALIEN SELL']   = {'tipe': 'SELL',
        'explanation': 'Candle penuh di bawah LowBB (Alien Candle Sell).'}
    if ali_buy:     signals['ALIEN BUY']    = {'tipe': 'BUY',
        'explanation': 'Candle penuh di atas TopBB (Alien Candle Buy).'}
    if signal_mahi: signals['MAHI5']        = {'tipe': 'INFO',
        'explanation': 'Close di atas MAHI5 — area MA High 5.'}
    if signal_malo: signals['MALO5']        = {'tipe': 'INFO',
        'explanation': 'Close di bawah MALO5 — area MA Low 5.'}
    if csaa:        signals['CSAA']         = {'tipe': 'INFO',
        'explanation': 'Close di luar area MA10 High/Low (CSAA/CHK).'}

    for k in signals:
        signals[k]['price'] = c['close']
        signals[k]['time']  = c['timestamp']

    return signals

# ==========================================
# 7. MULTI-TIMEFRAME KONFIRMASI
# ==========================================
# Bobot konfirmasi: semakin banyak TF yang setuju → skor lebih tinggi
def get_mtf_bias(symbol: str) -> dict:
    """
    Periksa arah (BUY/SELL/NEUTRAL) pada 1h, 4h, 1d, 1w.
    Return dict berisi:
      {
        '1h':  'BUY' | 'SELL' | 'NEUTRAL',
        '4h':  ...,
        '1d':  ...,
        '1w':  ...,
        'score_buy':  int,   # jumlah TF konfirmasi BUY  (0-4)
        'score_sell': int,   # jumlah TF konfirmasi SELL (0-4)
        'aligned':    bool,  # True jika ≥3 TF searah
        'direction':  'BUY' | 'SELL' | 'MIXED',
      }
    """
    bias = {}
    score_buy = score_sell = 0

    for tf in ['1h', '4h', '1d', '1w']:
        df = fetch_ohlcv(symbol, tf)
        if df is None or len(df) < 30:
            bias[tf] = 'NEUTRAL'
            continue
        df = add_indicators(df)
        c  = df.iloc[-2]

        # Logika arah sederhana berbasis posisi close terhadap EMA50 dan MidBB
        above_ema = c['close'] > c['ema50']
        above_mid = c['close'] > c['midBB']
        malo_above_mid = c['malo5'] > c['midBB']
        mahi_below_mid = c['mahi5'] < c['midBB']

        if above_ema and above_mid and malo_above_mid:
            bias[tf] = 'BUY';  score_buy  += 1
        elif not above_ema and not above_mid and mahi_below_mid:
            bias[tf] = 'SELL'; score_sell += 1
        else:
            bias[tf] = 'NEUTRAL'

    direction = (
        'BUY'  if score_buy  >= 3 else
        'SELL' if score_sell >= 3 else
        'MIXED'
    )
    bias.update({
        'score_buy':  score_buy,
        'score_sell': score_sell,
        'aligned':    score_buy >= 3 or score_sell >= 3,
        'direction':  direction,
    })
    return bias

# ==========================================
# 8. CHART GENERATOR (multi-TF aware)
# ==========================================
def generate_chart(df: pd.DataFrame, symbol: str, signal_name: str,
                   timeframe: str = '1h') -> str | None:
    try:
        safe_sig = signal_name.replace(' ', '_')
        filename = f"{OUTPUT_FOLDER}/{symbol.replace('/', '-')}_{timeframe}_{safe_sig}.png"

        tail = 80 if timeframe in ('1d', '1w') else 100
        plot_df = df.tail(tail).copy()
        plot_df.set_index('timestamp', inplace=True)

        style = mpf.make_mpf_style(base_mpf_style='nightclouds', rc={'font.size': 8})
        adds  = [
            mpf.make_addplot(plot_df['topBB'],  color='white',   width=1.5),
            mpf.make_addplot(plot_df['midBB'],  color='yellow',  width=1.5, linestyle='--'),
            mpf.make_addplot(plot_df['lowBB'],  color='white',   width=1.5),
            mpf.make_addplot(plot_df['mahi5'],  color='fuchsia', width=0.8),
            mpf.make_addplot(plot_df['mahi10'], color='red',     width=1.2),
            mpf.make_addplot(plot_df['malo5'],  color='aqua',    width=0.8),
            mpf.make_addplot(plot_df['malo10'], color='blue',    width=1.2),
            mpf.make_addplot(plot_df['ema50'],  color='lime',    width=2.0),
        ]
        mpf.plot(plot_df, type='candle', style=style, addplot=adds,
                 title=f"{symbol} [{timeframe.upper()}] — {signal_name}",
                 savefig=dict(fname=filename, bbox_inches='tight'), volume=True)
        return filename
    except Exception as e:
        print(f"  [Chart Error] {e}")
        return None

# ==========================================
# 10. TELEGRAM ALERT (dengan MTF info)
# ==========================================
SIGNAL_ICON = {'BUY': '🟢', 'SELL': '🔴', 'INFO': '🔵'}
TF_EMOJI    = {'1h': '🕐', '4h': '🕓', '1d': '📅', '1w': '📆'}
DIR_EMOJI   = {'BUY': '🟢', 'SELL': '🔴', 'NEUTRAL': '⚪', 'MIXED': '🟡'}

def format_mtf_block(mtf: dict) -> str:
    """Buat baris ringkasan MTF untuk pesan Telegram."""
    lines = []
    for tf in ['1h', '4h', '1d', '1w']:
        d   = mtf.get(tf, 'NEUTRAL')
        em  = DIR_EMOJI.get(d, '⚪')
        lines.append(f"  {TF_EMOJI.get(tf,'')} {tf.upper():>3} : {em} {d}")
    aligned_txt = (
        f"✅ ALIGNED {mtf['direction']} ({mtf['score_buy' if mtf['direction']=='BUY' else 'score_sell']}/4 TF)"
        if mtf['aligned'] else
        f"⚠️ MIXED ({mtf['score_buy']} BUY / {mtf['score_sell']} SELL)"
    )
    return "\n".join(lines) + f"\n  {aligned_txt}"

def send_telegram_alert(symbol: str, signal_name: str, timeframe: str,
                        data: dict, change_24h: float,
                        mtf: dict, image_path=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    icon    = SIGNAL_ICON.get(data['tipe'], '⚪')
    tf_em   = TF_EMOJI.get(timeframe, '')
    mtf_blk = format_mtf_block(mtf)

    caption = (
        f"{icon} <b>BBMA SIGNAL — {signal_name}</b>\n"
        f"──────────────────────\n"
        f"💎 <b>Symbol :</b> {symbol}\n"
        f"🏷 <b>Tipe   :</b> {data['tipe']}\n"
        f"{tf_em} <b>TF     :</b> {timeframe.upper()}\n"
        f"💰 <b>Harga  :</b> {data['price']}\n"
        f"📈 <b>24h    :</b> +{change_24h:.2f}%\n"
        f"──────────────────────\n"
        f"📐 <b>Multi-TF Bias:</b>\n{mtf_blk}\n"
        f"──────────────────────\n"
        f"📝 <b>Analisa:</b> {data['explanation']}\n"
        f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    try:
        if image_path and os.path.exists(image_path):
            with open(image_path, "rb") as img:
                requests.post(f"{base}/sendPhoto",
                              data={'chat_id': TELEGRAM_CHAT_ID,
                                    'caption': caption, 'parse_mode': 'HTML'},
                              files={'photo': img}, timeout=20)
        else:
            requests.post(f"{base}/sendMessage",
                          data={'chat_id': TELEGRAM_CHAT_ID,
                                'text': caption, 'parse_mode': 'HTML'},
                          timeout=20)
    except Exception as e:
        print(f"  [TG Error] {e}")

# ==========================================
# 11. WORKER SCAN (per koin, semua TF)
# ==========================================
def worker_scan(coin_data: dict, active_tfs: list) -> list:
    """
    Scan satu koin pada semua TF yang aktif di siklus ini.
    Kembalikan list alert yang siap dikirim.
    """
    results   = []
    symbol    = coin_data['symbol']
    change_24h = coin_data['change']

    # Ambil MTF bias sekali per koin (1h/4h/1d/1w)
    mtf_bias: dict | None = None

    try:
        for tf in active_tfs:
            df = fetch_ohlcv(symbol, tf)
            if df is None:
                continue

            df = add_indicators(df)
            signals = compute_signals(df)
            if not signals:
                continue

            # Ambil MTF bias satu kali
            if mtf_bias is None:
                mtf_bias = get_mtf_bias(symbol)

            for sig_name, sig_data in signals.items():
                results.append({
                    'symbol':      symbol,
                    'signal':      sig_name,
                    'timeframe':   tf,
                    'data':        sig_data,
                    '24h_change':  change_24h,
                    'df':          df,
                    'mtf':         mtf_bias,
                    # Prioritas: TF lebih tinggi + sinyal BUY/SELL lebih berharga
                    'priority':    TF_WEIGHT.get(tf, 1) * (2 if sig_data['tipe'] != 'INFO' else 1),
                })

    except Exception:
        pass

    return results

# ==========================================
# 12. MAIN LOOP
# ==========================================
def main():
    print("=" * 60)
    print("  🚀  BBMA OMA ALLY — MULTI-TIMEFRAME BOT")
    print("=" * 60)
    print(f"  TF Scan  : {' · '.join(tf.upper() for tf in TIMEFRAMES)}")
    print(f"  Target   : Top {TOP_COIN_COUNT} koin (24h Gainer)")
    print(f"  Sinyal   : REENTRY · CSAK · EXTREME · MMT · ALIEN")
    print(f"             MAHI5 · MALO5 · CSAA")
    print(f"  MTF Bias : 1H · 4H · 1D · 1W (konfirmasi arah)")
    print("=" * 60)

    global processed_signals, last_scan_time

    while True:
        try:
            now = time.time()

            # Tentukan TF mana yang perlu di-scan siklus ini
            active_tfs = [
                tf for tf in TIMEFRAMES
                if now - last_scan_time[tf] >= TF_SCAN_INTERVAL[tf]
            ]

            if not active_tfs:
                time.sleep(5)
                continue

            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] "
                  f"TF aktif: {' | '.join(tf.upper() for tf in active_tfs)}")

            viral_coins = get_viral_symbols(TOP_COIN_COUNT)
            if not viral_coins:
                print("  Gagal ambil data market. Retry 10s...")
                time.sleep(10)
                continue

            print(f"  Memindai {len(viral_coins)} koin "
                  f"(Top: {viral_coins[0]['symbol']} "
                  f"+{viral_coins[0]['change']:.1f}%)")

            all_alerts: list = []
            completed  = 0
            start_t    = time.time()

            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_THREADS) as ex:
                futures = {
                    ex.submit(worker_scan, coin, active_tfs): coin['symbol']
                    for coin in viral_coins
                }
                for future in concurrent.futures.as_completed(futures):
                    res = future.result()
                    if res:
                        all_alerts.extend(res)
                    completed += 1
                    if completed % 25 == 0:
                        sys.stdout.write(f"\r  Progress: {completed}/{len(viral_coins)}...")
                        sys.stdout.flush()

            # Update waktu scan terakhir
            for tf in active_tfs:
                last_scan_time[tf] = now

            dur = time.time() - start_t
            print(f"\n  ✅ Selesai ({dur:.1f}s) — "
                  f"{len(all_alerts)} sinyal ditemukan.")

            # Urutkan: prioritas tertinggi dulu (TF tinggi + BUY/SELL > INFO)
            all_alerts.sort(key=lambda x: x['priority'], reverse=True)

            sent_count = 0
            for alert in all_alerts:
                sym      = alert['symbol']
                sig_name = alert['signal']
                tf       = alert['timeframe']
                sig_data = alert['data']
                mtf      = alert['mtf'] or {}

                sig_key = f"{sym}_{sig_name}_{tf}"

                # Anti-spam
                if processed_signals.get(sig_key) == sig_data['time']:
                    continue
                processed_signals[sig_key] = sig_data['time']

                tipe = sig_data['tipe']
                mtf_dir   = mtf.get('direction', '?')
                aligned   = mtf.get('aligned', False)
                align_txt = f"✅ MTF {mtf_dir}" if aligned else "⚠️ MTF MIXED"

                print(
                    f"  🔔 {sym} | {tf.upper():>3} | {sig_name:<14} "
                    f"[{tipe}] @ {sig_data['price']:.6g} | {align_txt}"
                )

                img = generate_chart(alert['df'], sym, sig_name, tf)
                send_telegram_alert(
                    symbol=sym,
                    signal_name=sig_name,
                    timeframe=tf,
                    data=sig_data,
                    change_24h=alert['24h_change'],
                    mtf=mtf,
                    image_path=img,
                )
                sent_count += 1

            if sent_count == 0:
                print("  (Tidak ada sinyal baru)")

            # Jeda minimum sebelum cek siklus berikutnya
            print("  ⏳ Jeda 60 detik...")
            time.sleep(60)

        except KeyboardInterrupt:
            print("\n⛔ Bot dihentikan.")
            break
        except Exception as e:
            print(f"  [Main Error] {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
