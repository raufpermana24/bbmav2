"""
╔══════════════════════════════════════════════════════════════════╗
║      BBMA OMA ALLY — BINANCE FUTURES  (WebSocket + REST)        ║
║                                                                  ║
║  ARSITEKTUR DATA:                                                ║
║  1. Saat start  → REST seed OHLCV historis (1x per simbol/TF)  ║
║  2. Saat live   → WebSocket kline stream update candle realtime ║
║  3. Jika WS gap → REST fallback otomatis untuk tambal data      ║
║                                                                  ║
║  KEUNTUNGAN:                                                     ║
║  • Tidak ada polling REST → nol risiko IP ban 418               ║
║  • Deteksi candle close realtime dari event WS (is_closed=True) ║
║  • WebSocket Binance Futures: TIDAK ada rate-limit              ║
║  • Koneksi WS di-batch (maks 200 stream per koneksi)           ║
║                                                                  ║
║  SINYAL: RE ENTRY · MMT · EXTREME  (BUY & SELL)                ║
║  TF    : 1H · 4H · 1D · 1W                                     ║
╚══════════════════════════════════════════════════════════════════╝
"""

import sys
import os
import re
import time
import json
import threading
import warnings
import concurrent.futures
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
    import websocket          # pip install websocket-client
except ImportError as e:
    sys.exit(
        f"Library Error: {e}.\n"
        "Install dulu:\n"
        "  pip install ccxt pandas mplfinance requests numpy websocket-client"
    )

# ==========================================
# 2. KONFIGURASI
# ==========================================
API_KEY    = os.environ.get('BINANCE_API_KEY',    'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
API_SECRET = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN',   '8361349338:AAHOlx4fKz_pb1MHnVg8CxS9MY_pcejxLes')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003559364460')

# ── Timeframe ──────────────────────────────────────────────────
TIMEFRAMES = ['1h', '4h', '1d', '1w']

# Jumlah candle historis yang di-seed via REST saat pertama start
TF_SEED_LIMIT = {'1h': 300, '4h': 200, '1d': 200, '1w': 100}

# Minimum candle agar DataFrame dianggap lengkap untuk indikator
TF_MIN_ROWS = {'1h': 60, '4h': 50, '1d': 50, '1w': 30}

# Durasi satu candle (detik) — untuk hitung gap setelah reconnect
TF_DURATION_SEC = {'1h': 3600, '4h': 14400, '1d': 86400, '1w': 604800}

# Mapping TF bot → interval Binance WebSocket
TF_WS_INTERVAL = {'1h': '1h', '4h': '4h', '1d': '1d', '1w': '1w'}

TF_WEIGHT  = {'1h': 1, '4h': 2, '1d': 3, '1w': 4}
MIN_VOLUME = 1_000_000    # minimal quoteVolume $1 juta

# ── REST (hanya seed & fallback) ──────────────────────────────
REST_DELAY       = 0.3    # detik jeda antar request
REST_MAX_RETRY   = 5
BAN_WAIT_SEC     = 120
MAX_SEED_THREADS = 4      # thread paralel saat seed awal

# ── WebSocket ─────────────────────────────────────────────────
WS_BASE_URL      = "wss://fstream.binance.com/stream?streams="
WS_MAX_STREAMS   = 200    # batas Binance per koneksi
WS_RECONNECT_SEC = 5

# ── Direktori output ───────────────────────────────────────────
DATA_DIR   = Path('bbma_data')
CHART_DIR  = Path('bbma_charts')
STATE_FILE = DATA_DIR / '_state.json'

DATA_DIR.mkdir(exist_ok=True)
CHART_DIR.mkdir(exist_ok=True)

# ── Missed signal scan saat startup ──────────────────────────
# Hanya scan sinyal dalam 8 jam ke belakang dari waktu bot start
MISSED_LOOKBACK_SECONDS = 3600  # 8 jam
# Jeda antar pengiriman sinyal terlewat (detik), hindari flood TG
MISSED_SIGNAL_DELAY = 1.5

# ── Label sinyal ──────────────────────────────────────────────
ALLOWED_SIGNALS = {
    'REENTRY BUY', 'REENTRY SELL',
    'MMT BUY',     'MMT SELL',
    'EXTREME BUY', 'EXTREME SELL',
}
SIGNAL_ICON  = {'BUY': '🟢', 'SELL': '🔴'}
TF_EMOJI     = {'1h': '🕐', '4h': '🕓', '1d': '📅', '1w': '📆'}
DIR_EMOJI    = {'BUY': '🟢', 'SELL': '🔴', 'NEUTRAL': '⚪', 'MIXED': '🟡'}
SIGNAL_LABEL = {
    'REENTRY BUY':  '🔁 RE ENTRY',  'REENTRY SELL': '🔁 RE ENTRY',
    'MMT BUY':      '⚡ MMT',        'MMT SELL':     '⚡ MMT',
    'EXTREME BUY':  '💥 EXTREME',    'EXTREME SELL': '💥 EXTREME',
}

# ==========================================
# 3. KONEKSI REST — hanya untuk seed & fallback
# ==========================================
exchange = ccxt.binance({
    'apiKey':    API_KEY,
    'secret':    API_SECRET,
    'options':   {'defaultType': 'future'},
    'enableRateLimit': True,
    'rateLimit': 300,
})

_rest_lock    = threading.Lock()
_last_rest_at = 0.0
_ban_until_ts = 0.0


def _extract_ban_ts(msg: str) -> int:
    m = re.search(r'banned until (\d{13})', msg)
    return int(m.group(1)) if m else 0


def _rest_call(fn, *args, **kwargs):
    """
    Semua panggilan REST melalui sini:
    - Satu-per-satu (_rest_lock)
    - Jeda minimum REST_DELAY
    - Auto-retry + tunggu jika kena ban 418/429
    """
    global _last_rest_at, _ban_until_ts

    for attempt in range(REST_MAX_RETRY + 1):
        ban_wait = max(_ban_until_ts - time.time(), 0)
        if ban_wait > 0:
            print(f"  ⏸️  REST ban aktif, tunggu {ban_wait:.0f}s...")
            time.sleep(ban_wait + 1)

        with _rest_lock:
            elapsed = time.time() - _last_rest_at
            if elapsed < REST_DELAY:
                time.sleep(REST_DELAY - elapsed)
            try:
                result = fn(*args, **kwargs)
                _last_rest_at = time.time()
                return result
            except ccxt.RateLimitExceeded as e:
                _last_rest_at = time.time()
                ban_ts = _extract_ban_ts(str(e))
                if ban_ts:
                    _ban_until_ts = ban_ts / 1000.0
                    wait = max(_ban_until_ts - time.time(), BAN_WAIT_SEC)
                else:
                    wait = BAN_WAIT_SEC * (attempt + 1)
                    _ban_until_ts = time.time() + wait
                print(f"  🚫 REST ban — tunggu {wait:.0f}s "
                      f"(percobaan {attempt+1}/{REST_MAX_RETRY})")
            except Exception as e:
                _last_rest_at = time.time()
                raise e

        time.sleep(BAN_WAIT_SEC)

    return None

# ==========================================
# 4. STATE MANAGER
# ==========================================
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {'processed_signals': {}, 'last_market_fetch': 0.0}


def save_state(state: dict):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        print(f"  [State Error] {e}")

# ==========================================
# 5. AMBIL DAFTAR SIMBOL (REST — 1x saat start, refresh tiap 4 jam)
# ==========================================
def get_all_futures_symbols() -> list:
    print("  📡 Mengambil daftar market Binance Futures USDT...")
    try:
        _rest_call(exchange.load_markets)
    except Exception as e:
        print(f"  [load_markets Error] {e}")
        return []

    tickers = _rest_call(exchange.fetch_tickers)
    if not tickers:
        print("  [Market Error] fetch_tickers gagal.")
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
# 6. DISK I/O
# ==========================================
def _csv_path(symbol: str, tf: str) -> Path:
    safe = symbol.replace('/', '-').replace(':', '-')
    d = DATA_DIR / safe
    d.mkdir(exist_ok=True)
    return d / f"{tf}.csv"


def save_ohlcv(symbol: str, tf: str, df: pd.DataFrame):
    try:
        df.to_csv(_csv_path(symbol, tf), index=False)
    except Exception as e:
        print(f"  [Save Error] {symbol} {tf}: {e}")


def load_ohlcv(symbol: str, tf: str) -> 'pd.DataFrame | None':
    try:
        p = _csv_path(symbol, tf)
        if p.exists():
            df = pd.read_csv(p)
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            return df
    except Exception:
        pass
    return None

# ==========================================
# 7. IN-MEMORY OHLCV STORE
#    Diupdate live oleh WebSocket, dibaca oleh signal processor
# ==========================================
_store_lock  = threading.Lock()
_ohlcv_store: dict = {}   # { symbol: { tf: DataFrame } }


def store_get(symbol: str, tf: str) -> 'pd.DataFrame | None':
    with _store_lock:
        sym_data = _ohlcv_store.get(symbol)
        if sym_data is None:
            return None
        df = sym_data.get(tf)
        return df.copy() if df is not None else None


def store_set(symbol: str, tf: str, df: pd.DataFrame):
    with _store_lock:
        if symbol not in _ohlcv_store:
            _ohlcv_store[symbol] = {}
        _ohlcv_store[symbol][tf] = df.copy()


def store_update_candle(symbol: str, tf: str,
                        ts_ms: int, o: float, h: float,
                        lo: float, c: float, v: float):
    """
    Update candle di store dari data WebSocket.
    - Jika timestamp sama dengan candle terakhir → update (candle running)
    - Jika timestamp lebih baru → append (candle baru)
    Setelah update tulis ke CSV (di luar lock).
    """
    ts  = pd.Timestamp(ts_ms, unit='ms')
    row = {'timestamp': ts, 'open': o, 'high': h, 'low': lo,
           'close': c, 'volume': v}

    df_to_save = None
    with _store_lock:
        sym_data = _ohlcv_store.get(symbol, {})
        df = sym_data.get(tf)
        if df is None or df.empty:
            return   # belum di-seed, skip

        last_ts = df.iloc[-1]['timestamp']
        if ts == last_ts:
            df.iloc[-1] = row
        elif ts > last_ts:
            new_row = pd.DataFrame([row])
            df = pd.concat([df, new_row], ignore_index=True)
            max_rows = TF_SEED_LIMIT.get(tf, 200) + 50
            if len(df) > max_rows:
                df = df.iloc[-max_rows:].reset_index(drop=True)

        _ohlcv_store.setdefault(symbol, {})[tf] = df
        df_to_save = df.copy()

    if df_to_save is not None:
        save_ohlcv(symbol, tf, df_to_save)


def _is_data_complete(df: 'pd.DataFrame | None', tf: str) -> bool:
    if df is None:
        return False
    return len(df) >= TF_MIN_ROWS.get(tf, 30)

# ==========================================
# 8. REST SEED — ambil historis saat pertama start
# ==========================================
def rest_seed_one(symbol: str, tf: str) -> bool:
    """Ambil OHLCV historis via REST untuk satu simbol/TF."""
    try:
        bars = _rest_call(
            exchange.fetch_ohlcv, symbol, tf,
            limit=TF_SEED_LIMIT.get(tf, 200)
        )
        if not bars:
            return False
        df = pd.DataFrame(bars,
                          columns=['timestamp', 'open', 'high',
                                   'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        store_set(symbol, tf, df)
        save_ohlcv(symbol, tf, df)
        return True
    except Exception:
        return False


def rest_seed_all(symbols: list):
    """
    Seed semua simbol × TF:
    1. Coba load dari disk dulu (tidak pakai REST)
    2. Jika data di disk kurang → REST
    """
    total  = len(symbols) * len(TIMEFRAMES)
    done   = 0
    seeded = 0

    def _seed_one(coin, tf):
        sym = coin['symbol']
        df  = load_ohlcv(sym, tf)
        if _is_data_complete(df, tf):
            store_set(sym, tf, df)
            return 'disk'
        ok = rest_seed_one(sym, tf)
        return 'rest' if ok else 'fail'

    print(f"\n  🌱 Seed historis {len(symbols)} simbol × {len(TIMEFRAMES)} TF "
          f"({MAX_SEED_THREADS} thread paralel)...")

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_SEED_THREADS) as ex:
        futs = {ex.submit(_seed_one, coin, tf): (coin['symbol'], tf)
                for coin in symbols for tf in TIMEFRAMES}
        for f in concurrent.futures.as_completed(futs):
            done += 1
            src = f.result()
            if src != 'fail':
                seeded += 1
            if done % 50 == 0 or done == total:
                sys.stdout.write(
                    f"\r  Seed: {done}/{total}  "
                    f"(✅ {seeded} ok | ❌ {done - seeded} gagal)   "
                )
                sys.stdout.flush()

    print(f"\n  ✅ Seed selesai — {seeded}/{total} berhasil.")

# ==========================================
# 9. REST FALLBACK — tambal gap setelah WS disconnect / data kurang
# ==========================================
def rest_fill_gap(symbol: str, tf: str):
    """
    Ambil candle terbaru via REST untuk mengisi gap.
    Hanya request candle yang belum ada di store.
    """
    try:
        df_existing = store_get(symbol, tf)
        if df_existing is None or df_existing.empty:
            rest_seed_one(symbol, tf)
            return

        dur         = TF_DURATION_SEC[tf]
        last_ts     = df_existing.iloc[-1]['timestamp']
        gap_candles = max(
            int((pd.Timestamp.now() - last_ts).total_seconds() / dur) + 2, 5
        )
        gap_candles = min(gap_candles, TF_SEED_LIMIT[tf])

        bars = _rest_call(exchange.fetch_ohlcv, symbol, tf, limit=gap_candles)
        if not bars:
            return

        df_new = pd.DataFrame(bars,
                              columns=['timestamp', 'open', 'high',
                                       'low', 'close', 'volume'])
        df_new['timestamp'] = pd.to_datetime(df_new['timestamp'], unit='ms')

        df_merged = pd.concat([df_existing, df_new], ignore_index=True)
        df_merged.drop_duplicates(subset='timestamp', keep='last', inplace=True)
        df_merged.sort_values('timestamp', inplace=True)
        df_merged.reset_index(drop=True, inplace=True)

        max_rows = TF_SEED_LIMIT.get(tf, 200) + 50
        if len(df_merged) > max_rows:
            df_merged = df_merged.iloc[-max_rows:].reset_index(drop=True)

        store_set(symbol, tf, df_merged)
        save_ohlcv(symbol, tf, df_merged)
        added = len(df_new[df_new['timestamp'] > last_ts])
        if added > 0:
            print(f"  🔧 [REST fallback] {symbol} {tf}: +{added} candle")

    except Exception as e:
        print(f"  [REST fallback Error] {symbol} {tf}: {e}")

# ==========================================
# 10. INDIKATOR BBMA
# ==========================================
def _wma(series: pd.Series, length: int) -> pd.Series:
    weights = np.arange(1, length + 1, dtype=float)
    return series.rolling(length).apply(
        lambda x: np.dot(x, weights) / weights.sum(), raw=True
    )


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['midBB']   = df['close'].rolling(20).mean()
    df['BBdev']   = 2.0 * df['close'].rolling(20).std(ddof=0)
    df['topBB']   = df['midBB'].shift(1) + df['BBdev']
    df['lowBB']   = df['midBB'].shift(1) - df['BBdev']
    df['mahi5']   = _wma(df['high'], 5)
    df['mahi10']  = _wma(df['high'], 10)
    df['malo5']   = _wma(df['low'],  5)
    df['malo10']  = _wma(df['low'],  10)
    df['mahi5_p'] = df['mahi5'].shift(1)
    df['malo5_p'] = df['malo5'].shift(1)
    df['topBB_p'] = df['topBB'].shift(1)
    df['lowBB_p'] = df['lowBB'].shift(1)
    df['ema50']   = df['close'].ewm(span=50, adjust=False).mean()
    return df

# ==========================================
# 11. HITUNG SINYAL BBMA
# ==========================================
def compute_signals(df: pd.DataFrame) -> dict:
    if df is None or len(df) < 30:
        return {}

    c    = df.iloc[-2]    # candle closed terbaru
    prev = df.iloc[-3]

    csz_c    = abs(c['close']    - c['open'])
    csz_prev = abs(prev['close'] - prev['open'])
    signals  = {}

    # RE ENTRY
    if (c['high'] > c['mahi5']
            and c['close'] < c['mahi5']
            and c['close'] < c['mahi10']
            and c['close'] < c['midBB']
            and c['mahi5'] < c['midBB']):
        signals['REENTRY SELL'] = {
            'tipe': 'SELL',
            'explanation': 'Harga ditolak dari MAHI5 — potensi turun lanjut.',
        }

    if (c['low'] < c['malo5']
            and c['close'] > c['malo5']
            and c['close'] > c['malo10']
            and c['close'] > c['midBB']
            and c['malo5'] > c['midBB']):
        signals['REENTRY BUY'] = {
            'tipe': 'BUY',
            'explanation': 'Harga ditolak dari MALO5 — potensi naik lanjut.',
        }

    # MMT / CSM
    if c['close'] < c['lowBB'] and c['open'] > c['lowBB']:
        signals['MMT SELL'] = {
            'tipe': 'SELL',
            'explanation': 'Momentum — close menembus ke bawah LowBB (CSM Sell).',
        }

    if c['close'] > c['topBB'] and c['open'] < c['topBB']:
        signals['MMT BUY'] = {
            'tipe': 'BUY',
            'explanation': 'Momentum — close menembus ke atas TopBB (CSM Buy).',
        }

    # EXTREME
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

    for k in signals:
        signals[k]['price'] = float(c['close'])
        signals[k]['time']  = str(c['timestamp'])

    return {k: v for k, v in signals.items() if k in ALLOWED_SIGNALS}


def compute_signals_at(df: pd.DataFrame, idx: int) -> dict:
    """
    Hitung sinyal BBMA pada candle indeks `idx` (bukan hanya candle terakhir).
    Dipakai saat scan sinyal terlewatkan saat startup.
    `idx` adalah posisi candle yang sudah *close* (candle[-1] = candle running,
    candle[-2] = closed terbaru, dst).
    """
    # Butuh minimal idx+2 baris (c = idx, prev = idx-1)
    if df is None or len(df) < idx + 3:
        return {}

    c    = df.iloc[-(idx + 2)]   # candle closed yang di-scan
    prev = df.iloc[-(idx + 3)]   # candle sebelumnya

    csz_c    = abs(c['close']    - c['open'])
    csz_prev = abs(prev['close'] - prev['open'])
    signals  = {}

    if (c['high'] > c['mahi5']
            and c['close'] < c['mahi5']
            and c['close'] < c['mahi10']
            and c['close'] < c['midBB']
            and c['mahi5'] < c['midBB']):
        signals['REENTRY SELL'] = {
            'tipe': 'SELL',
            'explanation': 'Harga ditolak dari MAHI5 — potensi turun lanjut.',
        }

    if (c['low'] < c['malo5']
            and c['close'] > c['malo5']
            and c['close'] > c['malo10']
            and c['close'] > c['midBB']
            and c['malo5'] > c['midBB']):
        signals['REENTRY BUY'] = {
            'tipe': 'BUY',
            'explanation': 'Harga ditolak dari MALO5 — potensi naik lanjut.',
        }

    if c['close'] < c['lowBB'] and c['open'] > c['lowBB']:
        signals['MMT SELL'] = {
            'tipe': 'SELL',
            'explanation': 'Momentum — close menembus ke bawah LowBB (CSM Sell).',
        }

    if c['close'] > c['topBB'] and c['open'] < c['topBB']:
        signals['MMT BUY'] = {
            'tipe': 'BUY',
            'explanation': 'Momentum — close menembus ke atas TopBB (CSM Buy).',
        }

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

    for k in signals:
        signals[k]['price'] = float(c['close'])
        signals[k]['time']  = str(c['timestamp'])

    return {k: v for k, v in signals.items() if k in ALLOWED_SIGNALS}

# ==========================================
# 12. MULTI-TIMEFRAME BIAS
# ==========================================
def get_mtf_bias(symbol: str) -> dict:
    bias = {}
    score_buy = score_sell = 0

    for tf in TIMEFRAMES:
        df = store_get(symbol, tf)
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
# 13. CHART GENERATOR
# ==========================================
def generate_chart(df: pd.DataFrame, symbol: str,
                   signal_name: str, timeframe: str) -> 'str | None':
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
# 14. TELEGRAM
# ==========================================
_tg_lock = threading.Lock()


def _format_mtf_block(mtf: dict) -> str:
    lines = []
    for tf in TIMEFRAMES:
        d  = mtf.get(tf, 'NEUTRAL')
        em = DIR_EMOJI.get(d, '⚪')
        lines.append(f"  {TF_EMOJI.get(tf,'')} {tf.upper():>3} : {em} {d}")
    sc_key = 'score_buy' if mtf.get('direction') == 'BUY' else 'score_sell'
    aligned_txt = (
        f"✅ ALIGNED {mtf['direction']} ({mtf.get(sc_key, 0)}/4 TF)"
        if mtf.get('aligned') else
        f"⚠️ MIXED ({mtf.get('score_buy',0)} BUY / {mtf.get('score_sell',0)} SELL)"
    )
    return "\n".join(lines) + f"\n  {aligned_txt}"


def send_telegram_alert(symbol: str, signal_name: str, timeframe: str,
                        data: dict, change_24h: float,
                        mtf: dict, image_path=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    icon    = SIGNAL_ICON.get(data['tipe'], '⚪')
    label   = SIGNAL_LABEL.get(signal_name, signal_name)
    tf_em   = TF_EMOJI.get(timeframe, '')
    mtf_blk = _format_mtf_block(mtf)

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
    with _tg_lock:
        try:
            if image_path and os.path.exists(image_path):
                with open(image_path, "rb") as img:
                    requests.post(
                        f"{base}/sendPhoto",
                        data={'chat_id': TELEGRAM_CHAT_ID,
                              'caption': caption, 'parse_mode': 'HTML'},
                        files={'photo': img}, timeout=20,
                    )
            else:
                requests.post(
                    f"{base}/sendMessage",
                    data={'chat_id': TELEGRAM_CHAT_ID,
                          'text': caption, 'parse_mode': 'HTML'},
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
# 15. SIGNAL PROCESSOR — dipanggil saat candle close via WS
# ==========================================
_processed_signals: dict = {}
_proc_lock = threading.Lock()


def on_candle_close(symbol: str, tf: str, change_24h: float = 0.0):
    """
    Dipanggil oleh WebSocket handler setiap kali candle TF close.
    Alur: cek data lengkap → hitung indikator → scan sinyal → kirim TG.
    Jika data kurang → minta REST fallback dulu.
    """
    df = store_get(symbol, tf)

    # Data tidak lengkap → REST fallback, lalu coba lagi setelah selesai
    if not _is_data_complete(df, tf):
        print(f"  ⚠️  [{tf.upper()}] {symbol} data kurang "
              f"({len(df) if df is not None else 0} baris) → REST fallback")
        rest_fill_gap(symbol, tf)
        df = store_get(symbol, tf)   # ambil ulang setelah fallback
        if not _is_data_complete(df, tf):
            return   # masih kurang setelah fallback, lewati

    df = add_indicators(df)
    signals = compute_signals(df)
    if not signals:
        return

    mtf = get_mtf_bias(symbol)

    for sig_name, sig_data in signals.items():
        sig_key = f"{symbol}_{sig_name}_{tf}"
        with _proc_lock:
            if _processed_signals.get(sig_key) == sig_data['time']:
                continue   # anti-spam
            _processed_signals[sig_key] = sig_data['time']

        label = SIGNAL_LABEL.get(sig_name, sig_name)
        print(
            f"  🔔 [{tf.upper()}] {symbol:<22} | "
            f"{label} {sig_data['tipe']:<4} @ {sig_data['price']:.6g}"
        )

        img = generate_chart(df, symbol, sig_name, tf)
        send_telegram_alert(
            symbol=symbol, signal_name=sig_name, timeframe=tf,
            data=sig_data, change_24h=change_24h,
            mtf=mtf, image_path=img,
        )

# ==========================================
# 15b. SCAN SINYAL TERLEWATKAN — dijalankan sekali saat startup
# ==========================================
def scan_missed_signals(symbols: list):
    """
    Scan candle-candle yang close-nya jatuh dalam 8 jam terakhir
    (MISSED_LOOKBACK_SECONDS dari waktu bot start).

    Logika filter waktu:
      - Ambil timestamp close tiap candle dari data historis.
      - Hanya proses candle yang close-nya >= (now - 8 jam).
      - Candle running (candle terakhir, masih berjalan) dilewati.

    Sinyal yang ditemukan dikirim ke Telegram dengan label ⏪ MISSED,
    lalu dicatat di _processed_signals agar tidak dikirim ulang saat live.
    """
    global _processed_signals

    now_ts     = pd.Timestamp.now(tz='UTC').tz_localize(None)
    cutoff_ts  = now_ts - pd.Timedelta(seconds=MISSED_LOOKBACK_SECONDS)

    print("\n" + "=" * 65)
    print("  ⏪  SCANNING SINYAL 8 JAM TERAKHIR (Missed Signal Scan)...")
    print(f"  Rentang  : {cutoff_ts.strftime('%Y-%m-%d %H:%M')} → sekarang")
    print("=" * 65)

    total_missed = 0

    for coin in symbols:
        sym    = coin['symbol']
        change = coin.get('change', 0.0)

        for tf in TIMEFRAMES:
            df_raw = store_get(sym, tf)
            if not _is_data_complete(df_raw, tf):
                continue

            df = add_indicators(df_raw)

            # Candle closed = semua kecuali baris terakhir (yang masih running)
            # Kita cari indeks candle (dari belakang) yang close-nya >= cutoff.
            # df.iloc[-1]  → candle running   (skip)
            # df.iloc[-2]  → closed terbaru   (idx=0 di compute_signals_at)
            # df.iloc[-3]  → closed sebelumnya (idx=1), dst.
            # Durasi satu candle dipakai untuk menentukan berapa candle
            # yang perlu di-cek agar mencakup tepat 8 jam.
            dur_sec   = TF_DURATION_SEC[tf]
            # Jumlah candle maksimal yang bisa menutup dalam 8 jam
            max_back  = max(int(MISSED_LOOKBACK_SECONDS / dur_sec) + 1, 1)

            mtf = None   # lazy — hitung sekali hanya jika ada sinyal

            # Scan dari candle paling lama ke paling baru dalam window
            for idx in range(max_back - 1, -1, -1):
                # Pastikan indeks tidak melampaui panjang df
                row_pos = -(idx + 2)   # +2 karena -1 = running, -2 = closed[0]
                if abs(row_pos) > len(df):
                    continue

                candle_ts = df.iloc[row_pos]['timestamp']
                # Normalisasi timezone agar perbandingan tidak error
                if hasattr(candle_ts, 'tzinfo') and candle_ts.tzinfo is not None:
                    candle_ts = candle_ts.tz_localize(None)

                # Lewati candle di luar window 8 jam
                if candle_ts < cutoff_ts:
                    continue

                sigs = compute_signals_at(df, idx)
                if not sigs:
                    continue

                if mtf is None:
                    mtf = get_mtf_bias(sym)

                for sig_name, sig_data in sigs.items():
                    sig_key = f"{sym}_{sig_name}_{tf}"

                    with _proc_lock:
                        if _processed_signals.get(sig_key) == sig_data['time']:
                            continue   # sudah pernah dikirim
                        _processed_signals[sig_key] = sig_data['time']

                    label       = f"⏪ {SIGNAL_LABEL.get(sig_name, sig_name)}"
                    icon        = SIGNAL_ICON.get(sig_data['tipe'], '⚪')
                    tf_em       = TF_EMOJI.get(tf, '')
                    mtf_blk     = _format_mtf_block(mtf)
                    candle_time = sig_data['time']

                    caption = (
                        f"⏪ {icon} <b>MISSED — {label} {sig_data['tipe']}</b>\n"
                        f"──────────────────────\n"
                        f"💎 <b>Symbol  :</b> {sym}\n"
                        f"🏷 <b>Sinyal  :</b> {sig_name}\n"
                        f"{tf_em} <b>TF      :</b> {tf.upper()}\n"
                        f"💰 <b>Harga   :</b> {sig_data['price']:.6g}\n"
                        f"📈 <b>24h Chg :</b> {change:+.2f}%\n"
                        f"🕯 <b>Candle  :</b> {candle_time}\n"
                        f"──────────────────────\n"
                        f"📐 <b>Multi-TF Bias:</b>\n{mtf_blk}\n"
                        f"──────────────────────\n"
                        f"📝 <b>Analisa :</b> {sig_data['explanation']}\n"
                        f"⚠️  <i>Sinyal terlewat dalam 8 jam terakhir</i>\n"
                        f"🕒 Scan: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )

                    print(
                        f"  ⏪ [{tf.upper()}] {sym:<22} | "
                        f"{SIGNAL_LABEL.get(sig_name, sig_name)} "
                        f"{sig_data['tipe']:<4} @ {sig_data['price']:.6g} "
                        f"(candle: {candle_time})"
                    )

                    # Generate chart BBMA (sama seperti sinyal live)
                    img = generate_chart(df, sym, sig_name, tf)

                    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
                    try:
                        with _tg_lock:
                            if img and os.path.exists(img):
                                with open(img, "rb") as photo:
                                    requests.post(
                                        f"{base}/sendPhoto",
                                        data={
                                            'chat_id':    TELEGRAM_CHAT_ID,
                                            'caption':    caption,
                                            'parse_mode': 'HTML',
                                        },
                                        files={'photo': photo},
                                        timeout=30,
                                    )
                            else:
                                # Fallback teks jika chart gagal dibuat
                                requests.post(
                                    f"{base}/sendMessage",
                                    data={
                                        'chat_id':    TELEGRAM_CHAT_ID,
                                        'text':       caption,
                                        'parse_mode': 'HTML',
                                    },
                                    timeout=20,
                                )
                    except Exception as e:
                        print(f"  [TG Missed Error] {e}")

                    total_missed += 1
                    time.sleep(MISSED_SIGNAL_DELAY)   # anti-flood Telegram

    if total_missed:
        print(f"\n  ✅ Missed scan selesai — {total_missed} sinyal dikirim.")
        send_telegram_text(
            f"⏪ <b>Missed Signal Scan Selesai</b>\n"
            f"Sinyal terlewat (8 jam terakhir): <b>{total_missed}</b>\n"
            f"Bot kini masuk mode LIVE (WebSocket)."
        )
    else:
        print("  ✅ Missed scan selesai — tidak ada sinyal dalam 8 jam terakhir.")

# ==========================================
# 16. WEBSOCKET — koneksi kline stream
# ==========================================
# Lookup cepat: "btcusdt_1h" → (symbol_ccxt, tf, change_24h)
_stream_map      : dict = {}
_stream_map_lock = threading.Lock()


def _symbol_to_ws(symbol: str) -> str:
    """'BTC/USDT:USDT' → 'btcusdt'"""
    base = symbol.split('/')[0]
    return (base + 'usdt').lower()


def _build_stream_name(symbol: str, tf: str) -> str:
    return f"{_symbol_to_ws(symbol)}@kline_{TF_WS_INTERVAL[tf]}"


def _register_streams(symbols: list):
    """Buat lookup _stream_map dari daftar simbol."""
    with _stream_map_lock:
        _stream_map.clear()
        for coin in symbols:
            sym = coin['symbol']
            chg = coin.get('change', 0.0)
            for tf in TIMEFRAMES:
                ws_sym = _symbol_to_ws(sym)
                key    = f"{ws_sym}_{TF_WS_INTERVAL[tf]}"
                _stream_map[key] = (sym, tf, chg)


class KlineWsConnection:
    """
    Satu koneksi WebSocket Binance Futures untuk sekumpulan stream kline.
    Reconnect otomatis jika terputus + REST fallback untuk tambal gap.
    """

    def __init__(self, stream_names: list, conn_id: int,
                 stop_event: threading.Event):
        self.stream_names = stream_names
        self.conn_id      = conn_id
        self.stop_event   = stop_event
        self._ws          = None

    # ── Callback WebSocket ────────────────────────────────────
    def _on_message(self, ws, raw):
        try:
            msg  = json.loads(raw)
            data = msg.get('data', msg)   # combined stream punya wrapper 'data'
            if data.get('e') != 'kline':
                return

            k        = data['k']
            ws_sym   = data['s'].lower()   # e.g. "btcusdt"
            interval = k['i']              # e.g. "1h"
            key      = f"{ws_sym}_{interval}"

            with _stream_map_lock:
                entry = _stream_map.get(key)
            if entry is None:
                return

            symbol, tf, change_24h = entry
            ts_ms  = int(k['t'])
            o, h   = float(k['o']), float(k['h'])
            lo, c  = float(k['l']), float(k['c'])
            v      = float(k['v'])
            closed = bool(k['x'])   # True = candle sudah close

            # Selalu update candle running di store
            store_update_candle(symbol, tf, ts_ms, o, h, lo, c, v)

            # Proses sinyal hanya saat candle benar-benar close
            if closed:
                threading.Thread(
                    target=on_candle_close,
                    args=(symbol, tf, change_24h),
                    daemon=True,
                ).start()

        except Exception as e:
            print(f"  [WS-{self.conn_id} msg] {e}")

    def _on_error(self, ws, error):
        print(f"  [WS-{self.conn_id}] Error: {error}")

    def _on_close(self, ws, code, msg):
        if not self.stop_event.is_set():
            print(f"  [WS-{self.conn_id}] Terputus ({code}) — reconnect...")

    def _on_open(self, ws):
        print(f"  [WS-{self.conn_id}] ✅ Terhubung "
              f"({len(self.stream_names)} stream)")

    # ── REST fallback setelah reconnect ──────────────────────
    def _fill_gaps_after_reconnect(self):
        """Tambal gap candle yang mungkin hilang selama WS mati."""
        seen = set()
        with _stream_map_lock:
            for sname in self.stream_names:
                # format: "btcusdt@kline_1h"
                parts = sname.split('@kline_')
                if len(parts) != 2:
                    continue
                key = f"{parts[0]}_{parts[1]}"
                entry = _stream_map.get(key)
                if entry:
                    sym, tf, _ = entry
                    seen.add((sym, tf))

        for sym, tf in seen:
            rest_fill_gap(sym, tf)

    # ── Loop utama ────────────────────────────────────────────
    def run(self):
        url = WS_BASE_URL + "/".join(self.stream_names)
        while not self.stop_event.is_set():
            try:
                self._ws = websocket.WebSocketApp(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                print(f"  [WS-{self.conn_id}] run_forever: {e}")

            if not self.stop_event.is_set():
                print(f"  [WS-{self.conn_id}] Reconnect "
                      f"dalam {WS_RECONNECT_SEC}s...")
                self._fill_gaps_after_reconnect()
                time.sleep(WS_RECONNECT_SEC)

    def stop(self):
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass


def launch_websocket_connections(symbols: list,
                                 stop_event: threading.Event) -> list:
    """
    Kumpulkan semua stream kline, bagi ke batch WS_MAX_STREAMS,
    jalankan tiap batch di thread terpisah.
    Return: list of (thread, KlineWsConnection)
    """
    all_streams = []
    for coin in symbols:
        sym = coin['symbol']
        for tf in TIMEFRAMES:
            all_streams.append(_build_stream_name(sym, tf))

    batches = [
        all_streams[i:i + WS_MAX_STREAMS]
        for i in range(0, len(all_streams), WS_MAX_STREAMS)
    ]

    print(f"\n  📡 WebSocket: {len(all_streams)} stream "
          f"→ {len(batches)} koneksi (maks {WS_MAX_STREAMS}/koneksi)")

    result = []
    for idx, batch in enumerate(batches):
        conn = KlineWsConnection(batch, conn_id=idx + 1,
                                 stop_event=stop_event)
        t = threading.Thread(target=conn.run,
                             name=f"WS-{idx+1}", daemon=True)
        t.start()
        result.append((t, conn))
        time.sleep(0.3)   # stagger koneksi

    return result

# ==========================================
# 17. DAEMON — refresh simbol tiap 4 jam
# ==========================================
_shared_symbols: list = []
_sym_lock = threading.Lock()


def symbol_refresh_daemon(state: dict, stop_event: threading.Event):
    INTERVAL = 4 * 3600
    while not stop_event.is_set():
        now = time.time()
        if now - state.get('last_market_fetch', 0) > INTERVAL:
            syms = get_all_futures_symbols()
            if syms:
                with _sym_lock:
                    _shared_symbols.clear()
                    _shared_symbols.extend(syms)
                _register_streams(syms)
                state['last_market_fetch'] = now
                save_state(state)
                print("  🔄 Daftar simbol diperbarui.")
            else:
                print("  ⚠️  Gagal refresh simbol, coba 5 menit lagi.")
                time.sleep(300)
                continue
        for _ in range(60):
            if stop_event.is_set():
                return
            time.sleep(1)

# ==========================================
# 18. DAEMON — simpan state ke disk tiap 5 menit
# ==========================================
def state_save_daemon(state: dict, stop_event: threading.Event):
    while not stop_event.is_set():
        time.sleep(300)
        with _proc_lock:
            state['processed_signals'] = dict(_processed_signals)
        save_state(state)

# ==========================================
# 19. MAIN
# ==========================================
def main():
    global _processed_signals

    print("=" * 65)
    print("  🚀  BBMA OMA ALLY — BINANCE FUTURES  (WebSocket + REST)")
    print("=" * 65)
    print(f"  Data     : WebSocket realtime (REST hanya seed & fallback)")
    print(f"  TF       : {' · '.join(tf.upper() for tf in TIMEFRAMES)}")
    print(f"  Sinyal   : RE ENTRY · MMT · EXTREME  (BUY & SELL)")
    print(f"  Output   : {DATA_DIR}/  |  Chart: {CHART_DIR}/")
    print("=" * 65)

    # ── Load state ────────────────────────────────────────────
    state = load_state()
    with _proc_lock:
        _processed_signals.update(state.get('processed_signals', {}))

    # ── Ambil daftar simbol (REST 1x) ─────────────────────────
    symbols = get_all_futures_symbols()
    while not symbols:
        print("  Gagal ambil market. Retry 30s...")
        time.sleep(30)
        symbols = get_all_futures_symbols()

    with _sym_lock:
        _shared_symbols.extend(symbols)
    state['last_market_fetch'] = time.time()
    save_state(state)

    # ── Seed historis: disk → REST fallback ───────────────────
    # (WebSocket hanya kirim candle running, tidak punya data lama)
    rest_seed_all(symbols)

    # ── Register stream lookup ─────────────────────────────────
    _register_streams(symbols)

    # ── Scan sinyal terlewatkan (sebelum WebSocket live) ───────
    scan_missed_signals(symbols)

    send_telegram_text(
        f"🚀 <b>BBMA Bot AKTIF — WebSocket + REST Fallback</b>\n"
        f"Data: WebSocket realtime, REST hanya saat data kurang\n"
        f"Sinyal: RE ENTRY · MMT · EXTREME\n"
        f"TF: 1H · 4H · 1D · 1W — {len(symbols)} simbol\n"
        f"⏪ Missed signal scan: selesai, mode LIVE dimulai\n"
        f"Waktu: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    stop_event  = threading.Event()
    all_threads = []

    # ── WebSocket connections ──────────────────────────────────
    ws_pairs = launch_websocket_connections(symbols, stop_event)
    all_threads.extend([t for t, _ in ws_pairs])

    # ── Symbol refresh daemon ──────────────────────────────────
    t_sym = threading.Thread(target=symbol_refresh_daemon,
                             args=(state, stop_event),
                             name="SymRefresh", daemon=True)
    t_sym.start()
    all_threads.append(t_sym)

    # ── State save daemon ──────────────────────────────────────
    t_save = threading.Thread(target=state_save_daemon,
                              args=(state, stop_event),
                              name="StateSave", daemon=True)
    t_save.start()
    all_threads.append(t_save)

    print(f"\n  ✅ Bot aktif — {len(ws_pairs)} koneksi WebSocket")
    print("  Tekan Ctrl+C untuk berhenti.\n")

    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        print("\n⛔ Menghentikan bot...")
        stop_event.set()
        for _, conn in ws_pairs:
            conn.stop()
        for t in all_threads:
            t.join(timeout=5)
        with _proc_lock:
            state['processed_signals'] = dict(_processed_signals)
        save_state(state)
        send_telegram_text("⛔ <b>BBMA Bot dihentikan.</b>")
        print("✅ Bot berhenti.")


if __name__ == "__main__":
    main()
