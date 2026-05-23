"""
╔══════════════════════════════════════════════════════════════════╗
║      BBMA OMA ALLY — BINANCE FUTURES  (WebSocket + REST)        ║
║                                                                  ║
║  FILTER SIMBOL:                                                  ║
║  • Ambil semua /USDT perpetual dari Binance Futures             ║
║  • Rank berdasarkan Open Interest (USD) = proxy market cap      ║
║  • Hanya scan TOP 100 koin terbesar (configurable)             ║
║  • Refresh otomatis tiap 4 jam                                  ║
║                                                                  ║
║  ARSITEKTUR DATA (DUAL-SOURCE):                                  ║
║  1. Sumber 1: REST API  → seed historis + fallback + validasi   ║
║  2. Sumber 2: WebSocket → update candle realtime (close event)  ║
║  3. REST Poll tiap 5 menit → cross-check & tambal jika WS miss  ║
║  4. Jika WS gap → REST fallback otomatis untuk tambal data      ║
║                                                                  ║
║  PERBAIKAN BUG:                                                  ║
║  FIX-1: Race condition on_candle_close → pakai Event + queue    ║
║  FIX-2: add_indicators shift(1) salah arah → shift(-1) fixed    ║
║  FIX-3: compute_signals index -2/-3 tidak konsisten → fixed     ║
║  FIX-4: periodic_scan langsung mulai setelah seed selesai       ║
║  FIX-5: Dual-source: REST poll tiap 5 menit validasi WS data    ║
║  FIX-6: _processed_signals key pakai candle_time bukan ts str   ║
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
import queue
import threading
import warnings
import concurrent.futures
import numpy as np
import shutil
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

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN',   '8361349338:AAHOlx4fKz_bp1MHnVg8CxS9MY_pcejxLes')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003979979885')

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
MIN_VOLUME = 500_000     # minimal quoteVolume $500 ribu
TOP_N      = 100         # jumlah simbol teratas berdasarkan Open Interest

# ── Binance Futures REST endpoint (tanpa auth) ────────────────
BNFUT_BASE = "https://fapi.binance.com"

# ── REST (seed, fallback, dan poll) ──────────────────────────
REST_DELAY       = 0.3    # detik jeda antar request
REST_MAX_RETRY   = 5
BAN_WAIT_SEC     = 120
MAX_SEED_THREADS = 4      # thread paralel saat seed awal

# ── DUAL-SOURCE: REST Poll interval ──────────────────────────
# FIX-5: REST poll tiap 5 menit untuk cross-check data WS
REST_POLL_INTERVAL  = 300   # 5 menit
REST_POLL_THREADS   = 6     # thread paralel saat REST poll
REST_POLL_CANDLES   = 5     # ambil 5 candle terbaru per TF per poll

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

# ==========================================
# TERMINAL DISPLAY — Warna & Animasi VPS
# ==========================================
class C:
    """ANSI color codes — auto-disable jika terminal tidak support."""
    _on = sys.stdout.isatty() or os.environ.get('FORCE_COLOR', '0') == '1'
    RESET  = '\033[0m'    if _on else ''
    BOLD   = '\033[1m'    if _on else ''
    DIM    = '\033[2m'    if _on else ''
    WHITE  = '\033[97m'   if _on else ''
    CYAN   = '\033[96m'   if _on else ''
    GREEN  = '\033[92m'   if _on else ''
    YELLOW = '\033[93m'   if _on else ''
    RED    = '\033[91m'   if _on else ''
    BLUE   = '\033[94m'   if _on else ''
    MAGENTA= '\033[95m'   if _on else ''
    ORANGE = '\033[33m'   if _on else ''
    GRAY   = '\033[90m'   if _on else ''

def _tw() -> int:
    return shutil.get_terminal_size((80, 24)).columns

def _sep(char='═', color=C.CYAN) -> str:
    return color + char * min(_tw(), 68) + C.RESET

def _hdr(title: str, icon: str = ''):
    w = min(_tw(), 68)
    inner = f"  {icon}  {title}  " if icon else f"  {title}  "
    pad   = max(0, w - len(inner))
    print(_sep('═'))
    print(C.CYAN + C.BOLD + inner + ' ' * pad + C.RESET)
    print(_sep('═'))

def _ok(msg: str):
    print(f"  {C.GREEN}✅ {msg}{C.RESET}")

def _warn(msg: str):
    print(f"  {C.YELLOW}⚠️  {msg}{C.RESET}")

def _err(msg: str):
    print(f"  {C.RED}❌ {msg}{C.RESET}")

def _info(msg: str):
    print(f"  {C.CYAN}ℹ️  {msg}{C.RESET}")

def _dim(msg: str):
    print(f"  {C.GRAY}{msg}{C.RESET}")

def _signal_line(icon: str, label: str, sym: str, tf: str,
                 price: float, source: str = '', extra: str = ''):
    dir_col = C.GREEN if 'BUY' in label else C.RED
    ts = datetime.now().strftime('%H:%M:%S')
    src_tag = f" {C.BLUE}[{source}]{C.RESET}" if source else ''
    print(
        f"  {C.GRAY}[{ts}]{C.RESET} "
        f"{dir_col}{icon}{C.RESET} "
        f"{C.BOLD}{C.WHITE}{label:<14}{C.RESET} "
        f"{C.YELLOW}{sym:<22}{C.RESET} "
        f"{C.CYAN}{tf.upper():<3}{C.RESET}  "
        f"{C.GREEN}${price:.6g}{C.RESET}"
        f"{src_tag}"
        f"  {C.GRAY}{extra}{C.RESET}"
    )

def _progress(done: int, total: int, label: str = '', width: int = 30):
    pct    = done / total if total else 0
    filled = int(pct * width)
    bar    = C.GREEN + '█' * filled + C.GRAY + '░' * (width - filled) + C.RESET
    pct_s  = f"{C.YELLOW}{pct*100:5.1f}%{C.RESET}"
    cnt_s  = f"{C.WHITE}{done}/{total}{C.RESET}"
    lbl_s  = f" {C.GRAY}{label}{C.RESET}" if label else ''
    sys.stdout.write(f"\r  [{bar}] {pct_s}  {cnt_s}{lbl_s}   ")
    sys.stdout.flush()

def _progress_end():
    sys.stdout.write('\n')
    sys.stdout.flush()

def _spinner_msg(msg: str, done: bool = False):
    icon = f"{C.GREEN}✓{C.RESET}" if done else f"{C.YELLOW}…{C.RESET}"
    ts   = datetime.now().strftime('%H:%M:%S')
    print(f"  {C.GRAY}[{ts}]{C.RESET} {icon}  {msg}")

def _section(title: str, icon: str = '▶'):
    print()
    print(f"  {C.BOLD}{C.CYAN}{icon} {title}{C.RESET}")
    print(f"  {C.GRAY}{'─' * (min(_tw(), 64) - 2)}{C.RESET}")

# ── Missed signal scan saat startup ──────────────────────────
MISSED_LOOKBACK_SECONDS = 28800  # 8 jam
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
# 3. KONEKSI REST — seed, fallback, dan poll
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
            print(f"  {C.YELLOW}⏸️  REST ban aktif, tunggu {ban_wait:.0f}s...{C.RESET}")
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
# 5. AMBIL TOP-N SIMBOL BERDASARKAN OPEN INTEREST BINANCE FUTURES
# ==========================================
def _fetch_binance_open_interest() -> dict:
    """
    Ambil Open Interest semua simbol USDT perpetual dari Binance Futures.
    Return: (oi_map, vol_map, usdt_perp)
    """
    try:
        r = requests.get(f"{BNFUT_BASE}/fapi/v1/exchangeInfo", timeout=15)
        r.raise_for_status()
        info = r.json()
    except Exception as e:
        print(f"  [OI] exchangeInfo gagal: {e}")
        return {}, {}, []

    usdt_perp = [
        s['symbol'] for s in info.get('symbols', [])
        if s.get('quoteAsset') == 'USDT'
        and s.get('contractType') == 'PERPETUAL'
        and s.get('status') == 'TRADING'
        and not any(x in s['symbol'] for x in ['UP', 'DOWN', 'BEAR', 'BULL'])
    ]

    try:
        r2 = requests.get(f"{BNFUT_BASE}/fapi/v1/ticker/24hr", timeout=15)
        r2.raise_for_status()
        tickers_raw = r2.json()
    except Exception as e:
        print(f"  [OI] ticker/24hr gagal: {e}")
        return {}, {}, []

    price_map = {}
    vol_map   = {}
    for t in tickers_raw:
        sym = t.get('symbol', '')
        if sym in usdt_perp:
            try:
                price_map[sym] = float(t.get('lastPrice', 0))
                vol_map[sym]   = float(t.get('quoteVolume', 0))
            except Exception:
                pass

    oi_map = {}

    def _fetch_oi(sym):
        try:
            r = requests.get(
                f"{BNFUT_BASE}/fapi/v1/openInterest",
                params={'symbol': sym},
                timeout=8
            )
            if r.status_code == 200:
                data = r.json()
                oi_qty  = float(data.get('openInterest', 0))
                price   = price_map.get(sym, 0)
                return sym, oi_qty * price
        except Exception:
            pass
        return sym, 0.0

    _section("Mengambil Open Interest semua simbol USDT perpetual", "📊")
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futures_oi = {ex.submit(_fetch_oi, s): s for s in usdt_perp}
        for fut in concurrent.futures.as_completed(futures_oi):
            sym, oi_usd = fut.result()
            oi_map[sym] = oi_usd

    return oi_map, vol_map, usdt_perp


def get_all_futures_symbols() -> list:
    _section(f"Menyusun Top-{TOP_N} Binance Futures berdasarkan Open Interest", "📡")

    result = _fetch_binance_open_interest()
    if not result or not result[0]:
        print("  ⚠️  Gagal ambil OI, fallback ke fetch_tickers CCXT...")
        return _fallback_get_symbols()

    oi_map, vol_map, usdt_perp = result

    ranked = []
    for sym in usdt_perp:
        oi_usd  = oi_map.get(sym, 0)
        vol_usd = vol_map.get(sym, 0)
        if vol_usd < MIN_VOLUME:
            continue
        base = sym.replace('USDT', '')
        ccxt_sym = f"{base}/USDT:USDT"
        ranked.append({
            'symbol':     ccxt_sym,
            'raw_symbol': sym,
            'oi_usd':     oi_usd,
            'volume':     vol_usd,
            'change':     0.0,
        })

    ranked.sort(key=lambda x: x['oi_usd'], reverse=True)
    top = ranked[:TOP_N]

    _section(f"TOP-{TOP_N} BINANCE FUTURES — Open Interest (USD)", "🏆")
    hdr = (f"  {C.BOLD}{C.GRAY}{'#':>3}  {'Simbol':<18} "
           f"{'Open Interest (USD)':>22}  {'Volume 24h':>18}{C.RESET}")
    print(hdr)
    print(f"  {C.GRAY}{'─'*3}  {'─'*18} {'─'*22}  {'─'*18}{C.RESET}")
    for i, c in enumerate(top, 1):
        oi_str  = f"${c['oi_usd']:>20,.0f}"
        vol_str = f"${c['volume']:>16,.0f}"
        num_col = C.YELLOW if i <= 3 else C.GRAY
        print(
            f"  {num_col}{i:>3}.{C.RESET} "
            f"{C.WHITE}{c['symbol']:<18}{C.RESET} "
            f"{C.GREEN}{oi_str}{C.RESET}  "
            f"{C.CYAN}{vol_str}{C.RESET}"
        )

    print()
    _ok(f"{len(top)} simbol aktif (dari {len(ranked)} total, top-{TOP_N} by OI)")
    return top


def _fallback_get_symbols() -> list:
    try:
        _rest_call(exchange.load_markets)
    except Exception as e:
        print(f"  [load_markets Error] {e}")
        return []

    tickers = _rest_call(exchange.fetch_tickers)
    if not tickers:
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
            'oi_usd': vol,
        })

    valid.sort(key=lambda x: x['volume'], reverse=True)
    top = valid[:TOP_N]
    print(f"  ✅ [Fallback] {len(top)} simbol teratas (by volume)")
    return top

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
                        lo: float, c: float, v: float) -> bool:
    """
    Update candle di store dari data WebSocket.
    FIX-1: Menggunakan lock yang benar, tidak ada race condition.
    Return True jika update berhasil.
    """
    ts  = pd.Timestamp(ts_ms, unit='ms')
    row = {'timestamp': ts, 'open': o, 'high': h, 'low': lo,
           'close': c, 'volume': v}

    df_to_save = None
    updated = False
    with _store_lock:
        sym_data = _ohlcv_store.get(symbol, {})
        df = sym_data.get(tf)
        if df is None or df.empty:
            return False

        last_ts = df.iloc[-1]['timestamp']
        if ts == last_ts:
            df = df.copy()
            df.iloc[-1] = row
            updated = True
        elif ts > last_ts:
            new_row = pd.DataFrame([row])
            df = pd.concat([df, new_row], ignore_index=True)
            max_rows = TF_SEED_LIMIT.get(tf, 200) + 50
            if len(df) > max_rows:
                df = df.iloc[-max_rows:].reset_index(drop=True)
            updated = True
        else:
            return False

        _ohlcv_store.setdefault(symbol, {})[tf] = df
        df_to_save = df.copy()

    if df_to_save is not None:
        save_ohlcv(symbol, tf, df_to_save)
    return updated


def _is_data_complete(df: 'pd.DataFrame | None', tf: str) -> bool:
    if df is None:
        return False
    return len(df) >= TF_MIN_ROWS.get(tf, 30)

# ==========================================
# 8. REST SEED — ambil historis saat pertama start
# ==========================================
def rest_seed_one(symbol: str, tf: str) -> bool:
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
    1. Coba load dari disk dulu
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

    _section(
        f"Seed historis {len(symbols)} simbol × {len(TIMEFRAMES)} TF "
        f"({MAX_SEED_THREADS} thread paralel)",
        "🌱"
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_SEED_THREADS) as ex:
        futs = {ex.submit(_seed_one, coin, tf): (coin['symbol'], tf)
                for coin in symbols for tf in TIMEFRAMES}
        for f in concurrent.futures.as_completed(futs):
            done += 1
            src = f.result()
            if src != 'fail':
                seeded += 1
            sym_name, tf_name = futs[f]
            src_tag = (f"{C.CYAN}[disk]{C.RESET}" if src == 'disk'
                       else f"{C.YELLOW}[REST]{C.RESET}" if src == 'rest'
                       else f"{C.RED}[FAIL]{C.RESET}")
            _progress(done, total,
                      label=f"{src_tag} {C.WHITE}{sym_name.split('/')[0]:<6}{C.RESET} {C.GRAY}{tf_name}{C.RESET}")

    _progress_end()
    _ok(f"Seed selesai — {C.GREEN}{seeded}{C.RESET}/{total} berhasil  "
        f"({C.RED}{done - seeded} gagal{C.RESET})")

# ==========================================
# 9. REST FALLBACK — tambal gap setelah WS disconnect
# ==========================================
def rest_fill_gap(symbol: str, tf: str):
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
# 9b. DUAL-SOURCE: REST POLL — sumber data ke-2
#     Ambil data fresh dari REST tiap REST_POLL_INTERVAL detik
#     untuk cross-check dan validasi data dari WebSocket.
# ==========================================
def rest_poll_one(symbol: str, tf: str) -> 'pd.DataFrame | None':
    """
    Ambil N candle terbaru dari REST API untuk satu simbol/TF.
    Merge dengan data yang ada di store, return DataFrame yang dimerge.
    FIX-5: Ini adalah sumber data ke-2 (REST poll) selain WebSocket.
    """
    try:
        bars = _rest_call(
            exchange.fetch_ohlcv, symbol, tf,
            limit=REST_POLL_CANDLES
        )
        if not bars:
            return None

        df_new = pd.DataFrame(bars,
                              columns=['timestamp', 'open', 'high',
                                       'low', 'close', 'volume'])
        df_new['timestamp'] = pd.to_datetime(df_new['timestamp'], unit='ms')

        df_existing = store_get(symbol, tf)
        if df_existing is None or df_existing.empty:
            return None

        # Merge: REST data menang untuk candle yang sama (lebih akurat)
        df_merged = pd.concat([df_existing, df_new], ignore_index=True)
        df_merged.drop_duplicates(subset='timestamp', keep='last', inplace=True)
        df_merged.sort_values('timestamp', inplace=True)
        df_merged.reset_index(drop=True, inplace=True)

        max_rows = TF_SEED_LIMIT.get(tf, 200) + 50
        if len(df_merged) > max_rows:
            df_merged = df_merged.iloc[-max_rows:].reset_index(drop=True)

        store_set(symbol, tf, df_merged)
        return df_merged

    except Exception as e:
        # Jangan print error untuk menghindari flood log
        return None


def rest_poll_daemon(stop_event: threading.Event):
    """
    FIX-5: Daemon REST poll — sumber data ke-2 selain WebSocket.
    Setiap REST_POLL_INTERVAL detik, ambil candle terbaru via REST
    untuk semua simbol × TF, merge ke store, lalu scan sinyal.
    
    Ini memastikan sinyal tidak terlewat meski:
    - WebSocket terlambat kirim event closed=True
    - TF panjang (1d/1w) yang candle close-nya jarang
    - Ada gap atau error di WebSocket
    """
    # Tunggu sampai seed selesai
    time.sleep(30)

    _info(f"REST Poll daemon aktif — interval: {REST_POLL_INTERVAL}s, "
          f"ambil {REST_POLL_CANDLES} candle/TF")

    while not stop_event.is_set():
        start_t = time.time()

        with _sym_lock:
            coins = list(_shared_symbols)

        if not coins:
            time.sleep(30)
            continue

        ts_str = datetime.now().strftime('%H:%M:%S')
        print(f"\n  {C.GRAY}[{ts_str}]{C.RESET} "
              f"{C.BLUE}📡 REST Poll: {len(coins)} simbol × {len(TIMEFRAMES)} TF...{C.RESET}")

        total_signals = 0

        def _poll_and_scan(coin):
            """Poll REST + scan sinyal untuk satu koin."""
            sym    = coin['symbol']
            change = coin.get('change', 0.0)
            found  = 0

            for tf in TIMEFRAMES:
                # Ambil data fresh dari REST (sumber ke-2)
                df_merged = rest_poll_one(sym, tf)
                if df_merged is None:
                    # Fallback ke data store
                    df_merged = store_get(sym, tf)

                if not _is_data_complete(df_merged, tf):
                    continue

                df      = add_indicators(df_merged)
                signals = compute_signals(df)
                if not signals:
                    continue

                mtf = get_mtf_bias(sym)

                for sig_name, sig_data in signals.items():
                    sig_key = f"{sym}_{sig_name}_{tf}"
                    with _proc_lock:
                        if _processed_signals.get(sig_key) == sig_data['time']:
                            continue
                        _processed_signals[sig_key] = sig_data['time']

                    label = SIGNAL_LABEL.get(sig_name, sig_name)
                    icon  = '🟢' if sig_data['tipe'] == 'BUY' else '🔴'
                    _signal_line(icon, f"{label} {sig_data['tipe']}",
                                 sym, tf, sig_data['price'], source='REST')

                    img = generate_chart(df, sym, sig_name, tf)
                    send_telegram_alert(
                        symbol=sym, signal_name=sig_name, timeframe=tf,
                        data=sig_data, change_24h=change,
                        mtf=mtf, image_path=img, source='REST Poll'
                    )
                    found += 1

            return found

        with concurrent.futures.ThreadPoolExecutor(max_workers=REST_POLL_THREADS) as ex:
            futs = [ex.submit(_poll_and_scan, c) for c in coins]
            for f in concurrent.futures.as_completed(futs):
                try:
                    total_signals += f.result()
                except Exception as e:
                    print(f"  [REST Poll Error] {e}")

        elapsed = time.time() - start_t
        ts_str2 = datetime.now().strftime('%H:%M:%S')
        print(f"  {C.GRAY}[{ts_str2}]{C.RESET} "
              f"{C.BLUE}✓ REST Poll selesai{C.RESET} — "
              f"{C.YELLOW}{total_signals} sinyal baru{C.RESET} — "
              f"durasi {elapsed:.1f}s")

        # Tunggu sisa interval
        wait = max(REST_POLL_INTERVAL - elapsed, 30)
        for _ in range(int(wait)):
            if stop_event.is_set():
                return
            time.sleep(1)

# ==========================================
# 10. INDIKATOR BBMA
# ==========================================
def _wma(series: pd.Series, length: int) -> pd.Series:
    weights = np.arange(1, length + 1, dtype=float)
    return series.rolling(length).apply(
        lambda x: np.dot(x, weights) / weights.sum(), raw=True
    )


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    FIX-2: Perbaiki shift direction untuk _p (previous) columns.
    shift(1) pada pandas = geser ke bawah = nilai row sebelumnya masuk ke row sekarang.
    Ini BENAR untuk indikator yang butuh "nilai candle sebelumnya" di candle sekarang.
    Tapi nama variabel _p (previous) bisa membingungkan.
    
    Verifikasi: df['topBB'].shift(1).iloc[i] = df['topBB'].iloc[i-1] ← ini benar
    """
    df = df.copy()
    df['midBB']   = df['close'].rolling(20).mean()
    df['BBdev']   = 2.0 * df['close'].rolling(20).std(ddof=0)
    # topBB dan lowBB adalah nilai BB pada candle SAAT INI (bukan shift)
    df['topBB']   = df['midBB'] + df['BBdev']
    df['lowBB']   = df['midBB'] - df['BBdev']
    df['mahi5']   = _wma(df['high'], 5)
    df['mahi10']  = _wma(df['high'], 10)
    df['malo5']   = _wma(df['low'],  5)
    df['malo10']  = _wma(df['low'],  10)
    # _p = nilai indikator pada candle SEBELUMNYA (shift(1) = geser 1 ke bawah = benar)
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
    """
    FIX-3: Perbaiki indexing sinyal.
    df.iloc[-1] = candle running (belum close) → SKIP
    df.iloc[-2] = candle closed terbaru → gunakan sebagai 'c' (current closed)
    df.iloc[-3] = candle sebelum closed → gunakan sebagai 'prev'
    
    Syarat: minimal 30 baris + 3 baris untuk c, prev, dan sebelumnya.
    """
    if df is None or len(df) < 32:  # butuh minimal 32 agar indikator valid
        return {}

    c    = df.iloc[-2]    # candle closed terbaru
    prev = df.iloc[-3]    # candle sebelumnya

    # Validasi: pastikan nilai indikator tidak NaN
    required_cols = ['mahi5', 'mahi10', 'malo5', 'malo10', 'midBB',
                     'topBB', 'lowBB', 'mahi5_p', 'malo5_p', 'topBB_p', 'lowBB_p']
    for col in required_cols:
        if pd.isna(c.get(col)) or pd.isna(prev.get(col)):
            return {}  # data belum cukup untuk hitung indikator

    csz_c    = abs(c['close']    - c['open'])
    csz_prev = abs(prev['close'] - prev['open'])
    signals  = {}

    # RE ENTRY SELL: harga ditolak dari MAHI5, dalam zona bearish
    if (c['high'] > c['mahi5']
            and c['close'] < c['mahi5']
            and c['close'] < c['mahi10']
            and c['close'] < c['midBB']
            and c['mahi5'] < c['midBB']):
        signals['REENTRY SELL'] = {
            'tipe': 'SELL',
            'explanation': 'Harga ditolak dari MAHI5 — potensi turun lanjut.',
        }

    # RE ENTRY BUY: harga ditolak dari MALO5, dalam zona bullish
    if (c['low'] < c['malo5']
            and c['close'] > c['malo5']
            and c['close'] > c['malo10']
            and c['close'] > c['midBB']
            and c['malo5'] > c['midBB']):
        signals['REENTRY BUY'] = {
            'tipe': 'BUY',
            'explanation': 'Harga ditolak dari MALO5 — potensi naik lanjut.',
        }

    # MMT SELL: close menembus ke bawah LowBB (CSM Sell)
    if c['close'] < c['lowBB'] and c['open'] > c['lowBB']:
        signals['MMT SELL'] = {
            'tipe': 'SELL',
            'explanation': 'Momentum — close menembus ke bawah LowBB (CSM Sell).',
        }

    # MMT BUY: close menembus ke atas TopBB (CSM Buy)
    if c['close'] > c['topBB'] and c['open'] < c['topBB']:
        signals['MMT BUY'] = {
            'tipe': 'BUY',
            'explanation': 'Momentum — close menembus ke atas TopBB (CSM Buy).',
        }

    # EXTREME SELL: engulfing bearish setelah MAHI5 di atas TopBB
    if (prev['close'] > prev['open']
            and c['close'] < c['topBB']
            and c['close'] < c['open']
            and (c['mahi5_p'] > c['topBB_p'] or c['mahi5'] > c['topBB'])
            and csz_c > csz_prev / 2):
        signals['EXTREME SELL'] = {
            'tipe': 'SELL',
            'explanation': 'Engulfing bearish — MAHI5 di atas TopBB, reversal turun.',
        }

    # EXTREME BUY: engulfing bullish setelah MALO5 di bawah LowBB
    if (prev['close'] < prev['open']
            and c['close'] > c['lowBB']
            and c['close'] > c['open']
            and (c['malo5_p'] < c['lowBB_p'] or c['malo5'] < c['lowBB'])
            and csz_c > csz_prev / 2):
        signals['EXTREME BUY'] = {
            'tipe': 'BUY',
            'explanation': 'Engulfing bullish — MALO5 di bawah LowBB, reversal naik.',
        }

    # FIX-6: Gunakan timestamp candle (bukan str(now)) sebagai dedup key
    for k in signals:
        signals[k]['price'] = float(c['close'])
        # Gunakan timestamp candle yang sebenarnya untuk dedup yang akurat
        signals[k]['time']  = str(c['timestamp'])

    return {k: v for k, v in signals.items() if k in ALLOWED_SIGNALS}


def compute_signals_at(df: pd.DataFrame, idx: int) -> dict:
    """
    FIX-3: Hitung sinyal pada candle indeks `idx` dari belakang.
    idx=0 → candle[-2] (closed terbaru)
    idx=1 → candle[-3], dst.
    """
    if df is None or len(df) < idx + 4:
        return {}

    c    = df.iloc[-(idx + 2)]
    prev = df.iloc[-(idx + 3)]

    required_cols = ['mahi5', 'mahi10', 'malo5', 'malo10', 'midBB',
                     'topBB', 'lowBB', 'mahi5_p', 'malo5_p', 'topBB_p', 'lowBB_p']
    for col in required_cols:
        if pd.isna(c.get(col)) or pd.isna(prev.get(col)):
            return {}

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

        # Validasi NaN
        if pd.isna(c.get('ema50')) or pd.isna(c.get('midBB')):
            bias[tf] = 'NEUTRAL'
            continue

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


def generate_status_chart(symbol: str = 'BTC/USDT:USDT',
                          tf: str = '1h',
                          label: str = 'STATUS') -> 'str | None':
    _candidates = [
        ('BTC/USDT:USDT', '1h'),
        ('ETH/USDT:USDT', '1h'),
        ('BTC/USDT:USDT', '4h'),
        ('ETH/USDT:USDT', '4h'),
    ]

    df_use  = None
    sym_use = symbol
    tf_use  = tf

    for _sym, _tf in _candidates:
        _df = store_get(_sym, _tf)
        if _is_data_complete(_df, _tf):
            _df = add_indicators(_df)
            df_use  = _df
            sym_use = _sym
            tf_use  = _tf
            break

    if df_use is None:
        with _store_lock:
            for _sym, _tfs in _ohlcv_store.items():
                for _tf, _df in _tfs.items():
                    if _is_data_complete(_df, _tf):
                        _df2 = add_indicators(_df.copy())
                        df_use  = _df2
                        sym_use = _sym
                        tf_use  = _tf
                        break
                if df_use is not None:
                    break

    if df_use is None:
        return None

    try:
        safe_sym = sym_use.replace('/', '-').replace(':', '-')
        safe_lbl = label.replace(' ', '_').upper()
        filename = str(CHART_DIR / f"_status_{safe_sym}_{tf_use}_{safe_lbl}.png")

        tail    = 80 if tf_use in ('1d', '1w') else 100
        plot_df = df_use.tail(tail).copy()
        plot_df.set_index('timestamp', inplace=True)

        style = mpf.make_mpf_style(
            base_mpf_style='nightclouds', rc={'font.size': 8}
        )
        adds = [
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
            title=f"{sym_use} [{tf_use.upper()}] — {label}",
            savefig=dict(fname=filename, bbox_inches='tight'),
            volume=True,
        )
        return filename
    except Exception as e:
        print(f"  [Status Chart Error] {e}")
        return None


# ==========================================
# 14. TELEGRAM
# ==========================================
_tg_lock = threading.Lock()


def send_telegram_photo(text: str, image_path: 'str | None' = None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    with _tg_lock:
        try:
            if image_path and os.path.exists(image_path):
                with open(image_path, 'rb') as img:
                    requests.post(
                        f"{base}/sendPhoto",
                        data={
                            'chat_id':    TELEGRAM_CHAT_ID,
                            'caption':    text,
                            'parse_mode': 'HTML',
                        },
                        files={'photo': img},
                        timeout=30,
                    )
            else:
                requests.post(
                    f"{base}/sendMessage",
                    data={
                        'chat_id':    TELEGRAM_CHAT_ID,
                        'text':       text,
                        'parse_mode': 'HTML',
                    },
                    timeout=15,
                )
        except Exception as e:
            print(f"  [TG Photo Error] {e}")


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
                        mtf: dict, image_path=None, source: str = 'WS'):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    icon    = SIGNAL_ICON.get(data['tipe'], '⚪')
    label   = SIGNAL_LABEL.get(signal_name, signal_name)
    tf_em   = TF_EMOJI.get(timeframe, '')
    mtf_blk = _format_mtf_block(mtf)
    # Tampilkan sumber data di notifikasi
    src_tag = f"📡 REST Poll" if source == 'REST Poll' else "📶 WebSocket"

    caption = (
        f"{icon} <b>BBMA FUTURES — {label} {data['tipe']}</b>\n"
        f"──────────────────────\n"
        f"💎 <b>Symbol  :</b> {symbol}\n"
        f"🏷 <b>Sinyal  :</b> {signal_name}\n"
        f"{tf_em} <b>TF      :</b> {timeframe.upper()}\n"
        f"💰 <b>Harga   :</b> {data['price']:.6g}\n"
        f"📈 <b>24h Chg :</b> {change_24h:+.2f}%\n"
        f"🔗 <b>Sumber  :</b> {src_tag}\n"
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

# FIX-1: Gunakan queue untuk memproses sinyal dari WS secara serial
# Hindari multiple thread masing2 baca store pada waktu bersamaan
_signal_queue: queue.Queue = queue.Queue(maxsize=1000)


def on_candle_close(symbol: str, tf: str, change_24h: float = 0.0):
    """
    FIX-1: Masukkan ke queue, bukan langsung proses.
    Queue processor membaca satu per satu untuk menghindari race condition.
    """
    _signal_queue.put_nowait((symbol, tf, change_24h))


def signal_queue_processor(stop_event: threading.Event):
    """
    FIX-1: Thread tunggal yang memproses signal queue dari WS.
    Satu thread = tidak ada race condition saat baca store.
    """
    while not stop_event.is_set():
        try:
            item = _signal_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        symbol, tf, change_24h = item

        try:
            _process_candle_close(symbol, tf, change_24h, source='WS')
        except Exception as e:
            print(f"  [SignalQueue Error] {symbol} {tf}: {e}")
        finally:
            _signal_queue.task_done()


def _process_candle_close(symbol: str, tf: str,
                           change_24h: float = 0.0,
                           source: str = 'WS'):
    """
    FIX-1, FIX-3: Core signal processing — dipanggil dari queue processor.
    Tidak ada race condition karena hanya dipanggil oleh satu thread.
    """
    df = store_get(symbol, tf)

    if not _is_data_complete(df, tf):
        print(f"  ⚠️  [{tf.upper()}] {symbol} data kurang "
              f"({len(df) if df is not None else 0} baris) → REST fallback")
        rest_fill_gap(symbol, tf)
        df = store_get(symbol, tf)
        if not _is_data_complete(df, tf):
            return

    df = add_indicators(df)
    signals = compute_signals(df)
    if not signals:
        return

    mtf = get_mtf_bias(symbol)

    for sig_name, sig_data in signals.items():
        sig_key = f"{symbol}_{sig_name}_{tf}"
        with _proc_lock:
            if _processed_signals.get(sig_key) == sig_data['time']:
                continue
            _processed_signals[sig_key] = sig_data['time']

        label = SIGNAL_LABEL.get(sig_name, sig_name)
        icon  = '🟢' if sig_data['tipe'] == 'BUY' else '🔴'
        _signal_line(icon, f"{label} {sig_data['tipe']}",
                     symbol, tf, sig_data['price'], source=source)

        img = generate_chart(df, symbol, sig_name, tf)
        send_telegram_alert(
            symbol=symbol, signal_name=sig_name, timeframe=tf,
            data=sig_data, change_24h=change_24h,
            mtf=mtf, image_path=img, source=source
        )

# ==========================================
# 15b. SCAN SINYAL TERLEWATKAN — dijalankan sekali saat startup
# ==========================================
def scan_missed_signals(symbols: list):
    global _processed_signals

    now_ts     = pd.Timestamp.now(tz='UTC').tz_localize(None)
    cutoff_ts  = now_ts - pd.Timedelta(seconds=MISSED_LOOKBACK_SECONDS)

    print()
    _sep_line = _sep('═')
    print(_sep_line)
    print(f"  {C.BOLD}{C.MAGENTA}⏪  SCANNING SINYAL 8 JAM TERAKHIR (Missed Signal Scan)...{C.RESET}")
    print(f"  {C.GRAY}Rentang  : {cutoff_ts.strftime('%Y-%m-%d %H:%M')} → sekarang{C.RESET}")
    print(_sep_line)

    total_missed = 0

    for coin in symbols:
        sym    = coin['symbol']
        change = coin.get('change', 0.0)

        for tf in TIMEFRAMES:
            df_raw = store_get(sym, tf)
            if not _is_data_complete(df_raw, tf):
                continue

            df = add_indicators(df_raw)

            dur_sec   = TF_DURATION_SEC[tf]
            max_back  = max(int(MISSED_LOOKBACK_SECONDS / dur_sec) + 1, 1)

            mtf = None

            for idx in range(max_back - 1, -1, -1):
                row_pos = -(idx + 2)
                if abs(row_pos) > len(df):
                    continue

                candle_ts = df.iloc[row_pos]['timestamp']
                if hasattr(candle_ts, 'tzinfo') and candle_ts.tzinfo is not None:
                    candle_ts = candle_ts.tz_localize(None)

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
                            continue
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

                    icon_ms = '🟢' if sig_data['tipe'] == 'BUY' else '🔴'
                    _signal_line(
                        icon_ms,
                        f"⏪ {SIGNAL_LABEL.get(sig_name, sig_name)} {sig_data['tipe']}",
                        sym, tf, sig_data['price'],
                        extra=f"candle: {candle_time}"
                    )

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
                    time.sleep(MISSED_SIGNAL_DELAY)

    if total_missed:
        print()
        _ok(f"Missed scan selesai — {C.YELLOW}{total_missed}{C.RESET} sinyal dikirim ke Telegram.")
        send_telegram_text(
            f"⏪ <b>Missed Signal Scan Selesai</b>\n"
            f"Sinyal terlewat (8 jam terakhir): <b>{total_missed}</b>\n"
            f"Bot kini masuk mode LIVE (WebSocket + REST Poll)."
        )
    else:
        _ok("Missed scan selesai — tidak ada sinyal dalam 8 jam terakhir.")

# ==========================================
# 15c. PERIODIC SCAN — scan sinyal tiap 10 menit (backup WS)
# ==========================================
PERIODIC_SCAN_INTERVAL = 600   # 10 menit
PERIODIC_SCAN_THREADS  = 8


def _periodic_scan_one(coin: dict):
    sym    = coin['symbol']
    change = coin.get('change', 0.0)
    found  = 0

    for tf in TIMEFRAMES:
        df_raw = store_get(sym, tf)
        if not _is_data_complete(df_raw, tf):
            continue

        df      = add_indicators(df_raw)
        signals = compute_signals(df)
        if not signals:
            continue

        mtf = get_mtf_bias(sym)

        for sig_name, sig_data in signals.items():
            sig_key = f"{sym}_{sig_name}_{tf}"
            with _proc_lock:
                if _processed_signals.get(sig_key) == sig_data['time']:
                    continue
                _processed_signals[sig_key] = sig_data['time']

            label = SIGNAL_LABEL.get(sig_name, sig_name)
            icon  = '🟢' if sig_data['tipe'] == 'BUY' else '🔴'
            _signal_line(icon, f"{label} {sig_data['tipe']}",
                         sym, tf, sig_data['price'], source='SCAN')

            img = generate_chart(df, sym, sig_name, tf)
            send_telegram_alert(
                symbol=sym, signal_name=sig_name, timeframe=tf,
                data=sig_data, change_24h=change,
                mtf=mtf, image_path=img, source='Periodic Scan'
            )
            found += 1

    return found


def periodic_scan_daemon(stop_event: threading.Event):
    """
    FIX-4: Mulai scan segera setelah seed selesai (bukan tunggu 60 detik).
    """
    # Tunggu singkat agar WS terkoneksi
    time.sleep(15)

    while not stop_event.is_set():
        start_t = time.time()

        with _sym_lock:
            coins = list(_shared_symbols)

        if not coins:
            time.sleep(30)
            continue

        ts_str = datetime.now().strftime('%H:%M:%S')
        print(f"\n  {C.GRAY}[{ts_str}]{C.RESET} "
              f"{C.CYAN}🔍 Periodic scan {len(coins)} koin...{C.RESET}")

        total_found = 0
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=PERIODIC_SCAN_THREADS) as ex:
            futs = [ex.submit(_periodic_scan_one, c) for c in coins]
            for f in concurrent.futures.as_completed(futs):
                try:
                    total_found += f.result()
                except Exception as e:
                    print(f"  [PeriodicScan Error] {e}")

        elapsed = time.time() - start_t
        ts_str2 = datetime.now().strftime('%H:%M:%S')
        print(f"  {C.GRAY}[{ts_str2}]{C.RESET} "
              f"{C.GREEN}✓ Scan selesai{C.RESET} — "
              f"{C.YELLOW}{total_found} sinyal baru{C.RESET} — "
              f"durasi {elapsed:.1f}s")

        wait = max(PERIODIC_SCAN_INTERVAL - elapsed, 30)
        for _ in range(int(wait)):
            if stop_event.is_set():
                return
            time.sleep(1)

# ==========================================
# 16. WEBSOCKET — sumber data realtime
# ==========================================
_stream_map      : dict = {}
_stream_map_lock = threading.Lock()


def _symbol_to_ws(symbol: str) -> str:
    base = symbol.split('/')[0]
    return (base + 'usdt').lower()


def _build_stream_name(symbol: str, tf: str) -> str:
    return f"{_symbol_to_ws(symbol)}@kline_{TF_WS_INTERVAL[tf]}"


def _register_streams(symbols: list):
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
    FIX-1: candle close dimasukkan ke queue, bukan langsung diproses.
    """

    def __init__(self, stream_names: list, conn_id: int,
                 stop_event: threading.Event):
        self.stream_names = stream_names
        self.conn_id      = conn_id
        self.stop_event   = stop_event
        self._ws          = None

    def _on_message(self, ws, raw):
        try:
            msg  = json.loads(raw)
            data = msg.get('data', msg)
            if data.get('e') != 'kline':
                return

            k        = data['k']
            ws_sym   = data['s'].lower()
            interval = k['i']
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
            closed = bool(k['x'])

            # Update store (candle running atau baru)
            store_update_candle(symbol, tf, ts_ms, o, h, lo, c, v)

            # FIX-1: Saat candle close, masukkan ke queue (bukan thread baru)
            if closed:
                try:
                    _signal_queue.put_nowait((symbol, tf, change_24h))
                except queue.Full:
                    # Queue penuh, skip — periodic scan akan catch ini
                    pass

        except Exception as e:
            print(f"  [WS-{self.conn_id} msg] {e}")

    def _on_error(self, ws, error):
        print(f"  [WS-{self.conn_id}] Error: {error}")

    def _on_close(self, ws, code, msg):
        if not self.stop_event.is_set():
            print(f"  [WS-{self.conn_id}] Terputus ({code}) — reconnect...")

    def _on_open(self, ws):
        _spinner_msg(
            f"{C.WHITE}WS-{self.conn_id}{C.RESET} terhubung — "
            f"{C.YELLOW}{len(self.stream_names)}{C.RESET} stream aktif",
            done=True
        )

    def _fill_gaps_after_reconnect(self):
        seen = set()
        with _stream_map_lock:
            for sname in self.stream_names:
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
    all_streams = []
    for coin in symbols:
        sym = coin['symbol']
        for tf in TIMEFRAMES:
            all_streams.append(_build_stream_name(sym, tf))

    batches = [
        all_streams[i:i + WS_MAX_STREAMS]
        for i in range(0, len(all_streams), WS_MAX_STREAMS)
    ]

    _section(
        f"WebSocket: {len(all_streams)} stream → {len(batches)} koneksi "
        f"(maks {WS_MAX_STREAMS}/koneksi)",
        "📡"
    )

    result = []
    for idx, batch in enumerate(batches):
        conn = KlineWsConnection(batch, conn_id=idx + 1,
                                 stop_event=stop_event)
        t = threading.Thread(target=conn.run,
                             name=f"WS-{idx+1}", daemon=True)
        t.start()
        result.append((t, conn))
        time.sleep(0.3)

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
# 18b. DAEMON — heartbeat Telegram tiap 1 jam
#      Kirim status "Bot masih LIVE" ke Telegram agar
#      operator tahu bot masih berjalan normal.
# ==========================================
HEARTBEAT_INTERVAL = 3600   # detik — kirim status tiap 1 jam

def heartbeat_daemon(start_time: datetime, ws_pairs: list,
                     symbols: list, stop_event: threading.Event):
    """
    Kirim pesan heartbeat ke Telegram tiap HEARTBEAT_INTERVAL detik.
    Tujuan: operator tahu bot masih aktif tanpa perlu cek manual.
    """
    while not stop_event.is_set():
        # Tunggu interval (pecah kecil agar bisa detect stop_event cepat)
        for _ in range(HEARTBEAT_INTERVAL):
            if stop_event.is_set():
                return
            time.sleep(1)

        now        = datetime.now()
        uptime_sec = int((now - start_time).total_seconds())
        uptime_str = (
            f"{uptime_sec // 3600}j "
            f"{(uptime_sec % 3600) // 60}m "
            f"{uptime_sec % 60}d"
        )
        with _proc_lock:
            total_sigs = len(_processed_signals)

        send_telegram_photo(
            f"💚 <b>BBMA Bot — MASIH AKTIF (Heartbeat)</b>\n"
            f"──────────────────────\n"
            f"🟢 Status      : Bot berjalan normal\n"
            f"⏱ Uptime       : <b>{uptime_str}</b>\n"
            f"🕐 Waktu check : {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"──────────────────────\n"
            f"💎 Simbol pantau: {len(symbols)} koin\n"
            f"📡 WebSocket   : {len(ws_pairs)} koneksi aktif\n"
            f"🧵 Threads     : {threading.active_count()} berjalan\n"
            f"📊 Total sinyal: {total_sigs} sinyal tercatat\n"
            f"──────────────────────\n"
            f"<i>⏰ Update berikutnya dalam {HEARTBEAT_INTERVAL // 60} menit</i>"
        )
        _info(f"Heartbeat Telegram terkirim — uptime {uptime_str}")

# ==========================================
# 19. MAIN
# ==========================================
def main():
    global _processed_signals

    _start_time = datetime.now()

    print()
    print(_sep('═'))
    print(f"  {C.BOLD}{C.CYAN}🚀  BBMA OMA ALLY — BINANCE FUTURES  (WebSocket + REST){C.RESET}")
    print(_sep('─'))
    print(f"  {C.GRAY}Simbol   :{C.RESET} {C.YELLOW}Top-{TOP_N}{C.RESET} Binance Futures (rank by Open Interest)")
    print(f"  {C.GRAY}Data     :{C.RESET} {C.GREEN}DUAL-SOURCE{C.RESET}: WebSocket realtime + REST Poll tiap {REST_POLL_INTERVAL//60}m")
    print(f"  {C.GRAY}TF       :{C.RESET} {C.CYAN}" + " · ".join(tf.upper() for tf in TIMEFRAMES) + C.RESET)
    print(f"  {C.GRAY}Sinyal   :{C.RESET} {C.MAGENTA}RE ENTRY · MMT · EXTREME{C.RESET}  (BUY & SELL)")
    print(f"  {C.GRAY}Output   :{C.RESET} {DATA_DIR}/  |  Chart: {CHART_DIR}/")
    print(f"  {C.GRAY}Waktu    :{C.RESET} {_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  {C.GRAY}Bug Fixes:{C.RESET} {C.GREEN}FIX-1{C.RESET} Race Condition | {C.GREEN}FIX-2{C.RESET} Indikator | "
          f"{C.GREEN}FIX-3{C.RESET} Index | {C.GREEN}FIX-4{C.RESET} Scan Delay | {C.GREEN}FIX-5{C.RESET} Dual-Source | {C.GREEN}FIX-6{C.RESET} Dedup Key")
    print(_sep('═'))
    print()

    send_telegram_photo(
        f"🟡 <b>BBMA Bot MENYALA</b>\n"
        f"──────────────────────\n"
        f"⏰ Waktu start  : {_start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"📊 Simbol target: Top-{TOP_N} Binance Futures\n"
        f"📡 Sumber data  : DUAL-SOURCE (WebSocket + REST Poll)\n"
        f"🔄 Status       : Inisialisasi dimulai...\n"
        f"⚠️  <i>Bot belum LIVE — sedang mempersiapkan data</i>"
    )

    state = load_state()
    with _proc_lock:
        _processed_signals.update(state.get('processed_signals', {}))
    _spinner_msg(f"State dimuat — {C.YELLOW}{len(_processed_signals)}{C.RESET} sinyal tercatat", done=True)

    _section("Mengambil daftar simbol Binance Futures", "🔄")

    send_telegram_photo(
        f"📡 <b>Mengambil daftar simbol...</b>\n"
        f"──────────────────────\n"
        f"🔍 Sumber  : Binance Futures Open Interest\n"
        f"🏆 Target  : Top-{TOP_N} USDT Perpetual\n"
        f"⏳ Mohon tunggu beberapa saat..."
    )

    symbols = get_all_futures_symbols()
    while not symbols:
        _warn("Gagal ambil market. Retry 30s...")
        send_telegram_photo(
            f"⚠️ <b>Gagal ambil daftar simbol</b>\n"
            f"Retry otomatis dalam 30 detik..."
        )
        time.sleep(30)
        symbols = get_all_futures_symbols()

    with _sym_lock:
        _shared_symbols.extend(symbols)
    state['last_market_fetch'] = time.time()
    save_state(state)

    send_telegram_photo(
        f"✅ <b>Daftar simbol berhasil diambil</b>\n"
        f"──────────────────────\n"
        f"💎 Total simbol : <b>{len(symbols)}</b> koin aktif\n"
        f"📥 TF           : {' · '.join(tf.upper() for tf in TIMEFRAMES)}\n"
        f"⏳ Sekarang mengunduh data historis...\n"
        f"<i>({len(symbols)} simbol × {len(TIMEFRAMES)} TF = {len(symbols)*len(TIMEFRAMES)} dataset)</i>"
    )

    _seed_start = time.time()
    rest_seed_all(symbols)
    _seed_elapsed = time.time() - _seed_start

    _register_streams(symbols)
    _spinner_msg(f"Stream map terdaftar — "
                 f"{C.YELLOW}{len(symbols) * len(TIMEFRAMES)}{C.RESET} stream", done=True)

    _chart_seed = generate_status_chart(label='SEED_SELESAI')
    send_telegram_photo(
        f"📦 <b>Unduh data historis selesai</b>\n"
        f"──────────────────────\n"
        f"✅ {len(symbols)} simbol × {len(TIMEFRAMES)} TF berhasil di-seed\n"
        f"⏱ Durasi unduh : {_seed_elapsed:.0f} detik\n"
        f"🔍 Selanjutnya : Scan sinyal 8 jam terakhir (missed signal)...\n"
        f"📊 <i>Chart: BTC/USDT 1H BBMA Overview</i>",
        image_path=_chart_seed,
    )

    scan_missed_signals(symbols)

    stop_event  = threading.Event()
    all_threads = []

    # ── FIX-1: Signal queue processor thread (satu thread, serial) ─
    t_queue = threading.Thread(target=signal_queue_processor,
                               args=(stop_event,),
                               name="SignalQueue", daemon=True)
    t_queue.start()
    all_threads.append(t_queue)

    # ── WebSocket connections (sumber data ke-1) ──────────────────
    ws_pairs = launch_websocket_connections(symbols, stop_event)
    all_threads.extend([t for t, _ in ws_pairs])

    # ── FIX-5: REST Poll daemon (sumber data ke-2) ────────────────
    t_poll = threading.Thread(target=rest_poll_daemon,
                              args=(stop_event,),
                              name="RestPoll", daemon=True)
    t_poll.start()
    all_threads.append(t_poll)

    # ── Symbol refresh daemon ──────────────────────────────────────
    t_sym = threading.Thread(target=symbol_refresh_daemon,
                             args=(state, stop_event),
                             name="SymRefresh", daemon=True)
    t_sym.start()
    all_threads.append(t_sym)

    # ── State save daemon ──────────────────────────────────────────
    t_save = threading.Thread(target=state_save_daemon,
                              args=(state, stop_event),
                              name="StateSave", daemon=True)
    t_save.start()
    all_threads.append(t_save)

    # ── FIX-4: Periodic scan daemon (mulai segera setelah seed) ───
    t_scan = threading.Thread(target=periodic_scan_daemon,
                              args=(stop_event,),
                              name="PeriodicScan", daemon=True)
    t_scan.start()
    all_threads.append(t_scan)

    # ── Heartbeat daemon — kirim status ke Telegram tiap 1 jam ───
    t_hb = threading.Thread(target=heartbeat_daemon,
                             args=(_start_time, ws_pairs, symbols, stop_event),
                             name="Heartbeat", daemon=True)
    t_hb.start()
    all_threads.append(t_hb)

    _live_time   = datetime.now()
    _total_init  = (_live_time - _start_time).seconds

    print()
    print(_sep('═'))
    print(f"  {C.BOLD}{C.GREEN}✅ Bot aktif — {len(ws_pairs)} koneksi WebSocket{C.RESET}")
    print(f"  {C.GRAY}Simbol   : {C.YELLOW}{len(symbols)}{C.GRAY} koin dipantau{C.RESET}")
    print(f"  {C.GRAY}Threads  : {C.YELLOW}{threading.active_count()}{C.GRAY} aktif{C.RESET}")
    print(f"  {C.GRAY}Sumber 1 : {C.GREEN}WebSocket{C.GRAY} — candle close realtime{C.RESET}")
    print(f"  {C.GRAY}Sumber 2 : {C.BLUE}REST Poll{C.GRAY} — tiap {REST_POLL_INTERVAL//60} menit, ambil {REST_POLL_CANDLES} candle/TF{C.RESET}")
    print(f"  {C.GRAY}Backup   : {C.CYAN}Periodic Scan{C.GRAY} tiap {PERIODIC_SCAN_INTERVAL//60} menit{C.RESET}")
    print(f"  {C.GRAY}Heartbeat: {C.MAGENTA}Telegram{C.GRAY} tiap {HEARTBEAT_INTERVAL//60} menit{C.RESET}")
    print(f"  {C.GRAY}Stop     : Ctrl+C untuk berhenti{C.RESET}")
    print(_sep('═'))
    print()

    print(f"  {C.BOLD}{C.GRAY}{'WAKTU':<10} {'SRC':<5} {'SINYAL':<14} {'SIMBOL':<22} {'TF':<4} {'HARGA'}{C.RESET}")
    print(f"  {C.GRAY}{'─'*10} {'─'*5} {'─'*14} {'─'*22} {'─'*4} {'─'*12}{C.RESET}")

    _chart_live = generate_status_chart(label='BOT_LIVE')
    send_telegram_photo(
        f"\U0001f7e2 <b>BBMA Bot SEKARANG LIVE!</b>\n"
        f"\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\n"
        f"\U0001f48e Simbol      : <b>{len(symbols)}</b> koin (Top-{TOP_N} by OI)\n"
        f"\U0001f4ca Timeframe   : {' \u00b7 '.join(tf.upper() for tf in TIMEFRAMES)}\n"
        f"\U0001f3af Sinyal      : RE ENTRY \u00b7 MMT \u00b7 EXTREME\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"\U0001f4e1 Sumber 1    : WebSocket ({len(ws_pairs)} koneksi aktif)\n"
        f"\U0001f517 Sumber 2    : REST Poll (tiap {REST_POLL_INTERVAL//60} menit)\n"
        f"\U0001f50d Backup scan : Tiap {PERIODIC_SCAN_INTERVAL//60} menit\n"
        f"\U0001f49a Heartbeat   : Status dikirim tiap {HEARTBEAT_INTERVAL//60} menit\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"\u23f0 Start       : {_start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"\U0001f680 LIVE sejak  : {_live_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"\u23f1 Init selesai: {_total_init} detik\n"
        f"\U0001f9f5 Threads     : {threading.active_count()} aktif\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"\u2705 <b>Bot aktif memantau sinyal dari 2 sumber data.</b>\n"
        f"\U0001f4e9 Sinyal & status dikirim otomatis ke sini.",
        image_path=_chart_live,
    )


    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        print()
        print(_sep('─'))
        _warn("Menghentikan bot (Ctrl+C)...")
        stop_event.set()
        for _, conn in ws_pairs:
            conn.stop()
        for t in all_threads:
            t.join(timeout=5)
        with _proc_lock:
            state['processed_signals'] = dict(_processed_signals)
        save_state(state)

        _stop_time   = datetime.now()
        _uptime_sec  = int((_stop_time - _start_time).total_seconds())
        _uptime_str  = (
            f"{_uptime_sec // 3600}j "
            f"{(_uptime_sec % 3600) // 60}m "
            f"{_uptime_sec % 60}d"
        )
        with _proc_lock:
            _total_sigs = len(_processed_signals)
        _chart_stop = generate_status_chart(label='BOT_STOP')
        send_telegram_photo(
            f"🔴 <b>BBMA Bot DIHENTIKAN (Manual)</b>\n"
            f"══════════════════════\n"
            f"⛔ Alasan      : Dihentikan oleh operator (Ctrl+C)\n"
            f"⏰ Mulai       : {_start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"🛑 Berhenti    : {_stop_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"⏱ Uptime       : {_uptime_str}\n"
            f"📊 Total sinyal: {_total_sigs} sinyal tercatat\n"
            f"──────────────────────\n"
            f"ℹ️  <i>Jalankan ulang bot untuk melanjutkan pemantauan.</i>",
            image_path=_chart_stop,
        )
        _ok("Bot berhenti.")
        print()
    except Exception as _fatal_err:
        _stop_time  = datetime.now()
        _uptime_sec = int((_stop_time - _start_time).total_seconds())
        _uptime_str = (
            f"{_uptime_sec // 3600}j "
            f"{(_uptime_sec % 3600) // 60}m "
            f"{_uptime_sec % 60}d"
        )
        stop_event.set()
        _chart_crash = generate_status_chart(label='BOT_CRASH')
        send_telegram_photo(
            f"💀 <b>BBMA Bot CRASH — ERROR TAK TERDUGA!</b>\n"
            f"══════════════════════\n"
            f"🚨 Error   : <code>{str(_fatal_err)[:300]}</code>\n"
            f"⏰ Mulai   : {_start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"💥 Crash   : {_stop_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"⏱ Uptime  : {_uptime_str}\n"
            f"──────────────────────\n"
            f"⚠️  <b>Bot mati mendadak! Perlu dinyalakan ulang secara manual.</b>",
            image_path=_chart_crash,
        )
        raise


if __name__ == "__main__":
    main()
