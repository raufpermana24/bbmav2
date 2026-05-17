"""
╔══════════════════════════════════════════════════════════════════╗
║          BBMA OMA ALLY — BINANCE FUTURES (ALL-IN-ONE)           ║
║                                                                  ║
║  FITUR:                                                          ║
║  • Market   : Binance USD-M Futures (USDT Perpetual)            ║
║  • Scan     : Semua market futures USDT aktif                   ║
║  • Simpan   : OHLCV per simbol per TF → CSV di disk             ║
║  • Sinyal   : RE ENTRY · MMT · EXTREME  (BUY & SELL)           ║
║  • Timing   : Per-close candle  1H · 4H · 1D · 1W              ║
║  • Telegram : Kirim sinyal + chart otomatis                     ║
╚══════════════════════════════════════════════════════════════════╝
"""

import sys
import os
import time
import json
import concurrent.futures
import warnings
import numpy as np
from datetime import datetime
from pathlib import Path

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
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003964469739')


# ── Timeframe ──────────────────────────────────────────────────
TIMEFRAMES = ['1h', '4h', '1d', '1w']

TF_LIMIT = {
    '1h':  300,
    '4h':  200,
    '1d':  200,
    '1w':  100,
}

# Durasi satu candle dalam detik — dipakai untuk mendeteksi waktu close
TF_DURATION_SEC = {
    '1h':  3600,
    '4h':  14400,
    '1d':  86400,
    '1w':  604800,
}

TF_WEIGHT   = {'1h': 1, '4h': 2, '1d': 3, '1w': 4}
MAX_THREADS = 3            # ⚠️  Jangan naikkan — Binance mudah ban jika terlalu banyak
MIN_VOLUME  = 1_000_000   # minimal quoteVolume $1 juta

# ── Rate Limit & Ban handling ──────────────────────────────────
REQUEST_DELAY   = 0.25    # detik jeda antar request (maks ~4 req/s)
BAN_RETRY_DELAY = 120     # detik tunggu jika kena 418/429
MAX_BAN_RETRIES = 5       # maksimal percobaan ulang saat ban

# ── Direktori output ───────────────────────────────────────────
DATA_DIR   = Path('bbma_data')          # simpan CSV OHLCV
CHART_DIR  = Path('bbma_charts')        # simpan gambar chart
STATE_FILE = DATA_DIR / '_state.json'   # simpan state anti-spam & last close

DATA_DIR.mkdir(exist_ok=True)
CHART_DIR.mkdir(exist_ok=True)

# ── Sinyal yang dikirim ke Telegram ───────────────────────────
ALLOWED_SIGNALS = {
    'REENTRY BUY', 'REENTRY SELL',
    'MMT BUY',     'MMT SELL',
    'EXTREME BUY', 'EXTREME SELL',
}

SIGNAL_ICON  = {'BUY': '🟢', 'SELL': '🔴'}
TF_EMOJI     = {'1h': '🕐', '4h': '🕓', '1d': '📅', '1w': '📆'}
DIR_EMOJI    = {'BUY': '🟢', 'SELL': '🔴', 'NEUTRAL': '⚪', 'MIXED': '🟡'}
SIGNAL_LABEL = {
    'REENTRY BUY':  '🔁 RE ENTRY',
    'REENTRY SELL': '🔁 RE ENTRY',
    'MMT BUY':      '⚡ MMT',
    'MMT SELL':     '⚡ MMT',
    'EXTREME BUY':  '💥 EXTREME',
    'EXTREME SELL': '💥 EXTREME',
}

# ==========================================
# 3. KONEKSI EXCHANGE — BINANCE FUTURES
# ==========================================
import threading as _threading

exchange = ccxt.binance({
    'apiKey':    API_KEY,
    'secret':    API_SECRET,
    'options':   {'defaultType': 'future'},   # USD-M Futures
    'enableRateLimit': True,
    'rateLimit': 300,   # ms antar request (ccxt internal limiter)
})

# ── Rate limiter terpusat — semua thread pakai satu antrian ───
# Binance batas: ~1200 weight/menit untuk REST, kita sangat konservatif
_api_lock         = _threading.Lock()   # satu request pada satu waktu
_last_request_at  = 0.0                 # timestamp request terakhir
_ban_until        = 0.0                 # timestamp kapan ban habis

def _safe_api_call(fn, *args, **kwargs):
    """
    Wrapper aman untuk semua panggilan Binance API:
    - Satu thread pada satu waktu (_api_lock)
    - Jeda minimum REQUEST_DELAY antar request
    - Deteksi 418/429 → tunggu BAN_RETRY_DELAY lalu retry
    """
    global _last_request_at, _ban_until

    for attempt in range(MAX_BAN_RETRIES + 1):
        # Jika masih dalam periode ban, tunggu dulu
        with _api_lock:
            now = time.time()
            if now < _ban_until:
                wait = _ban_until - now
                print(f"  ⏸️  IP ban aktif — menunggu {wait:.0f}s sebelum retry...")
            else:
                wait = 0.0

        if wait > 0:
            time.sleep(wait + 1)

        with _api_lock:
            # Paksa jeda minimum antar request
            elapsed = time.time() - _last_request_at
            if elapsed < REQUEST_DELAY:
                time.sleep(REQUEST_DELAY - elapsed)

            try:
                result = fn(*args, **kwargs)
                _last_request_at = time.time()
                return result

            except ccxt.RateLimitExceeded as e:
                _last_request_at = time.time()
                # Coba ekstrak durasi ban dari pesan error Binance
                msg = str(e)
                ban_ts = _extract_ban_timestamp(msg)
                if ban_ts:
                    _ban_until = ban_ts / 1000.0
                    wait_sec   = max(_ban_until - time.time(), BAN_RETRY_DELAY)
                else:
                    wait_sec   = BAN_RETRY_DELAY * (attempt + 1)
                    _ban_until = time.time() + wait_sec

                print(
                    f"  🚫 Rate limit / IP ban (418/429) — "
                    f"tunggu {wait_sec:.0f}s (percobaan {attempt+1}/{MAX_BAN_RETRIES})"
                )

            except Exception as e:
                _last_request_at = time.time()
                raise e   # error lain langsung lempar ke pemanggil

        # Tunggu di luar lock agar thread lain tidak ikut diblokir
        time.sleep(wait_sec)

    return None   # semua percobaan habis


def _extract_ban_timestamp(msg: str) -> int:
    """Ekstrak timestamp milidetik dari pesan error Binance (jika ada)."""
    import re
    m = re.search(r'banned until (\d{13})', msg)
    return int(m.group(1)) if m else 0

# ==========================================
# 4. STATE MANAGER (persist ke disk)
# ==========================================
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {
        'last_close':        {tf: 0.0 for tf in TIMEFRAMES},
        'processed_signals': {},
        'last_market_fetch': 0.0,
    }


def save_state(state: dict):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        print(f"  [State Error] {e}")

# ==========================================
# 5. AMBIL SEMUA MARKET FUTURES
# ==========================================
def get_all_futures_symbols() -> list:
    """
    Ambil semua pasang USDT futures aktif via satu panggilan fetch_tickers.
    Menggunakan _safe_api_call agar aman dari rate-limit / IP ban.
    """
    print("  📡 Mengambil semua market Binance Futures USDT...")

    # load_markets hanya 1x — ccxt cache otomatis, tidak perlu panggil ulang
    try:
        _safe_api_call(exchange.load_markets)
    except Exception as e:
        print(f"  [load_markets Error] {e}")
        return []

    tickers = _safe_api_call(exchange.fetch_tickers)
    if not tickers:
        print("  [Market Error] fetch_tickers gagal atau kena ban.")
        return []

    valid = []
    for s, t in tickers.items():
        if not (s.endswith('/USDT') or s.endswith('/USDT:USDT')):
            continue
        if any(x in s for x in ['UP/', 'DOWN/', 'BEAR/', 'BULL/']):
            continue
        vol = t.get('quoteVolume') or 0
        if vol < MIN_VOLUME:
            continue
        valid.append({
            'symbol': t['symbol'],
            'change': t.get('percentage') or 0.0,
            'volume': vol,
        })

    valid.sort(key=lambda x: x['volume'], reverse=True)
    print(f"  ✅ {len(valid)} pasang futures aktif (vol > ${MIN_VOLUME:,})")
    return valid

# ==========================================
# 6. FETCH OHLCV & SIMPAN KE DISK
# ==========================================
def fetch_and_save_ohlcv(symbol: str, timeframe: str) -> 'pd.DataFrame | None':
    """
    Ambil OHLCV dari Binance Futures via _safe_api_call (rate-limit aman),
    simpan CSV, kembalikan DataFrame.
    """
    try:
        bars = _safe_api_call(
            exchange.fetch_ohlcv, symbol, timeframe,
            limit=TF_LIMIT.get(timeframe, 200)
        )
        if not bars:
            return None

        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

        safe_sym = symbol.replace('/', '-').replace(':', '-')
        sym_dir  = DATA_DIR / safe_sym
        sym_dir.mkdir(exist_ok=True)
        df.to_csv(sym_dir / f"{timeframe}.csv", index=False)

        return df
    except Exception:
        return None


def load_saved_ohlcv(symbol: str, timeframe: str) -> 'pd.DataFrame | None':
    """Muat CSV yang sudah tersimpan dari disk."""
    try:
        safe_sym = symbol.replace('/', '-').replace(':', '-')
        csv_path = DATA_DIR / safe_sym / f"{timeframe}.csv"
        if csv_path.exists():
            df = pd.read_csv(csv_path)
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            return df
    except Exception:
        pass
    return None

# ==========================================
# 7. INDIKATOR BBMA
# ==========================================
def _wma(series: pd.Series, length: int) -> pd.Series:
    """Weighted Moving Average — identik wma() Pine Script."""
    weights = np.arange(1, length + 1, dtype=float)
    return series.rolling(length).apply(
        lambda x: np.dot(x, weights) / weights.sum(), raw=True
    )


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # Bollinger Bands
    df['midBB'] = df['close'].rolling(20).mean()
    df['BBdev'] = 2.0 * df['close'].rolling(20).std(ddof=0)
    df['topBB'] = df['midBB'].shift(1) + df['BBdev']
    df['lowBB'] = df['midBB'].shift(1) - df['BBdev']
    # MA High & Low (WMA)
    df['mahi5']  = _wma(df['high'], 5)
    df['mahi10'] = _wma(df['high'], 10)
    df['malo5']  = _wma(df['low'],  5)
    df['malo10'] = _wma(df['low'],  10)
    # Nilai bar sebelumnya (untuk kondisi EXTREME)
    df['mahi5_p'] = df['mahi5'].shift(1)
    df['malo5_p'] = df['malo5'].shift(1)
    df['topBB_p'] = df['topBB'].shift(1)
    df['lowBB_p'] = df['lowBB'].shift(1)
    # EMA 50
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    return df

# ==========================================
# 8. HITUNG SINYAL BBMA
# ==========================================
def compute_signals(df: pd.DataFrame) -> dict:
    """
    Hitung sinyal dari candle yang sudah closed:
      iloc[-2] = candle closed terbaru  ← titik sinyal
      iloc[-3] = candle sebelumnya
      iloc[-1] = candle running (diabaikan)
    """
    if df is None or len(df) < 30:
        return {}

    c    = df.iloc[-2]
    prev = df.iloc[-3]

    csz_c    = abs(c['close']    - c['open'])
    csz_prev = abs(prev['close'] - prev['open'])

    signals = {}

    # ── RE ENTRY ──────────────────────────────────────────────
    if (c['high']  > c['mahi5']
            and c['close'] < c['mahi5']
            and c['close'] < c['mahi10']
            and c['close'] < c['midBB']
            and c['mahi5'] < c['midBB']):
        signals['REENTRY SELL'] = {
            'tipe': 'SELL',
            'explanation': 'Harga ditolak dari MAHI5 — potensi turun lanjut.',
        }

    if (c['low']   < c['malo5']
            and c['close'] > c['malo5']
            and c['close'] > c['malo10']
            and c['close'] > c['midBB']
            and c['malo5'] > c['midBB']):
        signals['REENTRY BUY'] = {
            'tipe': 'BUY',
            'explanation': 'Harga ditolak dari MALO5 — potensi naik lanjut.',
        }

    # ── MMT / CSM ─────────────────────────────────────────────
    if (c['close'] < c['lowBB']) and (c['open'] > c['lowBB']):
        signals['MMT SELL'] = {
            'tipe': 'SELL',
            'explanation': 'Momentum — close menembus ke bawah LowBB (CSM Sell).',
        }

    if (c['close'] > c['topBB']) and (c['open'] < c['topBB']):
        signals['MMT BUY'] = {
            'tipe': 'BUY',
            'explanation': 'Momentum — close menembus ke atas TopBB (CSM Buy).',
        }

    # ── EXTREME (engulfing type) ───────────────────────────────
    if (prev['close'] > prev['open']
            and c['close'] < c['topBB']
            and c['close'] < c['open']
            and (c['mahi5_p'] > c['topBB_p'] or c['mahi5'] > c['topBB'])
            and csz_c > csz_prev / 2):
        signals['EXTREME SELL'] = {
            'tipe': 'SELL',
            'explanation': 'Engulfing bearish — MAHI5 di atas TopBB, reversal turun.',
        }

    if (prev['close'] < prev['open']
            and c['close'] > c['lowBB']
            and c['close'] > c['open']
            and (c['malo5_p'] < c['lowBB_p'] or c['malo5'] < c['lowBB'])
            and csz_c > csz_prev / 2):
        signals['EXTREME BUY'] = {
            'tipe': 'BUY',
            'explanation': 'Engulfing bullish — MALO5 di bawah LowBB, reversal naik.',
        }

    # Tambahkan harga & waktu ke setiap sinyal
    for k in signals:
        signals[k]['price'] = c['close']
        signals[k]['time']  = str(c['timestamp'])

    # Hanya kembalikan sinyal yang diizinkan
    return {k: v for k, v in signals.items() if k in ALLOWED_SIGNALS}

# ==========================================
# 9. MULTI-TIMEFRAME BIAS
# ==========================================
def get_mtf_bias(symbol: str) -> dict:
    """Cek arah BUY/SELL/NEUTRAL di semua TF menggunakan data dari disk."""
    bias = {}
    score_buy = score_sell = 0

    for tf in TIMEFRAMES:
        df = load_saved_ohlcv(symbol, tf)
        if df is None or len(df) < 30:
            bias[tf] = 'NEUTRAL'
            continue
        df = add_indicators(df)
        c  = df.iloc[-2]

        above_ema      = c['close'] > c['ema50']
        above_mid      = c['close'] > c['midBB']
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
# 10. CHART GENERATOR
# ==========================================
def generate_chart(df: pd.DataFrame, symbol: str, signal_name: str,
                   timeframe: str = '1h') -> 'str | None':
    try:
        safe_sig = signal_name.replace(' ', '_')
        safe_sym = symbol.replace('/', '-').replace(':', '-')
        filename = str(CHART_DIR / f"{safe_sym}_{timeframe}_{safe_sig}.png")

        tail    = 80 if timeframe in ('1d', '1w') else 100
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
        mpf.plot(
            plot_df, type='candle', style=style, addplot=adds,
            title=f"{symbol} [{timeframe.upper()}] — {signal_name}",
            savefig=dict(fname=filename, bbox_inches='tight'), volume=True,
        )
        return filename
    except Exception as e:
        print(f"  [Chart Error] {e}")
        return None

# ==========================================
# 11. TELEGRAM
# ==========================================
def _format_mtf_block(mtf: dict) -> str:
    lines = []
    for tf in TIMEFRAMES:
        d  = mtf.get(tf, 'NEUTRAL')
        em = DIR_EMOJI.get(d, '⚪')
        lines.append(f"  {TF_EMOJI.get(tf,'')} {tf.upper():>3} : {em} {d}")
    sc_key      = 'score_buy' if mtf.get('direction') == 'BUY' else 'score_sell'
    aligned_txt = (
        f"✅ ALIGNED {mtf['direction']} ({mtf.get(sc_key, 0)}/4 TF)"
        if mtf.get('aligned') else
        f"⚠️ MIXED ({mtf.get('score_buy', 0)} BUY / {mtf.get('score_sell', 0)} SELL)"
    )
    return "\n".join(lines) + f"\n  {aligned_txt}"


def send_telegram_alert(symbol: str, signal_name: str, timeframe: str,
                        data: dict, change_24h: float,
                        mtf: dict, image_path=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    icon      = SIGNAL_ICON.get(data['tipe'], '⚪')
    label     = SIGNAL_LABEL.get(signal_name, signal_name)
    tf_em     = TF_EMOJI.get(timeframe, '')
    mtf_blk   = _format_mtf_block(mtf)

    caption = (
        f"{icon} <b>BBMA FUTURES — {label} {data['tipe']}</b>\n"
        f"──────────────────────\n"
        f"💎 <b>Symbol  :</b> {symbol}\n"
        f"🏷 <b>Sinyal  :</b> {signal_name}\n"
        f"{tf_em} <b>TF      :</b> {timeframe.upper()}\n"
        f"💰 <b>Harga   :</b> {data['price']:.6g}\n"
        f"📈 <b>24h Chg :</b> {change_24h:+.2f}%\n"
        f"──────────────────────\n"
        f"📐 <b>Multi-TF Bias:</b>\n{mtf_blk}\n"
        f"──────────────────────\n"
        f"📝 <b>Analisa :</b> {data['explanation']}\n"
        f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} WIB"
    )

    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    try:
        if image_path and os.path.exists(image_path):
            with open(image_path, "rb") as img:
                requests.post(
                    f"{base}/sendPhoto",
                    data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'HTML'},
                    files={'photo': img}, timeout=20,
                )
        else:
            requests.post(
                f"{base}/sendMessage",
                data={'chat_id': TELEGRAM_CHAT_ID, 'text': caption, 'parse_mode': 'HTML'},
                timeout=20,
            )
    except Exception as e:
        print(f"  [TG Error] {e}")


def send_telegram_text(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'},
            timeout=10,
        )
    except Exception:
        pass

# ==========================================
# 12. FASE 1 — DOWNLOAD & SIMPAN SEMUA DATA
# ==========================================
def phase1_download(symbols: list, timeframes: list) -> dict:
    """
    Download OHLCV semua simbol × semua TF secara paralel dan simpan ke disk.
    Return: {symbol: {tf: DataFrame | None}}
    """
    print(f"\n  📥 FASE 1 — Download data {len(symbols)} simbol × {len(timeframes)} TF...")

    result  = {coin['symbol']: {} for coin in symbols}
    total   = len(symbols) * len(timeframes)
    done    = 0
    failed  = 0

    def _fetch(coin, tf):
        return coin['symbol'], tf, fetch_and_save_ohlcv(coin['symbol'], tf)

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_THREADS) as ex:
        futs = [ex.submit(_fetch, coin, tf) for coin in symbols for tf in timeframes]
        for f in concurrent.futures.as_completed(futs):
            sym, tf, df = f.result()
            result[sym][tf] = df
            done += 1
            if df is None:
                failed += 1
            if done % 100 == 0 or done == total:
                sys.stdout.write(
                    f"\r  Progress: {done}/{total}  "
                    f"(✅ {done - failed} ok | ❌ {failed} gagal)     "
                )
                sys.stdout.flush()

    print(f"\n  ✅ Fase 1 selesai — data disimpan di {DATA_DIR}/")
    return result

# ==========================================
# 13. FASE 2 — SCAN SINYAL DARI DATA TERSIMPAN
# ==========================================
def phase2_scan(symbols: list, timeframes: list,
                saved_data: dict, processed_signals: dict) -> list:
    """
    Scan sinyal RE ENTRY / MMT / EXTREME dari data yang sudah tersimpan.
    Hanya candle yang sudah close (iloc[-2]).
    Return: list alert siap kirim.
    """
    print(f"\n  🔍 FASE 2 — Scan sinyal per-close candle "
          f"{' | '.join(tf.upper() for tf in timeframes)}...")

    all_alerts  = []
    total_scanned = 0

    for coin in symbols:
        sym        = coin['symbol']
        change_24h = coin['change']
        mtf_bias   = None

        for tf in timeframes:
            df = saved_data.get(sym, {}).get(tf) or load_saved_ohlcv(sym, tf)
            if df is None or len(df) < 30:
                continue

            df = add_indicators(df)
            signals = compute_signals(df)
            total_scanned += 1

            for sig_name, sig_data in signals.items():
                sig_key = f"{sym}_{sig_name}_{tf}"

                # Anti-spam: skip jika candle ini sudah pernah dikirim
                if processed_signals.get(sig_key) == sig_data['time']:
                    continue

                if mtf_bias is None:
                    mtf_bias = get_mtf_bias(sym)

                all_alerts.append({
                    'symbol':     sym,
                    'signal':     sig_name,
                    'timeframe':  tf,
                    'data':       sig_data,
                    '24h_change': change_24h,
                    'df':         df,
                    'mtf':        mtf_bias,
                    'sig_key':    sig_key,
                    'priority':   TF_WEIGHT.get(tf, 1),
                })

    print(f"  ✅ Fase 2 selesai — {total_scanned} candle di-scan, "
          f"{len(all_alerts)} sinyal baru ditemukan.")
    return all_alerts

# ==========================================
# 14. HELPER — WAKTU CLOSE CANDLE
# ==========================================
def seconds_until_next_close(timeframe: str) -> float:
    """Hitung sisa detik hingga candle TF berikutnya menutup."""
    now = time.time()
    dur = TF_DURATION_SEC[timeframe]
    return max((int(now / dur) + 1) * dur - now, 0)


def get_due_timeframes(last_close: dict) -> list:
    """Return TF yang candle-nya sudah close tapi belum di-scan."""
    now = time.time()
    return [
        tf for tf in TIMEFRAMES
        if last_close.get(tf, 0) < int(now / TF_DURATION_SEC[tf]) * TF_DURATION_SEC[tf]
    ]

# ==========================================
# 15. SHARED STATE (thread-safe)
# ==========================================
import threading

_state_lock      = threading.Lock()   # melindungi processed_signals & last_close
_symbols_lock    = threading.Lock()   # melindungi daftar symbols
_tg_lock         = threading.Lock()   # satu-per-satu kirim ke Telegram
_shared_symbols  = []                 # dipakai semua thread TF


def _get_symbols() -> list:
    with _symbols_lock:
        return list(_shared_symbols)


def _set_symbols(sym_list: list):
    global _shared_symbols
    with _symbols_lock:
        _shared_symbols.clear()
        _shared_symbols.extend(sym_list)


# ==========================================
# 16. WORKER PER-TIMEFRAME
# ==========================================
def tf_worker(tf: str, state: dict, stop_event: threading.Event):
    """
    Thread mandiri untuk satu timeframe.
    Looping sendiri: tunggu close → download → scan → kirim.
    """
    processed_signals = state['processed_signals']
    last_close        = state['last_close']
    dur               = TF_DURATION_SEC[tf]
    tf_up             = tf.upper()
    first_run         = True

    print(f"  🔧 [{tf_up}] Worker dimulai.")

    while not stop_event.is_set():
        try:
            now = time.time()

            # ── Cek apakah candle sudah close ─────────────────
            current_close = int(now / dur) * dur
            with _state_lock:
                last = last_close.get(tf, 0.0)

            if not first_run and last >= current_close:
                # Belum ada candle baru — hitung waktu tunggu
                wait_sec = max(int(seconds_until_next_close(tf)) - 5, 10)
                if wait_sec > 120:
                    # Tidur bertahap agar bisa dihentikan dengan cepat
                    for _ in range(wait_sec // 30):
                        if stop_event.is_set():
                            return
                        time.sleep(30)
                    time.sleep(wait_sec % 30)
                else:
                    time.sleep(min(wait_sec, 60))
                continue

            first_run = False

            symbols = _get_symbols()
            if not symbols:
                time.sleep(10)
                continue

            print(
                f"\n[{datetime.now().strftime('%H:%M:%S')}] "
                f"⏰ [{tf_up}] Candle close — {len(symbols)} simbol"
            )
            scan_start = time.time()

            # ── FASE 1: Download data untuk TF ini ────────────
            saved_data = phase1_download(symbols, [tf])

            # Catat waktu close
            with _state_lock:
                last_close[tf] = current_close

            # ── FASE 2: Scan sinyal ───────────────────────────
            with _state_lock:
                proc_copy = dict(processed_signals)

            all_alerts = phase2_scan(symbols, [tf], saved_data, proc_copy)
            all_alerts.sort(key=lambda x: x['priority'], reverse=True)

            # ── Kirim sinyal ke Telegram ──────────────────────
            sent_count = 0
            for alert in all_alerts:
                sym      = alert['symbol']
                sig_name = alert['signal']
                sig_data = alert['data']
                mtf      = alert['mtf'] or {}

                with _state_lock:
                    processed_signals[alert['sig_key']] = sig_data['time']

                label = SIGNAL_LABEL.get(sig_name, sig_name)
                print(
                    f"  🔔 [{tf_up}] {sym:<22} | "
                    f"{label} {sig_data['tipe']:<4} @ {sig_data['price']:.6g}"
                )

                img = generate_chart(alert['df'], sym, sig_name, tf)
                with _tg_lock:
                    send_telegram_alert(
                        symbol=sym, signal_name=sig_name, timeframe=tf,
                        data=sig_data, change_24h=alert['24h_change'],
                        mtf=mtf, image_path=img,
                    )
                sent_count += 1
                time.sleep(0.4)

            dur_total = time.time() - scan_start
            print(
                f"  📊 [{tf_up}] Selesai: {len(symbols)} simbol | "
                f"{sent_count} sinyal | {dur_total:.1f}s"
            )
            if sent_count == 0:
                print(f"  [{tf_up}] Tidak ada sinyal baru.")

            # ── Simpan state ──────────────────────────────────
            with _state_lock:
                state['processed_signals'] = processed_signals
                state['last_close']        = last_close
                save_state(state)

        except Exception as e:
            print(f"  [{tf_up} Error] {e}")
            time.sleep(15)

    print(f"  🛑 [{tf_up}] Worker dihentikan.")


# ==========================================
# 17. SYMBOL REFRESH DAEMON
# ==========================================
def symbol_refresh_daemon(state: dict, stop_event: threading.Event):
    """
    Refresh daftar simbol tiap 4 jam di background thread.
    (lebih lama dari sebelumnya agar tidak sering panggil fetch_tickers)
    """
    REFRESH_INTERVAL = 4 * 3600   # 4 jam
    while not stop_event.is_set():
        now = time.time()
        if now - state.get('last_market_fetch', 0) > REFRESH_INTERVAL or not _shared_symbols:
            symbols = get_all_futures_symbols()
            if symbols:
                _set_symbols(symbols)
                with _state_lock:
                    state['last_market_fetch'] = now
                    save_state(state)
            else:
                print("  ⚠️  Gagal refresh market, akan retry 5 menit lagi...")
                time.sleep(300)
                continue
        # Cek tiap 60 detik
        for _ in range(60):
            if stop_event.is_set():
                return
            time.sleep(1)


# ==========================================
# 18. MAIN — JALANKAN SEMUA THREAD
# ==========================================
def main():
    print("=" * 65)
    print("  🚀  BBMA OMA ALLY — BINANCE FUTURES  (PARALLEL TF)")
    print("=" * 65)
    print(f"  Market   : Semua pasang USDT Binance Futures")
    print(f"  TF       : {' · '.join(tf.upper() for tf in TIMEFRAMES)}  ← berjalan BERSAMAAN")
    print(f"  Sinyal   : RE ENTRY · MMT · EXTREME  (BUY & SELL)")
    print(f"  Timing   : Per-close candle, tiap TF punya thread sendiri")
    print(f"  Data     : {DATA_DIR}/  |  Chart: {CHART_DIR}/")
    print("=" * 65)

    state = load_state()

    # ── Ambil market pertama kali (blokir sampai berhasil) ────
    symbols = get_all_futures_symbols()
    while not symbols:
        print("  Gagal ambil market. Retry 30s...")
        time.sleep(30)
        symbols = get_all_futures_symbols()
    _set_symbols(symbols)
    state['last_market_fetch'] = time.time()
    save_state(state)

    send_telegram_text(
        f"🚀 <b>BBMA Bot AKTIF — Binance Futures (Parallel TF)</b>\n"
        f"Sinyal: RE ENTRY · MMT · EXTREME\n"
        f"TF: 1H · 4H · 1D · 1W  ← Scan bersamaan!\n"
        f"Waktu: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    stop_event = threading.Event()
    threads    = []

    # ── Thread refresh simbol ──────────────────────────────────
    t_sym = threading.Thread(
        target=symbol_refresh_daemon,
        args=(state, stop_event),
        name="SymbolRefresh", daemon=True,
    )
    t_sym.start()
    threads.append(t_sym)

    # ── Satu thread per timeframe ──────────────────────────────
    for tf in TIMEFRAMES:
        t = threading.Thread(
            target=tf_worker,
            args=(tf, state, stop_event),
            name=f"TF-{tf.upper()}", daemon=True,
        )
        t.start()
        threads.append(t)
        time.sleep(0.5)   # stagger sedikit agar print tidak tabrakan

    print(f"\n  ✅ {len(TIMEFRAMES)} thread TF aktif + 1 thread symbol refresh")
    print("  Tekan Ctrl+C untuk menghentikan bot.\n")

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n⛔ Menghentikan semua thread...")
        stop_event.set()
        for t in threads:
            t.join(timeout=10)
        with _state_lock:
            state['processed_signals'] = state.get('processed_signals', {})
            save_state(state)
        send_telegram_text("⛔ <b>BBMA Bot dihentikan.</b>")
        print("✅ Bot berhenti.")


if __name__ == "__main__":
    main()
