import base64
import copy
import functools
import io
import json
import math
import os
import random
import statistics
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

try:
    import websocket
except ImportError:
    sys.exit("Missing package. Run: pip install websocket-client cryptography requests pillow")

try:
    from zoneinfo import ZoneInfo
    CHART_TZ = ZoneInfo(os.getenv("CHART_TZ", "America/New_York"))
except Exception:
    CHART_TZ = timezone.utc

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
SERIES = os.getenv("KALSHI_SERIES", "KXETH15M")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
POLL = float(os.getenv("POLL_SECONDS", "0.5"))
EDIT_SECONDS = float(os.getenv("EDIT_SECONDS", "1"))
IMG_SECONDS = float(os.getenv("IMG_SECONDS", "1"))
NEW_MSG_SECONDS = float(os.getenv("NEW_MSG_SECONDS", "30"))
MIN_GAP = float(os.getenv("MIN_GAP", "99999"))  # momentum-shift posts off by default
RUN_SECONDS = int(os.getenv("RUN_SECONDS", str(345 * 60)))
STATE_FILE = os.getenv("STATE_FILE", "state.json")
GREEN, RED, GREY = 0x00FF00, 0xFF3B30, 0x808080
W_MARKET = float(os.getenv("W_MARKET", "0.5"))
FINAL_SECONDS = 60  # Kalshi settles on the average of the last 60s
CALL_CUTOFF = max(int(os.getenv("CALL_CUTOFF", "60")), FINAL_SECONDS)
TILT_W = float(os.getenv("TILT_W", "0.25"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "0.22"))
MOM_MIN = float(os.getenv("MOM_MIN", "0.10"))
FLIP_SCORE = float(os.getenv("FLIP_SCORE", "99"))
CALL_AFTER = int(os.getenv("CALL_AFTER", "99999"))
CONFIRM_SECONDS = float(os.getenv("CONFIRM_SECONDS", "1.5"))
W_MOM = float(os.getenv("W_MOM", "0.35"))
W_RSI = float(os.getenv("W_RSI", "0.10"))
W_FIB = float(os.getenv("W_FIB", "0.35"))
W_TREND = float(os.getenv("W_TREND", "0.10"))
W_PROB = float(os.getenv("W_PROB", "0.10"))
RSI_N = int(os.getenv("RSI_N", "14"))
RSI_BLOCK = float(os.getenv("RSI_BLOCK", "20"))
RSI_GRACE = float(os.getenv("RSI_GRACE", "10"))
FIB_AGREE = float(os.getenv("FIB_AGREE", "0.15"))
FIB_LEVELS = (0.236, 0.382, 0.5, 0.618, 0.786)
TRIGGER_PCT = float(os.getenv("TRIGGER_PCT", "50.1"))
FLIP_HARD = float(os.getenv("FLIP_HARD", "0.45"))
FLIP_HOLD = float(os.getenv("FLIP_HOLD", "8"))
FLIP_MIN_AGE = float(os.getenv("FLIP_MIN_AGE", "20"))
LOGO_PATH = os.getenv("LOGO_PATH", "eth_logo.png")
LIVE_SCALE = float(os.getenv("LIVE_SCALE", "1.0"))
FINAL_SCALE = float(os.getenv("FINAL_SCALE", "3"))
MAX_BYTES = int(float(os.getenv("MAX_MB", "7.5")) * 1024 * 1024)
LIVE_FRAMES = int(os.getenv("LIVE_FRAMES", "4"))      # LIVE: fewer frames, faster render
TIMER_LEAD = float(os.getenv("TIMER_LEAD", "0.7"))    # LIVE: clock shows the time Discord will display

# ---- combined call settings ----
CALL_MODE = os.getenv("CALL_MODE", "combined")
W_CORE = float(os.getenv("W_CORE", "0.5"))
W_VOTE = float(os.getenv("W_VOTE", "0.5"))
COMBO_MIN = float(os.getenv("COMBO_MIN", "0.30"))
COMBO_OPP = int(os.getenv("COMBO_OPP", "1"))
CONF_MIN = int(os.getenv("CONF_MIN", "3"))
EMA_DEAD = float(os.getenv("EMA_DEAD", "0.00005"))
MACD_DEAD = float(os.getenv("MACD_DEAD", "0.00002"))
BB_UP = float(os.getenv("BB_UP", "0.65"))
BB_DN = float(os.getenv("BB_DN", "0.35"))
ATR_N = int(os.getenv("ATR_N", "14"))
ATR_Z = float(os.getenv("ATR_Z", "0.5"))
CONF_FULL = float(os.getenv("CONF_FULL", "0.6"))
CONF_VOTE_W = float(os.getenv("CONF_VOTE_W", "0.4"))

# ---- fast flip settings (now unused: SELL uses the call trigger) ----
FAST_FLIP = int(os.getenv("FAST_FLIP", "1"))
FAST_FLIP_COMB = float(os.getenv("FAST_FLIP_COMB", "0.50"))
FAST_FLIP_MOM = float(os.getenv("FAST_FLIP_MOM", "0.60"))
FAST_FLIP_CONF = float(os.getenv("FAST_FLIP_CONF", "85"))
FAST_FLIP_OPP = int(os.getenv("FAST_FLIP_OPP", "0"))
FAST_FLIP_HOLD = float(os.getenv("FAST_FLIP_HOLD", "2.0"))
FAST_FLIP_AGE = float(os.getenv("FAST_FLIP_AGE", "10"))
FAST_FLIP_CUTOFF = int(os.getenv("FAST_FLIP_CUTOFF", "30"))
FAST_FLIP_MAX = int(os.getenv("FAST_FLIP_MAX", "2"))
FAST_FLIP_NEED_MKT = int(os.getenv("FAST_FLIP_NEED_MKT", "0"))
SELL_FLIP = int(os.getenv("SELL_FLIP", "1"))
SELL_CUTOFF = int(os.getenv("SELL_CUTOFF", "10"))

# ---- reversal flip settings ----
REV_FLIP = int(os.getenv("REV_FLIP", "1"))
REV_MKT = float(os.getenv("REV_MKT", "60"))
REV_PM = float(os.getenv("REV_PM", "65"))
REV_HOLD = float(os.getenv("REV_HOLD", "3"))
REV_MAX = int(os.getenv("REV_MAX", "3"))

# ---- last-minute settlement settings ----
W_MARKET_FINAL = float(os.getenv("W_MARKET_FINAL", "0.35"))
FINAL_CALL_PCT = float(os.getenv("FINAL_CALL_PCT", "70"))
FINAL_FLIP_PCT = float(os.getenv("FINAL_FLIP_PCT", "80"))
FINAL_HOLD = float(os.getenv("FINAL_HOLD", "2.0"))
FINAL_CUTOFF = int(os.getenv("FINAL_CUTOFF", "5"))


# ---------- state ----------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"history": [], "current": None, "pending": []}


def save_state(s):
    s["history"] = s["history"][-500:]
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=1)


# ---------- kalshi data ----------
def cents(m, key):
    if m.get(key) is not None:
        return float(m[key])
    if m.get(key + "_dollars") is not None:
        return float(m[key + "_dollars"]) * 100
    return None


def yes_pct(m):
    bid, ask = cents(m, "yes_bid"), cents(m, "yes_ask")
    if bid is not None and ask is not None and ask > 0:
        return (bid + ask) / 2
    last = cents(m, "last_price")
    return last if last is not None else 50.0


def ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def open_market():
    r = requests.get(f"{KALSHI}/markets",
                     params={"series_ticker": SERIES, "status": "open", "limit": 20}, timeout=10)
    r.raise_for_status()
    now = time.time()
    ms = [m for m in r.json().get("markets", []) if ts(m["close_time"]) > now]
    return min(ms, key=lambda m: ts(m["close_time"])) if ms else None


def get_market(ticker):
    r = requests.get(f"{KALSHI}/markets/{ticker}", timeout=10)
    r.raise_for_status()
    return r.json()["market"]


# ---------- crypto prices (ETH, BTC) ----------
WEIGHTS = {"ETH": 0.5, "BTC": 0.5}
DEF_SIG = {"ETH": 1.1e-4, "BTC": 0.9e-4}
PRICES = {s: deque(maxlen=4000) for s in WEIGHTS}


def _cb(pair):
    return float(requests.get(f"https://api.coinbase.com/v2/prices/{pair}/spot",
                              timeout=5).json()["data"]["amount"])


def _kr(pair):
    r = requests.get("https://api.kraken.com/0/public/Ticker",
                     params={"pair": pair}, timeout=5).json()["result"]
    return float(next(iter(r.values()))["c"][0])


def _bs(pair):
    return float(requests.get(f"https://www.bitstamp.net/api/v2/ticker/{pair}/",
                              timeout=5).json()["last"])


SOURCES = {
    "ETH": [(_cb, "ETH-USD"), (_kr, "ETHUSD"), (_bs, "ethusd")],
    "BTC": [(_cb, "BTC-USD"), (_kr, "XBTUSD"), (_bs, "btcusd")],
}


def _safe(job):
    sym, fn, pair = job
    try:
        return sym, fn(pair)
    except Exception:
        return sym, None


def all_prices():
    """{sym: (median_price, n_exchanges)}; (None, 0) if every source failed."""
    jobs = [(s, fn, pair) for s, srcs in SOURCES.items() for fn, pair in srcs]
    got = {s: [] for s in SOURCES}
    with ThreadPoolExecutor(len(jobs)) as ex:
        for sym, p in ex.map(_safe, jobs):
            if p:
                got[sym].append(p)
    return {s: ((statistics.median(v), len(v)) if v else (None, 0)) for s, v in got.items()}


def eth_price():
    return all_prices()["ETH"]


# ---------- prediction ----------
def record_price(sym, price):
    if price:
        PRICES[sym].append((time.time(), price))


def series_10s(sym="ETH", n=90, step=10):
    """Resample the price history to a 10s grid, so signal windows keep their meaning."""
    pts = list(PRICES[sym])
    if not pts:
        return []
    now = pts[-1][0]
    out, j, last = [], 0, None
    for i in range(n):
        tgt = now - step * (n - 1 - i)
        while j < len(pts) and pts[j][0] <= tgt:
            last = pts[j][1]
            j += 1
        if last is not None:
            out.append(last)
    return out


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def vol(sym):
    pts = list(PRICES[sym])
    rets = []
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        dt = t1 - t0
        if dt > 0.5:
            rets.append(math.log(p1 / p0) / math.sqrt(dt))
    base = DEF_SIG[sym]
    if len(rets) < 8:
        return base
    s = math.sqrt(sum(r * r for r in rets) / len(rets))
    return max(base * 0.4, min(s, base * 4))


def z60(sym):
    pts = list(PRICES[sym])
    if len(pts) < 2:
        return 0.0
    t_now, p_now = pts[-1]
    old = [x for x in pts if t_now - x[0] >= 60]
    if not old:
        return 0.0
    t0, p0 = old[-1]
    return math.log(p_now / p0) / (vol(sym) * math.sqrt(t_now - t0))


def z_open(sym, price, open_px, since_open):
    if not price or not open_px or since_open < 20:
        return 0.0
    return math.log(price / open_px) / (vol(sym) * math.sqrt(since_open))


def market_signal(prices, opens, since_open):
    """Returns (tilt, breadth, per-coin z). Positive tilt = basket pulling ETH up."""
    zs = {}
    for s in WEIGHTS:
        z = 0.6 * z60(s) + 0.4 * z_open(s, prices.get(s), opens.get(s), since_open)
        zs[s] = max(-3.0, min(3.0, z))
    basket = sum(WEIGHTS[s] * zs[s] for s in WEIGHTS)
    agree = sum(WEIGHTS[s] * (0 if abs(zs[s]) < 0.15 else (1 if zs[s] > 0 else -1)) for s in WEIGHTS)
    breadth = abs(agree) / sum(WEIGHTS.values())
    return basket * (0.5 + 0.5 * breadth), breadth, zs


def window_avg(sym, close_ts, now):
    """Time-weighted sum and seconds of price already inside the final 60s window."""
    start, end = close_ts - FINAL_SECONDS, min(now, close_ts)
    pts = list(PRICES[sym])
    if end <= start or not pts:
        return 0.0, 0.0
    px, inside = None, []
    for t, p in pts:
        if t <= start:
            px = p
        elif t <= end:
            inside.append((t, p))
    if px is None:
        px = inside[0][1] if inside else pts[-1][1]
    total, t0 = 0.0, start
    for t, p in inside:
        total += px * (t - t0)
        t0, px = t, p
    total += px * (end - t0)
    return total, end - start


def settle_stats(price, strike, close_ts, now, tilt=0.0):
    """Odds that the 60s average finishes >= strike. Locked-in seconds carry no risk."""
    if not price or not strike:
        return None
    tau0 = max(close_ts - now, 0.0)
    total, e = window_avg("ETH", close_ts, now)
    rem = min(tau0, float(FINAL_SECONDS))
    a, b = max(tau0 - FINAL_SECONDS, 0.0), tau0
    exp_avg = (total + price * rem) / FINAL_SECONDS
    V = -(b ** 3 - a ** 3) / 6 - a * a * (b - a) / 2 + b * (b * b - a * a) / 2
    std = price * vol("ETH") * math.sqrt(max(V, 0.0)) / FINAL_SECONDS
    fade = min(1.0, math.sqrt(tau0 / 300)) * rem / FINAL_SECONDS
    shift = TILT_W * max(-3.0, min(3.0, tilt)) * fade
    if std <= 1e-9:
        pm = 99.0 if exp_avg >= strike else 1.0
    else:
        pm = max(1.0, min(99.0, norm_cdf((exp_avg - strike) / std + shift) * 100))
    return {"pm": pm, "exp_avg": exp_avg, "std": std, "rem": rem,
            "obs_avg": (total / e) if e > 1 else None,
            "need": ((strike * FINAL_SECONDS - total) / rem) if rem > 0.5 else None}


def predict(yes_mkt, ss, secs_left):
    if ss is None:
        return yes_mkt, None
    w = W_MARKET_FINAL if secs_left <= FINAL_SECONDS else W_MARKET
    return w * yes_mkt + (1 - w) * ss["pm"], ss["pm"]


def _fib_score(d):
    if d < 0.236:
        return 1.0
    if d < 0.382:
        return 0.7
    if d < 0.5:
        return 0.5
    if d < 0.618:
        return 0.2
    if d < 0.786:
        return -0.3
    return -0.8


def fib_signal(prices):
    """(score -1..1, nearest fib level string or None)"""
    if len(prices) < 8:
        return 0.0, None
    hi, lo, now = max(prices), min(prices), prices[-1]
    if hi - lo < now * 0.0003:
        return 0.0, None
    up_swing = prices.index(lo) < len(prices) - 1 - prices[::-1].index(hi)
    d = (hi - now) / (hi - lo) if up_swing else (now - lo) / (hi - lo)
    d = max(0.0, min(1.0, d))
    lvl = min(FIB_LEVELS, key=lambda r: abs(r - d))
    return (1 if up_swing else -1) * _fib_score(d), f"{lvl * 100:.1f}".rstrip("0").rstrip(".")


def fib_ok(side, f):
    sgn = 1 if side == "UP" else -1
    return f * sgn >= FIB_AGREE


def fib_bias(f):
    if f >= 0.15:
        return "bullish"
    if f <= -0.15:
        return "bearish"
    return "neutral"


def trend_signal(prices, tilt):
    if len(prices) < 8:
        ema = 0.0
    else:
        e3, e8 = _ema(prices, 3)[-1], _ema(prices, 8)[-1]
        ema = math.tanh(math.log(e3 / e8) / (vol("ETH") * math.sqrt(30)))
    return 0.5 * ema + 0.5 * math.tanh(tilt / 1.5)


def rsi(prices, n=None):
    n = n or RSI_N
    if len(prices) < n + 1:
        return None
    ch = [b - a for a, b in zip(prices, prices[1:])]
    ag = sum(max(c, 0) for c in ch[:n]) / n
    al = sum(max(-c, 0) for c in ch[:n]) / n
    for c in ch[n:]:
        ag = (ag * (n - 1) + max(c, 0)) / n
        al = (al * (n - 1) + max(-c, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def rsi_ok(side, r):
    if r is None:
        return True
    return r >= RSI_BLOCK if side == "UP" else r <= 100 - RSI_BLOCK


# ---------- indicator votes (EMA 9/21, Bollinger, ATR, MACD) ----------
def vote_ema(p):
    if len(p) < 21:
        return None, "warming up"
    e9, e21 = _ema(p, 9)[-1], _ema(p, 21)[-1]
    gap = (e9 - e21) / p[-1]
    v = 1 if gap > EMA_DEAD else -1 if gap < -EMA_DEAD else 0
    return v, f"EMA9 {'above' if e9 > e21 else 'below'} EMA21 ({gap * 1e4:+.1f}bp)"


def vote_boll(p, n=20, k=2.0):
    if len(p) < n:
        return None, "warming up"
    w = p[-n:]
    m, sd = statistics.fmean(w), statistics.pstdev(w)
    if sd == 0:
        return 0, "flat"
    pb = (p[-1] - (m - k * sd)) / (2 * k * sd)
    v = 1 if pb >= BB_UP else -1 if pb <= BB_DN else 0
    return v, f"%B {pb:.2f}"


def atr(p, n=None):
    n = n or ATR_N
    if len(p) < n + 1:
        return None
    tr = [abs(b - a) for a, b in zip(p, p[1:])]
    a = sum(tr[:n]) / n
    for t in tr[n:]:
        a = (a * (n - 1) + t) / n
    return a


def vote_atr(p, price, strike, secs_left, ss=None):
    if secs_left <= FINAL_SECONDS and ss:
        gap = ss["exp_avg"] - strike
        z = gap / ss["std"] if ss["std"] > 1e-9 else (9.0 if gap >= 0 else -9.0)
        v = 1 if z >= ATR_Z else -1 if z <= -ATR_Z else 0
        return v, f"60s avg proj ${ss['exp_avg']:,.2f}, {z:+.1f}σ from strike"
    a = atr(p)
    if a is None or not price or not strike or a <= 0:
        return None, "warming up"
    reach = a * math.sqrt(max(secs_left, 10) / 10)
    z = (price - strike) / reach
    v = 1 if z >= ATR_Z else -1 if z <= -ATR_Z else 0
    return v, f"ATR ${a:.2f}, {z:+.1f}x reach from strike"


def vote_macd(p):
    if len(p) < 35:
        return None, "warming up"
    line = [a - b for a, b in zip(_ema(p, 12), _ema(p, 26))]
    sig = _ema(line, 9)
    hist, prev = line[-1] - sig[-1], line[-2] - sig[-2]
    dead = p[-1] * MACD_DEAD
    v = 1 if hist > dead else -1 if hist < -dead else 0
    return v, f"hist {hist:+.2f} ({'rising' if hist > prev else 'falling'})"


def _pressure(p):
    ups = sum(max(b - a, 0) for a, b in zip(p, p[1:]))
    dns = sum(max(a - b, 0) for a, b in zip(p, p[1:]))
    t = ups + dns
    return (ups - dns) / t if t > 0 else 0.0


def momentum_fast(sym="ETH", win=10):
    pts = list(PRICES[sym])
    if len(pts) < 6:
        return 0.0
    t_now = pts[-1][0]
    cur = [p for t, p in pts if t_now - t <= win]
    prev = [p for t, p in pts if win < t_now - t <= 2 * win]
    if len(cur) < 4:
        return 0.0
    now = _pressure(cur)
    gain = (now - _pressure(prev)) / 2 if len(prev) >= 4 else 0.0
    return max(-1.0, min(1.0, 0.65 * now + 0.35 * gain))


def composite(mom, fib, trend, blend, rsi_s=0.0):
    prob = max(-1.0, min(1.0, (blend - 50) / 50))
    return (W_MOM * mom + W_FIB * fib + W_TREND * trend + W_PROB * prob + W_RSI * rsi_s), prob


def pick_call(score, mom, fib, trend, prob):
    sgn = 1 if mom > 0 else -1
    if abs(mom) < MOM_MIN or score * sgn <= 0:
        return None
    if fib * sgn < -0.5 or trend * sgn < -0.5:
        return None
    return ("UP" if sgn > 0 else "DOWN"), 50 + 50 * min(1.0, abs(mom))


def side_pct(side, yes):
    up = min(max(yes, 1), 99)
    pct = up if side == "UP" else 100 - up
    return max(1.0, pct - 5)


def combo_conf(side, comb, votes):
    sgn = 1 if side == "UP" else -1
    vs = [v[0] for v in votes.values() if v[0] is not None]
    agree = sum(v * sgn for v in vs) / len(vs) if vs else 0.0
    strength = max(-1.0, min(1.0, comb * sgn / CONF_FULL))
    x = (1 - CONF_VOTE_W) * strength + CONF_VOTE_W * agree
    return max(1.0, min(99.0, 50 + 49 * x))


def combo_side(comb, ready, fib, r):
    sgn = 1 if comb > 0 else -1
    cside = "UP" if sgn > 0 else "DOWN"
    against = sum(1 for v in ready if v == -sgn)
    good = (abs(comb) >= COMBO_MIN and against <= COMBO_OPP
            and fib * sgn >= -0.5 and rsi_ok(cside, r))
    return cside if good else None


def pct_trigger(yes):
    up = min(max(yes, 1), 99)
    dn = 100 - up
    if up >= TRIGGER_PCT:
        return "UP"
    if dn >= TRIGGER_PCT:
        return "DOWN"
    return None


# ---------- stats ----------
def stats(history):
    n = len(history)
    w = sum(1 for h in history if h["win"])
    return {"all": f"{w}W-{n - w}L ({w / n * 100:.0f}%)" if n else "n/a"}


# ---------- call labels ----------
def momentum_level(c):
    sg = c.get("sig")
    if not sg:
        return "LOW"
    m = abs(sg.get("mom", 0))
    if m >= 0.6:
        return "HIGH"
    if m >= 0.3:
        return "MODERATE"
    return "LOW"


def fib_level(c):
    return c.get("fib")


# ---------- chart ----------
CW, CH = 720, 860
WHITE = (255, 255, 255, 255)
MUTED = (160, 168, 178, 255)
C_UP, C_DN, ORG = (0, 217, 142), (255, 87, 87), (255, 140, 58)
CYAN, BLUE, AMBER = (0, 229, 255), (80, 120, 255), (255, 190, 40)
TGT = (205, 210, 220)

_FONT_DIRS = ["", "/usr/share/fonts/truetype/inter/", "/usr/share/fonts/truetype/roboto/",
              "/usr/share/fonts/truetype/liberation/", "/usr/share/fonts/truetype/liberation2/",
              "/usr/share/fonts/truetype/noto/", "/usr/share/fonts/truetype/dejavu/",
              "C:/Windows/Fonts/", "/Library/Fonts/", "/System/Library/Fonts/Supplemental/"]
_FONTS_B = ["Inter-Bold.ttf", "Roboto-Bold.ttf", "LiberationSans-Bold.ttf", "NotoSans-Bold.ttf",
            "arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf"]
_FONTS_R = ["Inter-Regular.ttf", "Roboto-Regular.ttf", "LiberationSans-Regular.ttf",
            "NotoSans-Regular.ttf", "arial.ttf", "Arial.ttf", "DejaVuSans.ttf"]


@functools.lru_cache(maxsize=128)
def _font(size, bold=True):
    custom = os.getenv("CHART_FONT")
    if custom:
        try:
            return ImageFont.truetype(custom, size)
        except OSError:
            pass
    for name in (_FONTS_B if bold else _FONTS_R):
        for dr in _FONT_DIRS:
            try:
                return ImageFont.truetype(dr + name, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _arrow(d, cx, cy, up, size, fill):
    shape = [(0, -1.0), (0.9, -0.1), (0.35, -0.1), (0.35, 0.9), (-0.35, 0.9), (-0.35, -0.1), (-0.9, -0.1)]
    d.polygon([(cx + px * size, cy + (py if up else -py) * size) for px, py in shape], fill=fill)


def _dots(d, x0, y0, x1, y1, fill, w=1, on=3.0, off=5.0, phase=0.0):
    L = math.hypot(x1 - x0, y1 - y0)
    if L <= 0:
        return
    ux, uy = (x1 - x0) / L, (y1 - y0) / L
    p = -(phase % (on + off))
    while p < L:
        a, b = max(p, 0), min(p + on, L)
        if b > a:
            d.line([(x0 + ux * a, y0 + uy * a), (x0 + ux * b, y0 + uy * b)], fill=fill, width=w)
        p += on + off


def _glow(base, fn, color, radius, strength=0.9):
    m = Image.new("L", base.size, 0)
    fn(ImageDraw.Draw(m))
    m = m.filter(ImageFilter.GaussianBlur(radius)).point(lambda v: int(v * strength))
    base.paste(color, (0, 0), m)


@functools.lru_cache(maxsize=8)
def _logo(px):
    try:
        im = ImageOps.fit(Image.open(LOGO_PATH).convert("RGBA"), (px, px), Image.LANCZOS)
    except Exception:
        im = Image.new("RGBA", (px, px), (15, 26, 61, 255))
        g = ImageDraw.Draw(im)
        c, u = px / 2, px / 100
        top, mid, low = (c, c - 36 * u), (c, c + 8 * u), (c, c + 38 * u)
        lft, rgt = (c - 22 * u, c - 2 * u), (c + 22 * u, c - 2 * u)
        g.polygon([top, rgt, mid], fill=(232, 159, 240, 255))
        g.polygon([top, lft, mid], fill=(155, 77, 202, 255))
        g.polygon([lft, mid, low], fill=(130, 65, 190, 255))
        g.polygon([rgt, mid, low], fill=(200, 120, 230, 255))
    m = Image.new("L", (px * 4, px * 4), 0)
    ImageDraw.Draw(m).ellipse([0, 0, px * 4 - 1, px * 4 - 1], fill=255)
    m = m.resize((px, px), Image.LANCZOS)
    out = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    out.paste(im, (0, 0), m)
    return out


def _clock(t):
    dt = datetime.fromtimestamp(t, CHART_TZ)
    return f"{dt.hour % 12 or 12}:{dt.minute:02d}{'am' if dt.hour < 12 else 'pm'}"


def _mmss(s):
    s = max(0, int(s))
    return f"{s // 60}:{s % 60:02d}"


def _bg(Wd, Hd):
    h = Image.linear_gradient("L").transpose(Image.Transpose.ROTATE_90).resize((Wd, Hd))
    top = ImageOps.colorize(h, (13, 31, 31), (45, 26, 31))
    v = Image.linear_gradient("L").resize((Wd, Hd)).point(lambda p: min(255, int(p * 1.5)))
    return Image.composite(Image.new("RGB", (Wd, Hd), (10, 10, 10)), top, v)


def _candles(eth, fallback):
    b = {}
    for t, v in eth:
        b.setdefault(max(0, min(int(t // 30), 29)), []).append(v)
    out, prev = [], None
    for i in sorted(b):
        vs = b[i]
        o = prev if prev is not None else vs[0]
        out.append((i, o, max(vs + [o]), min(vs + [o]), vs[-1]))
        prev = vs[-1]
    return out or [(0, fallback, fallback, fallback, fallback)]


def _ema(vals, n):
    a = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(out[-1] + a * (v - out[-1]))
    return out


def render_frames(cur, outcome, S, n):
    SS = 2 if n > 1 else 1
    u = S * SS
    k = lambda v: v * u
    F = lambda sz, b=True: _font(max(6, int(k(sz))), b)
    lw = lambda v: max(1, int(k(v)))
    Wd, Hd = int(CW * u), int(CH * u)
    outW, outH = int(CW * S), int(CH * S)
    now = time.time() + TIMER_LEAD  # LIVE: show the time as Discord will display it

    pts = cur.get("pts") or [[0, 50]]
    eth = cur.get("eth") or []
    side = cur.get("side")
    waiting = side is None
    is_up = side == "UP"
    yes_last = pts[-1][1]
    up_pct = min(max(yes_last, 1), 99)
    dn_pct = 100 - up_pct
    conf = cur.get("conf") or max(up_pct, dn_pct)
    strike = cur.get("strike") or (eth[0][1] if eth else 0)
    now_px = eth[-1][1] if eth else (cur.get("price") or strike)
    close_ts = cur.get("close_ts") or (now + 900)
    accent = {"win": C_UP, "loss": C_DN, "settling": AMBER}.get(outcome)
    col = C_UP if is_up else C_DN

    base = _bg(Wd, Hd)
    d = ImageDraw.Draw(base, "RGBA")
    rng = random.Random(7)
    for _ in range(60):
        sx, sy = rng.randint(0, Wd), rng.randint(0, int(Hd * 0.3))
        r = k(rng.choice((0.6, 0.9, 1.2)))
        d.ellipse([sx - r, sy - r, sx + r, sy + r], fill=(255, 255, 255, rng.randint(40, 110)))

    # ---- header ----
    lg = int(k(60))
    base.paste(_logo(lg), (int(k(24)), int(k(24))), _logo(lg))
    d.ellipse([k(24), k(24), k(24) + lg, k(24) + lg], outline=(255, 255, 255, 80), width=lw(1.5))
    d.text((k(98), k(54)), "ETH 15 min", font=F(30), fill=WHITE, anchor="lm")

    nflips = cur.get("flips", 0)
    if nflips and not waiting:
        tw0 = d.textlength("ETH 15 min", font=F(30)) / u
        ftxt = f"FLIP x{nflips}"
        fw = d.textlength(ftxt, font=F(14)) / u + 18
        fx0 = 98 + tw0 + 12
        d.rounded_rectangle([k(fx0), k(42), k(fx0 + fw), k(66)], radius=k(10),
                            fill=AMBER + (60,), outline=AMBER + (255,), width=lw(1.5))
        d.text((k(fx0 + fw / 2), k(54)), ftxt, font=F(14), fill=AMBER + (255,), anchor="mm")

    ring = outcome in (None, "settling")
    if outcome == "win":
        btxt, bcol = "WIN", C_UP
    elif outcome == "loss":
        btxt, bcol = "LOSS", C_DN
    elif outcome == "settling":
        btxt, bcol = "NEXT " + _mmss(close_ts + 900 - now), AMBER
    else:
        btxt, bcol = _mmss(close_ts - now), (255, 255, 255)
    fb = F(22)
    bw = d.textlength(btxt, font=fb) / u + (68 if ring else 40)
    bx0 = 696 - bw
    d.rounded_rectangle([k(bx0), k(28), k(696), k(80)], radius=k(16), fill=(255, 255, 255, 26),
                        outline=(bcol + (190,)) if outcome else (255, 255, 255, 50), width=lw(2))
    if ring:
        cx, cy, r = k(bx0 + 28), k(54), k(12)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 255, 255, 40), width=lw(3))
        left = (close_ts - now) if outcome is None else (close_ts + 900 - now)
        frac = max(0.0, min(1.0, left / 900))
        if frac > 0.003:
            d.arc([cx - r, cy - r, cx + r, cy + r], -90, -90 + 360 * frac,
                  fill=bcol + (255,), width=lw(3))
        d.text((k(bx0 + 50), k(54)), btxt, font=fb, fill=bcol + (255,), anchor="lm")
    else:
        d.text((k(bx0 + bw / 2), k(54)), btxt, font=fb, fill=bcol + (255,), anchor="mm")

    # ---- price row ----
    d.text((k(24), k(124)), f"Target · {_clock(close_ts - 900)}", font=F(15, False), fill=MUTED, anchor="ls")
    d.text((k(24), k(166)), f"${strike:,.2f}" if strike else "--", font=F(32), fill=WHITE, anchor="ls")
    diff = (now_px - strike) if strike else 0
    dcol = (C_UP if diff >= 0 else C_DN) + (255,)
    d.text((k(372), k(124)), "Now", font=F(15), fill=dcol, anchor="ls")
    nw = d.textlength("Now", font=F(15))
    _arrow(d, k(372) + nw + k(12), k(119), diff >= 0, k(6), dcol)
    d.text((k(372) + nw + k(24), k(124)), f"${abs(diff):,.2f}", font=F(15), fill=dcol, anchor="ls")
    d.text((k(372), k(166)), f"${now_px:,.2f}" if now_px else "--", font=F(32), fill=WHITE, anchor="ls")
    d.ellipse([k(673), k(136), k(695), k(158)], outline=MUTED, width=lw(2))
    d.text((k(684), k(147)), "i", font=F(15), fill=MUTED, anchor="mm")

    # ---- chart panel ----
    d.rounded_rectangle([k(12), k(196), k(708), k(568)], radius=k(18), fill=(255, 255, 255, 14),
                        outline=(255, 255, 255, 28), width=lw(1))
    PX0, PX1, PY0, PY1 = k(84), k(650), k(238), k(452)
    VY0, VY1, IY0, IY1 = k(462), k(498), k(510), k(548)
    cw = (PX1 - PX0) / 30
    cx_ = lambda i: PX0 + (i + 0.5) * cw
    cs = _candles(eth, now_px or strike or 1)
    extra = [v for v in (strike, cur.get("avg60")) if v]
    hi = max([c[2] for c in cs] + extra)
    lo = min([c[3] for c in cs] + extra)
    span = max(hi - lo, (strike or hi or 1) * 0.0008)
    mid = (hi + lo) / 2
    lo, hi = mid - span / 2 * 1.3, mid + span / 2 * 1.3
    Y = lambda v: PY1 - (v - lo) / (hi - lo) * (PY1 - PY0)

    base.paste(_logo(int(k(24))), (int(k(26)), int(k(203))), _logo(int(k(24))))
    d.text((k(58), k(215)), "ETH / USD · 30s", font=F(12, False), fill=MUTED, anchor="lm")
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        _dots(d, PX0, Y(v), PX1, Y(v), (150, 165, 200, 40), lw(1), k(2), k(5))
        d.text((PX1 + k(8), Y(v)), f"{v:,.1f}", font=F(11, False), fill=MUTED, anchor="lm")
    for m in (0, 5, 10, 15):
        x = PX0 + (PX1 - PX0) * m / 15
        if 0 < m < 15:
            _dots(d, x, PY0, x, IY1, (150, 165, 200, 28), lw(1), k(2), k(6))
        d.text((x, k(558)), f"{m}m", font=F(10, False), fill=MUTED, anchor="mm")

    wmpx = int(k(230))
    wm = _logo(wmpx).copy()
    wm.putalpha(wm.getchannel("A").point(lambda v: int(v * 0.09)))
    base.paste(wm, (int((PX0 + PX1) / 2 - wmpx / 2), int((PY0 + PY1) / 2 - wmpx / 2)), wm)
    d = ImageDraw.Draw(base, "RGBA")

    bwid = cw * 0.28

    def candle_fn(want_up):
        def fn(g):
            for (i, o, h, l, c) in cs:
                if (c >= o) != want_up:
                    continue
                x = cx_(i)
                g.line([(x, Y(h)), (x, Y(l))], fill=255, width=lw(2))
                t, b = Y(max(o, c)), Y(min(o, c))
                g.rectangle([x - bwid, t, x + bwid, max(b, t + k(2))], fill=255)
        return fn

    closes = [c[4] for c in cs]
    xs = [cx_(c[0]) for c in cs]
    e3, e8 = _ema(closes, 3), _ema(closes, 8)

    def ema_fn(vals):
        def fn(g):
            if len(xs) > 1:
                g.line([(x, Y(v)) for x, v in zip(xs, vals)], fill=255, width=lw(2.5), joint="curve")
        return fn

    _glow(base, candle_fn(True), C_UP, k(4))
    _glow(base, candle_fn(False), C_DN, k(4))
    _glow(base, ema_fn(e3), CYAN, k(3), 0.6)
    _glow(base, ema_fn(e8), ORG, k(3), 0.6)
    d = ImageDraw.Draw(base, "RGBA")

    for (i, o, h, l, c) in cs:
        cc = C_UP if c >= o else C_DN
        x = cx_(i)
        d.line([(x, Y(h)), (x, Y(l))], fill=cc + (255,), width=lw(2))
        t, b = Y(max(o, c)), Y(min(o, c))
        d.rectangle([x - bwid, t, x + bwid, max(b, t + k(2))], fill=cc + (255,))
    if len(xs) > 1:
        d.line([(x, Y(v)) for x, v in zip(xs, e3)], fill=CYAN + (255,), width=lw(2), joint="curve")
        d.line([(x, Y(v)) for x, v in zip(xs, e8)], fill=ORG + (255,), width=lw(2), joint="curve")
    d.text((PX0 + k(6), PY0 + k(6)), "EMA3", font=F(10), fill=CYAN + (255,), anchor="lm")
    d.text((PX0 + k(44), PY0 + k(6)), "EMA8", font=F(10), fill=ORG + (255,), anchor="lm")

    hc, lc_ = max(cs, key=lambda c: c[2]), min(cs, key=lambda c: c[3])
    d.text((cx_(hc[0]), max(PY0 + k(18), Y(hc[2]) - k(9))), f"H {hc[2]:,.1f}", font=F(10),
           fill=C_UP + (230,), anchor="mm")
    d.text((cx_(lc_[0]), min(PY1 - k(6), Y(lc_[3]) + k(9))), f"L {lc_[3]:,.1f}", font=F(10),
           fill=C_DN + (230,), anchor="mm")

    fmt = ".0f" if (hi - lo) >= 20 else ".1f"
    for fr in (0.2, 0.5, 0.8):
        v = lo + (hi - lo) * fr
        dd = v - (strike or v)
        c = C_UP if dd >= 0 else C_DN
        d.text((k(24), Y(v)), f"{'+' if dd >= 0 else '-'} ${abs(dd):{fmt}}", font=F(13),
               fill=c + (255,), anchor="lm")

    zx = PX0 + (PX1 - PX0) * (900 - FINAL_SECONDS) / 900
    d.rectangle([zx, PY0, PX1, IY1], fill=AMBER + (22,))
    d.text(((zx + PX1) / 2, PY0 + k(18)), "LAST 60s", font=F(10), fill=AMBER + (255,), anchor="mm")
    if cur.get("avg60"):
        ay = max(PY0, min(PY1, Y(cur["avg60"])))
        _dots(d, zx, ay, PX1, ay, AMBER + (255,), lw(2), k(4), k(3))
        d.text((zx + k(4), ay - k(8)), "AVG60", font=F(10), fill=AMBER + (255,), anchor="ls")

    ca = cur.get("called_at")
    if ca is not None and not waiting:
        ex = PX0 + (PX1 - PX0) * min(ca, 900) / 900
        _dots(d, ex, PY0, ex, PY1, col + (150,), lw(1), k(3), k(4))
        _arrow(d, ex, PY0 + k(14), is_up, k(7), col + (255,))
        d.text((ex + k(10), PY0 + k(14)), "ENTRY", font=F(10), fill=col + (230,), anchor="lm")

    acts = [(c[2] - c[3]) + 1e-9 for c in cs]
    mx = max(acts)
    for c, a in zip(cs, acts):
        cc = C_UP if c[4] >= c[1] else C_DN
        x = cx_(c[0])
        d.rectangle([x - bwid, VY1 - a / mx * (VY1 - VY0), x + bwid, VY1], fill=cc + (120,))

    d.rounded_rectangle([PX0, IY0, PX1, IY1], radius=k(6), fill=(255, 255, 255, 10))
    _dots(d, PX0, (IY0 + IY1) / 2, PX1, (IY0 + IY1) / 2, (150, 165, 200, 40), lw(1), k(2), k(5))

    def line(series, color):
        if len(series) < 2:
            return
        vs = [v for _, v in series]
        a, b = min(vs), max(vs)
        ptsl = [(PX0 + (PX1 - PX0) * min(t, 900) / 900,
                 IY1 - k(3) - (((v - a) / (b - a)) if b != a else 0.5) * (IY1 - IY0 - k(6)))
                for t, v in series]
        d.line(ptsl, fill=color + (230,), width=lw(1.5), joint="curve")

    tail = eth[-900:]
    if len(tail) >= 2:
        tt = [t for t, _ in tail]
        pp = [p for _, p in tail]
        a8, a3 = _ema(pp, 8), _ema(pp, 3)
        line(list(zip(tt, [p - (strike or pp[0]) for p in pp])), BLUE)
        line(list(zip(tt, [p - e for p, e in zip(pp, a8)])), C_DN)
        line(list(zip(tt, [x - y for x, y in zip(a3, a8)])), CYAN)
    line([(t, v) for t, v in pts[-900:]], ORG)

    lc = cs[-1]
    lcx, ly = cx_(lc[0]), Y(lc[4])
    tcol = C_UP if lc[4] >= lc[1] else C_DN
    d.rectangle([PX1 + k(2), ly - k(9), k(706), ly + k(9)], fill=tcol + (255,))
    d.text((PX1 + k(6), ly), f"{lc[4]:,.1f}", font=F(11), fill=(8, 10, 20, 255), anchor="lm")

    if accent:
        tag = {"win": "WIN", "loss": "LOSS", "settling": "SETTLING"}[outcome]
        tw = d.textlength(tag, font=F(12)) / u + 22
        d.rounded_rectangle([k(700 - tw), k(202), k(700), k(228)], radius=k(8), fill=accent + (50,),
                            outline=accent + (255,), width=lw(1.5))
        d.text((k(700 - tw / 2), k(215)), tag, font=F(12), fill=accent + (255,), anchor="mm")

    # ---- status row ----
    d.rounded_rectangle([k(24), k(586), k(696), k(650)], radius=k(18), fill=(255, 255, 255, 14))
    base.paste(_logo(int(k(40))), (int(k(36)), int(k(598))), _logo(int(k(40))))
    tx = k(92)
    if waiting:
        l1 = "Watching the market"
        l2 = "Waiting for a call"
    else:
        live = cur.get("live_conf") if outcome is None else None
        shown = live if live is not None else conf
        ent = f"{ca // 60}:{ca % 60:02d}" if ca is not None else "--"
        l1 = f"{side} call · {shown:.0f}% confidence"
        l2 = f"Entry {conf:.0f}% · called at {ent}"
        _arrow(d, k(104), k(611), is_up, k(10), col + (255,))
        tx = k(122)
    d.text((tx, k(610)), l1, font=F(17), fill=WHITE, anchor="lm")
    d.text((k(92), k(634)), l2, font=F(13, False), fill=MUTED, anchor="lm")

    # ---- indicator vote badges + confluence tally + confidence ----
    done = outcome in ("win", "loss")
    vt = (cur.get("votes_call") if done else None) or cur.get("votes") or {}
    tl = (cur.get("tally_call") if done else None) or cur.get("tally")
    cv = cur.get("live_conf") if outcome is None else None
    cshown = cv if cv is not None else conf
    grey = (90, 96, 108)
    badges = []
    for key, nm in (("mom", "MOM"), ("fib", "FIB"), ("ema", "EMA"), ("boll", "BOLL"),
                    ("atr", "ATR"), ("macd", "MACD")):
        v = (vt.get(key) or [None])[0]
        bc = C_UP if v == 1 else C_DN if v == -1 else grey
        lab = "UP" if v == 1 else "DOWN" if v == -1 else ("NEUTRAL" if v == 0 else "WAIT")
        badges.append((nm, lab, bc, v, bool(v)))
    if tl and sum(tl) > 0:
        tu, td = tl[0], tl[1]
        tv = 1 if tu > td else -1 if td > tu else 0
        badges.append(("TALLY", f"{tu}U {td}D", C_UP if tv == 1 else C_DN if tv == -1 else grey,
                       tv, bool(tv)))
    else:
        badges.append(("TALLY", "WAIT", grey, None, False))
    if waiting:
        badges.append(("CONF", "--", grey, None, False))
    else:
        badges.append(("CONF", f"{cshown:.0f}%", col, 1 if is_up else -1, True))
    gap = 5
    nb = len(badges)
    bwn = (720 - 48 - (nb - 1) * gap) / nb
    for i, (nm, lab, bc, v, solid) in enumerate(badges):
        x0 = k(24 + i * (bwn + gap))
        x1 = x0 + k(bwn)
        y0, y1 = k(662), k(716)
        d.rounded_rectangle([x0, y0, x1, y1], radius=k(11), fill=bc + (255 if solid else 150,),
                            outline=(255, 255, 255, 70), width=lw(1.5))
        if v:
            _arrow(d, x0 + k(10), (y0 + y1) / 2, v == 1, k(6), (255, 255, 255, 255))
        d.text(((x0 + x1) / 2 + k(5), y0 + k(18)), nm, font=F(10, False),
               fill=(255, 255, 255, 215), anchor="mm")
        d.text(((x0 + x1) / 2 + k(5), y0 + k(38)), lab, font=F(12),
               fill=(255, 255, 255, 255), anchor="mm")

    # ---- buttons ----
    by0, by1 = k(730), k(834)
    bwd = (720 - 48 - 14) / 2
    for i, (name, pct, bc, up_btn) in enumerate((("Up", up_pct, C_UP, True), ("Down", dn_pct, C_DN, False))):
        x0 = k(24 + i * (bwd + 14))
        x1 = x0 + k(bwd)
        called = (not waiting) and (up_btn == is_up)
        dim = (not waiting) and not called
        ocol = (accent or (255, 255, 255)) if called else None
        d.rounded_rectangle([x0, by0, x1, by1], radius=k(30), fill=bc + (75 if dim else 255,),
                            outline=(ocol + (255,)) if ocol else None, width=lw(3))
        ta = 150 if dim else 255
        _arrow(d, x0 + k(44), (by0 + by1) / 2, up_btn, k(15), (255, 255, 255, ta))
        shown_pct = pct if waiting else (cshown if up_btn == is_up else 100 - cshown)
        d.text(((x0 + x1) / 2 + k(14), by0 + k(38)), f"{name} · {shown_pct:.0f}%", font=F(24),
               fill=(255, 255, 255, ta), anchor="mm")
        d.text(((x0 + x1) / 2 + k(14), by0 + k(74)), f"{100 / max(pct, 1):.2f}x", font=F(16, False),
               fill=(255, 255, 255, 120 if dim else 225), anchor="mm")

    # ---- animated frames ----
    ty = Y(strike) if strike else (PY0 + PY1) / 2
    frames = []
    for f in range(n):
        t = f / n if n > 1 else 0.25
        sn = math.sin(2 * math.pi * t)
        im = base.copy()
        fd = ImageDraw.Draw(im, "RGBA")
        _dots(fd, k(24), ty, PX1, ty, TGT + (210,), lw(1.5), k(6), k(5), phase=t * k(11))
        fd.text((PX0 + k(16), ty - k(9)), "TARGET", font=F(11), fill=TGT + (255,), anchor="ls")
        tw2 = fd.textlength("TARGET", font=F(11))
        _arrow(fd, PX0 + k(16) + tw2 + k(10), ty - k(13), False, k(5), TGT + (255,))
        r = k(8 + 1.8 * sn)
        fd.ellipse([lcx - r, ly - r, lcx + r, ly + r], outline=tcol + (255,), width=lw(2))
        fd.ellipse([lcx - k(3), ly - k(3), lcx + k(3), ly + k(3)], fill=(255, 255, 255, 255))
        fd.rectangle([lcx - cw * 0.5, Y(lc[2]) - k(4), lcx + cw * 0.5, Y(lc[3]) + k(4)],
                     outline=tcol + (int(100 + 80 * sn),), width=lw(1))
        if outcome in ("win", "loss"):
            fd.rounded_rectangle([k(3), k(3), Wd - k(4), Hd - k(4)], radius=k(24),
                                 outline=accent + (int(110 + 90 * sn),), width=lw(4))
        if SS > 1:
            im = im.resize((outW, outH), Image.LANCZOS)
        frames.append(im)
    return frames


def chart_file(cur, outcome=None):
    """(bytes, filename, mime). Live = animated GIF, win/loss = PNG. Shrinks if too big."""
    animated = outcome not in ("win", "loss")
    S = LIVE_SCALE if animated else FINAL_SCALE
    while True:
        frames = render_frames(cur, outcome, S, LIVE_FRAMES if animated else 1)
        buf = io.BytesIO()
        if not animated:
            frames[0].save(buf, format="PNG", optimize=True)
            res = (buf.getvalue(), "chart.png", "image/png")
        else:
            frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:],
                           duration=150, loop=0)
            res = (buf.getvalue(), "chart.gif", "image/gif")
        if len(res[0]) <= MAX_BYTES or S <= 1:
            return res
        S *= 0.75


# ---------- discord ----------
def build_embed(c, state, yes, price, n_ex, secs_left, outcome=None, blend=None, pm=None):
    side = c["side"]
    p_up = blend if blend is not None else yes
    s = stats(state["history"])
    fields = [
        {"name": "All-time", "value": s["all"], "inline": True},
    ]

    if side is None:
        mom_now = abs((c.get("sig") or {}).get("mom", 0))
        lines = [
            f"**Call:** ⚪ NO CALL (momentum {mom_now * 100:.0f}, needs {MOM_MIN * 100:.0f}+ held for {CONFIRM_SECONDS:.1f}s)",
            "**Action:** WAIT, no position",
            f"**Kalshi** YES {yes:.0f}% / NO {100 - yes:.0f}%",
        ]
        title, color = "⚪ KALSHI ETH 15m: NO CALL", GREY
    else:
        arrow = "🆙" if side == "UP" else "⬇️"
        conf = c.get("conf") or (p_up if side == "UP" else 100 - p_up)
        if outcome == "win":
            tag, color = "✅ WIN", GREEN
        elif outcome == "loss":
            tag, color = "❌ LOSS", RED
        elif outcome == "settling":
            tag, color = "⏳ SETTLING", GREY
        else:
            tag, color = "HOLD", GREEN if side == "UP" else RED
        if outcome:
            action = tag
        elif c.get("flip_t") and time.time() - c["flip_t"] < 30:
            action = f"SELL {c.get('flip_from')} → BUY {side}"
        elif c.get("flipped"):
            action = "SELL"
        elif not c.get("buy_shown"):
            action = "BUY"
        else:
            action = "HOLD"
        fl = fib_level(c)
        fib_txt = f" | Fibonacci retracement at {fl}% + trend" if fl and not outcome else ""
        lines = [
            f"**Call:** {arrow} {side} ({conf:.0f}% confidence at entry){fib_txt} | momentum is {momentum_level(c)}",
        ]
        if not outcome:
            live_conf = c.get("live_conf")
            if live_conf is None:
                live_conf = conf
            lines.append(f"**Updated confidence:** {live_conf:.0f}% ({live_conf - conf:+.0f} vs entry)")
        lines += [
            f"**Action:** {action}",
            f"**Kalshi** YES {yes:.0f}% / NO {100 - yes:.0f}%",
        ]
        title = f"{arrow} KALSHI ETH 15m: {side} | {action}"
    if pm is not None:
        lines.append(f"**Strike model** UP {pm:.0f}% / DOWN {100 - pm:.0f}%")
    if c.get("avg_exp") is not None and (secs_left <= FINAL_SECONDS or outcome):
        lines.append("**Last minute** Kalshi settles on the 60s average, not the final tick")
        if c.get("avg60") is not None:
            lines.append(f"**60s average so far** ${c['avg60']:,.2f} | projected ${c['avg_exp']:,.2f} "
                         f"vs strike ${c['strike']:,.2f}")
        if c.get("avg_need") is not None and (c.get("avg_rem") or 0) >= 1:
            lines.append(f"**To finish UP** the last {c['avg_rem']:.0f}s must average "
                         f"${c['avg_need']:,.2f}+")
    mk = c.get("mkt")
    if mk:
        arrows = " / ".join(f"{sym} {'▲' if z >= 0 else '▼'}" for sym, z in mk["z"].items())
        lines.append(f"**Basket** {arrows} (agreement {mk['breadth'] * 100:.0f}%)")

    sg = c.get("sig")
    if sg:
        fl = c.get("fib")
        fscore = sg.get("fib", 0.0)
        if fl:
            fib_part = f"Fibonacci retracement {fl}% (score {fscore:+.2f}, {fib_bias(fscore)})"
        else:
            fib_part = "Fibonacci forming (not enough swing yet)"
        rs = c.get("rsi")
        rsi_part = f" | RSI {rs:.0f}" if rs is not None else " | RSI warming up"
        lines.append(f"**Signals** {fib_part}{rsi_part}")

    done = outcome in ("win", "loss")
    vt = (c.get("votes_call") if done else None) or c.get("votes")
    tally = (c.get("tally_call") if done else None) or c.get("tally")
    if vt and tally:
        names = {"mom": "Momentum", "fib": "Fibonacci", "ema": "EMA 9/21", "boll": "Bollinger",
                 "atr": "ATR", "macd": "MACD"}
        lab = lambda v: "⚪ WARMING" if v is None else "🟢 UP" if v > 0 else "🔴 DOWN" if v < 0 else "⚪ NEUTRAL"
        lines.append("**Indicators at entry**" if done and c.get("votes_call") else "**Indicators**")
        for key, nm in names.items():
            if key not in vt:
                continue
            v, txt = vt[key]
            lines.append(f"{lab(v)} · **{nm}** {txt}")
        u, d_, n_ = tally
        cb = c.get("comb_call") if done and c.get("comb_call") is not None else c.get("comb")
        verdict = f" → **{side}**" if side else " → no call yet"
        lines.append(f"**Confluence** UP {u} / DOWN {d_} / NEUTRAL {n_}{verdict}")
        if cb is not None:
            lines.append(f"**Combined score** {cb:+.2f} (call at ±{COMBO_MIN:.2f})")

    if side is None and c.get("rsi_block"):
        why = c.get("hold_why") or ""
        cap = "" if "Fibonacci" in why else f" (max {RSI_GRACE:.0f}s)"
        lines.append(f"**Hold** {c['rsi_block']} call waiting on {why}{cap}")
    elif side is not None and c.get("fib_call") is not None:
        over = [n for n, f in (("RSI", c.get("rsi_over")), ("Fibonacci", c.get("fib_over"))) if f]
        if over:
            lines.append(f"**Entry** called despite {' and '.join(over)}")
        elif c.get("rsi_call") is None:
            lines.append("**Entry** Fibonacci agreed (RSI warming up)")
        else:
            lines.append("**Entry** RSI and Fibonacci agreed")
    if c.get("flips"):
        lines.append(f"**Flips** {c['flips']} this window")
    if price:
        lines.append(f"**Price** ${price:,.2f} (median of {n_ex} exch)")
    lines.append(f"**Strike** {c['strike']:,.2f} | {max(int(secs_left), 0)}s left")

    return {
        "title": title,
        "description": "\n".join(lines),
        "color": color,
        "fields": fields,
        "footer": {"text": "Signals only, not financial advice"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


_build_embed_raw = build_embed


def build_embed(*a, **kw):
    """Same embed, minus anything that hints at how a call is triggered."""
    e = _build_embed_raw(*a, **kw)
    out = []
    for ln in e["description"].split("\n"):
        if ln.startswith("**Call:** ⚪ NO CALL"):
            ln = "**Call:** ⚪ NO CALL yet"
        elif ln.startswith("**Call:**"):
            ln = ln.split(" | ")[0]
        out.append(ln)
    e["description"] = "\n".join(out)
    return e


HTTP = requests.Session()  # LIVE: reuse connections for Discord


def send(embed, message_id=None, cur=None, outcome=None, text_only=False):
    payload = {"embeds": [embed]}
    f_data = None
    if text_only and message_id:
        embed["image"] = {"url": "attachment://chart.gif"}
    elif cur is not None:
        f_data = chart_file(cur, outcome)
        embed["image"] = {"url": f"attachment://{f_data[1]}"}
        if message_id:
            payload["attachments"] = []
    for _ in range(3):
        kw = {"timeout": 60}
        if f_data:
            kw["files"] = {"files[0]": (f_data[1], f_data[0], f_data[2])}
            kw["data"] = {"payload_json": json.dumps(payload)}
        else:
            kw["json"] = payload
        if message_id:
            r = HTTP.patch(f"{WEBHOOK}/messages/{message_id}", **kw)
        else:
            r = HTTP.post(WEBHOOK, params={"wait": "true"}, **kw)
        if r.status_code == 429:
            time.sleep(r.json().get("retry_after", 1) + 0.1)
            continue
        r.raise_for_status()
        return r.json()["id"]
    raise RuntimeError("discord rate limited")


SEND_LOCK = threading.Lock()


def send_sync(embed, message_id=None, cur=None, outcome=None):
    with SEND_LOCK:
        if cur is not None and cur.get("message_id"):
            message_id = cur["message_id"]
        return send(embed, message_id, cur=cur, outcome=outcome)


def send_async(embed, message_id, cur, store=None, text_only=False):
    if not SEND_LOCK.acquire(blocking=False):
        return False
    snap = None if text_only else copy.deepcopy(cur)

    def run():
        try:
            mid = send(embed, message_id, cur=snap, text_only=text_only)
            if store is not None:
                store["message_id"] = mid
        except Exception as e:
            print("send error", e)
        finally:
            SEND_LOCK.release()

    threading.Thread(target=run, daemon=True).start()
    return True


# ---------- core ----------
def settle_pending(state):
    for p in list(state["pending"]):
        try:
            m = get_market(p["ticker"])
            res = m.get("result")
            if res not in ("yes", "no"):
                if time.time() < p["close_ts"] + 300:
                    continue
                if (p.get("avg_exp") is not None and p.get("avg_rem") is not None
                        and p["avg_rem"] <= 5):
                    res = "yes" if p["avg_exp"] >= p["strike"] else "no"
                else:
                    price, _ = eth_price()
                    if price is None:
                        continue
                    res = "yes" if price >= p["strike"] else "no"
            win = (res == "yes") == (p["side"] == "UP")
            state["history"].append({"ticker": p["ticker"], "side": p["side"], "win": win,
                                     "flips": p.get("flips", 0),
                                     "calls": p.get("calls"),
                                     "tilt": (p.get("mkt") or {}).get("tilt"),
                                     "sig": p.get("sig")})
            state["pending"].remove(p)
            yes_final = 100.0 if res == "yes" else 0.0
            p.setdefault("pts", []).append([900, 100 if res == "yes" else 0])
            send_sync(build_embed(p, state, yes_final, None, 0, 0, "win" if win else "loss"),
                      p["message_id"], cur=p, outcome="win" if win else "loss")
            _save_now(state)  # LIVE: always persist a settled result immediately
        except Exception as e:
            print("settle error", e)


def flip_call(cur, new_side, comb, r, fib, elapsed, now, src):
    cur.setdefault("calls", []).append(
        {"side": cur["side"], "conf": cur.get("conf"), "at": cur.get("called_at")})
    cur.update(flip_from=cur["side"], side=new_side,
               conf=combo_conf(new_side, comb, cur["votes"]),
               called_at=round(elapsed), src=src,
               flips=cur.get("flips", 0) + 1, flip_t=now,
               flipped=False, buy_shown=False,
               fib_call=round(fib, 2), rsi_call=cur["rsi"],
               rsi_over=False, fib_over=False,
               votes_call=copy.deepcopy(cur["votes"]),
               tally_call=list(cur["tally"]), comb_call=cur["comb"])
    cur.pop("flip_since", None)
    cur.pop("ff_t", None)
    cur.pop("rev_t", None)


def tick(state):
    settle_pending(state)
    m = open_market()
    cur = state["current"]
    now = time.time()

    if cur and (m is None or m["ticker"] != cur["ticker"]):
        state["current"] = None
        if cur["side"] is None:
            last_yes = cur["pts"][-1][1] if cur["pts"] else 50
            send_sync(build_embed(cur, state, last_yes, None, 0, 0), cur["message_id"], cur=cur)
        else:
            state["pending"].append(cur)
            send_sync(build_embed(cur, state, 50, None, 0, 0, "settling"), cur["message_id"],
                      cur=cur, outcome="settling")
        cur = None
        _save_now(state)
    if m is None:
        return

    close_ts = ts(m["close_time"])
    secs_left = close_ts - now
    elapsed = 900 - secs_left
    yes = yes_pct(m)
    px = all_prices()
    for s, (p, _) in px.items():
        record_price(s, p)
    price, n_ex = px["ETH"]
    strike = float(m.get("floor_strike") or m.get("cap_strike") or 0)

    if cur is None:
        cur = {"ticker": m["ticker"], "side": None, "strike": strike, "close_ts": close_ts,
               "message_id": None, "pts": [], "eth": [], "locked": False, "conf": None}
        state["current"] = cur
    cur.setdefault("open", {})
    cur.setdefault("open_t", now)
    for s, (p, _) in px.items():
        if p:
            cur["open"].setdefault(s, p)
    now_px = {s: p for s, (p, _) in px.items()}
    tilt, breadth, zs = market_signal(now_px, cur["open"], now - cur["open_t"])
    final = secs_left <= FINAL_SECONDS
    ss = settle_stats(price, strike, close_ts, now, tilt)
    blend, pm = predict(yes, ss, secs_left)
    if ss:
        cur["avg60"] = round(ss["obs_avg"], 2) if ss["obs_avg"] else None
        cur["avg_exp"] = round(ss["exp_avg"], 2)
        cur["avg_rem"] = round(ss["rem"], 1)
        cur["avg_need"] = round(ss["need"], 2) if ss["need"] is not None else None
    cur["mkt"] = {"tilt": round(tilt, 2), "breadth": round(breadth, 2),
                  "z": {s: round(z, 2) for s, z in zs.items()}}
    cur["read"] = {"ETH": blend}

    series = series_10s("ETH")
    fib, fib_lvl = fib_signal(series)
    trend = trend_signal(series, tilt)
    r = rsi(series)
    cur["rsi"] = round(r, 1) if r is not None else None
    rsi_s = max(-1.0, min(1.0, (r - 50) / 50)) if r is not None else 0.0
    mom = 0.7 * momentum_fast("ETH") + 0.3 * math.tanh(tilt / 1.5)
    who = "buyers" if mom > 0.1 else "sellers" if mom < -0.1 else "even"
    score, prob = composite(mom, fib, trend, blend, rsi_s)
    if final and ss:
        lock = max(0.0, min(1.0, (FINAL_SECONDS - secs_left) / FINAL_SECONDS))
        score = (1 - lock) * score + lock * max(-1.0, min(1.0, (ss["pm"] - 50) / 50))
    cur["score"] = round(score, 3)
    cur["fib"] = fib_lvl
    cur["sig"] = {"mom": round(mom, 2), "fib": round(fib, 2), "trend": round(trend, 2),
                  "prob": round(prob, 2)}
    prev_side, prev_flip = cur["side"], cur.get("flipped")

    # ---- indicator votes + combined call ----
    votes = {"ema": vote_ema(series), "boll": vote_boll(series),
             "atr": vote_atr(series, price, strike, secs_left, ss), "macd": vote_macd(series)}
    ready = [v for v, _ in votes.values() if v is not None]
    all_ready = len(ready) == 4
    c_up, c_dn = ready.count(1), ready.count(-1)
    vscore = sum(ready) / len(ready) if ready else 0.0
    comb = W_CORE * score + W_VOTE * vscore
    cur["votes"] = {k: [v, t] for k, (v, t) in votes.items()}
    cur["tally"] = [c_up, c_dn, len(ready) - c_up - c_dn]
    cur["votes"]["mom"] = [1 if mom >= MOM_MIN else -1 if mom <= -MOM_MIN else 0,
                           f"{mom:+.2f}, {momentum_level(cur)} ({who})"]
    cur["votes"]["fib"] = [None if fib_lvl is None else 1 if fib >= FIB_AGREE else -1 if fib <= -FIB_AGREE else 0,
                           f"retrace {fib_lvl}% ({fib:+.2f})" if fib_lvl else "forming"]
    cur["comb"] = round(comb, 3)

    # ---- initial call ----
    if CALL_MODE == "combined" and all_ready and cur["side"] is None and secs_left > CALL_CUTOFF:
        cside = combo_side(comb, ready, fib, r)
        if cside is None:
            cur.pop("comb_dir", None)
        elif cur.get("comb_dir") != cside:
            cur["comb_dir"], cur["comb_t"] = cside, now
        elif now - cur["comb_t"] >= CONFIRM_SECONDS:
            cur.update(side=cside, conf=combo_conf(cside, comb, cur["votes"]), locked=True,
                       called_at=round(elapsed), src="COMBINED",
                       votes_call=copy.deepcopy(cur["votes"]), tally_call=list(cur["tally"]),
                       comb_call=cur["comb"])

    if cur["side"] is None and secs_left > CALL_CUTOFF and (CALL_MODE == "kalshi" or not all_ready):
        if not cur.get("first_side"):
            fs0 = pct_trigger(yes)
            if fs0:
                cur["first_side"], cur["first_t"] = fs0, now
        fs = cur.get("first_side")
        if fs:
            rsi_agree = rsi_ok(fs, r)
            fib_agree = fib_ok(fs, fib)
            waited = now - cur["first_t"]
            rsi_pass = rsi_agree or waited >= RSI_GRACE
            fib_pass = fib_agree
            if rsi_pass and fib_pass:
                cur.pop("rsi_block", None)
                cur.pop("hold_why", None)
                cur.update(side=fs, conf=combo_conf(fs, comb, cur["votes"]), locked=True,
                           called_at=round(elapsed), src="KALSHI",
                           rsi_call=cur["rsi"], fib_call=round(fib, 2),
                           rsi_over=not rsi_agree, fib_over=not fib_agree,
                           votes_call=copy.deepcopy(cur["votes"]), tally_call=list(cur["tally"]),
                           comb_call=cur["comb"])
            else:
                cur["rsi_block"] = fs
                cur["hold_why"] = " and ".join(
                    n for n, ok in (("RSI", rsi_pass), ("Fibonacci", fib_pass)) if not ok)

    if cur["side"] is None and CALL_AFTER <= elapsed and secs_left > CALL_CUTOFF:
        lead = 1 if mom >= MOM_MIN else -1 if mom <= -MOM_MIN else 0
        fm = cur.get("first")
        if not lead:
            fm = None
        elif fm is None or fm["dir"] != lead:
            fm = {"dir": lead, "t": now}
        cur["first"] = fm
        if fm and now - fm["t"] >= CONFIRM_SECONDS:
            pick = pick_call(score, mom, fib, trend, prob)
            if pick:
                side, _ = pick
                cur.update(side=side, conf=combo_conf(side, comb, cur["votes"]), locked=True,
                           called_at=round(elapsed), src="ETH",
                           votes_call=copy.deepcopy(cur["votes"]), tally_call=list(cur["tally"]),
                           comb_call=cur["comb"])

    # ---- SELL = the call trigger, firing for the opposite side ----
    if (CALL_MODE == "combined" and all_ready and cur["side"] and cur["side"] == prev_side
            and secs_left > CALL_CUTOFF and cur.get("flips", 0) < REV_MAX):
        cside = combo_side(comb, ready, fib, r)
        if cside is None or cside == cur["side"]:
            cur.pop("sell_dir", None)
        elif cur.get("sell_dir") != cside:
            cur["sell_dir"], cur["sell_t"] = cside, now
        elif now - cur["sell_t"] >= CONFIRM_SECONDS:
            flip_call(cur, cside, comb, r, fib, elapsed, now, "SELL_FLIP")
            cur.pop("sell_dir", None)

    # ---- last minute ----
    if final and ss and secs_left > FINAL_CUTOFF:
        up_p, cand = ss["pm"], None
        if cur["side"] is None:
            if up_p >= FINAL_CALL_PCT:
                cand = "UP"
            elif 100 - up_p >= FINAL_CALL_PCT:
                cand = "DOWN"
        elif cur.get("flips", 0) < REV_MAX:
            opp = "DOWN" if cur["side"] == "UP" else "UP"
            if (up_p if opp == "UP" else 100 - up_p) >= FINAL_FLIP_PCT:
                cand = opp
        if cand is None:
            cur.pop("final_dir", None)
        elif cur.get("final_dir") != cand:
            cur["final_dir"], cur["final_t"] = cand, now
        elif now - cur["final_t"] >= FINAL_HOLD:
            cp = up_p if cand == "UP" else 100 - up_p
            if cur["side"] is None:
                cur.update(side=cand, conf=cp, locked=True, called_at=round(elapsed),
                           src="FINAL_AVG", votes_call=copy.deepcopy(cur["votes"]),
                           tally_call=list(cur["tally"]), comb_call=cur["comb"])
            else:
                flip_call(cur, cand, comb, r, fib, elapsed, now, "FINAL_FLIP")
                cur["conf"] = cp
            cur.pop("final_dir", None)

    if cur["side"]:
        if final and ss:
            sp = ss["pm"] if cur["side"] == "UP" else 100 - ss["pm"]
            cur["live_conf"] = round(max(1.0, min(99.0, sp)), 1)
        else:
            cur["live_conf"] = round(combo_conf(cur["side"], comb, cur["votes"]), 1)

    cur["price"] = price
    if price:
        cur.setdefault("eth", []).append([round(elapsed), round(price, 2)])
    cur["pts"].append([round(elapsed), round(yes, 1)])

    # ---- post a new message on events, otherwise refresh the latest one ----
    old_who, cur["who"] = cur.get("who"), who
    since_post = now - cur.get("last_post", 0)
    ev = None
    if cur["side"] and not prev_side:
        ev = f"New call: {cur['side']}"
    elif prev_side and cur["side"] != prev_side:
        label = {"SELL_FLIP": "Sell flip", "REV_FLIP": "Reversal flip",
                 "FINAL_FLIP": "Last-minute flip"}.get(cur.get("src"), "Fast flip")
        ev = f"{label}: SELL {prev_side} → BUY {cur['side']} (confidence {cur['conf']:.0f}%)"
    elif cur.get("flipped") and not prev_flip:
        ev = "Signal flipped against the call"
    elif old_who and who != old_who and who != "even" and since_post >= MIN_GAP:
        ev = f"Momentum shift: {who} gaining"
    ev = ev or cur.get("pending_ev")
    new_due = cur["message_id"] is None or ev is not None or since_post >= NEW_MSG_SECONDS
    edit_due = now - cur.get("last_edit", 0) >= EDIT_SECONDS
    if new_due or edit_due:
        embed = build_embed(cur, state, yes, price, n_ex, secs_left, blend=blend, pm=pm)
        if ev:
            embed["description"] = f"**Update:** {ev}\n" + embed["description"]
        text_only = (not new_due) and (now - cur.get("last_img", 0) < IMG_SECONDS)
        if send_async(embed, None if new_due else cur["message_id"], cur,
                      cur if new_due else None, text_only):
            cur.pop("pending_ev", None)
            if new_due:
                cur["last_post"] = now
            if not text_only:
                cur["last_img"] = now
            cur["last_edit"] = now
            if cur["side"]:
                cur["buy_shown"] = True
        elif ev:
            cur["pending_ev"] = ev
    save_state(state)


# =====================================================================
# LIVE LAYER: push feeds, throttles and local dashboard
# =====================================================================
KALSHI_POLL = float(os.getenv("KALSHI_POLL", "0.1"))
DASH_PORT = int(os.getenv("DASH_PORT", "8000"))
DASH_HZ = float(os.getenv("DASH_HZ", "30"))
KEY_ID = os.getenv("KALSHI_KEY_ID")
KEY_PATH = os.getenv("KALSHI_KEY_PATH")

FEED = {"m": None, "t": 0.0}
LATEST = {}
STATE = {"s": None}
FLOCK = threading.Lock()


def run_ws(name, url, on_open, on_msg, headers=None):
    def loop():
        while True:
            try:
                ws = websocket.WebSocketApp(
                    url, header=headers() if headers else None,
                    on_open=on_open, on_message=on_msg,
                    on_error=lambda w, e: print(name, "error", e))
                ws.run_forever(ping_interval=15, ping_timeout=8)
            except Exception as e:
                print(name, "crashed", e)
            time.sleep(1)
    threading.Thread(target=loop, daemon=True).start()


def start_price_feeds():
    def cb_open(ws):
        ws.send(json.dumps({"type": "subscribe", "product_ids": ["ETH-USD", "BTC-USD"],
                            "channels": ["ticker"]}))

    def cb_msg(ws, m):
        d = json.loads(m)
        if d.get("type") == "ticker" and d.get("price"):
            LATEST[(d["product_id"][:3], "cb")] = (float(d["price"]), time.time())

    def kr_open(ws):
        ws.send(json.dumps({"method": "subscribe",
                            "params": {"channel": "ticker", "symbol": ["ETH/USD", "BTC/USD"]}}))

    def kr_msg(ws, m):
        d = json.loads(m)
        if d.get("channel") == "ticker":
            for x in d.get("data", []):
                LATEST[(x["symbol"].split("/")[0], "kr")] = (float(x["last"]), time.time())

    def bs_open(ws):
        for p in ("ethusd", "btcusd"):
            ws.send(json.dumps({"event": "bts:subscribe",
                                "data": {"channel": f"live_trades_{p}"}}))

    def bs_msg(ws, m):
        d = json.loads(m)
        if d.get("event") == "trade":
            sym = d["channel"].replace("live_trades_", "")[:3].upper()
            LATEST[(sym, "bs")] = (float(d["data"]["price"]), time.time())

    run_ws("coinbase", "wss://ws-feed.exchange.coinbase.com", cb_open, cb_msg)
    run_ws("kraken", "wss://ws.kraken.com/v2", kr_open, kr_msg)
    run_ws("bitstamp", "wss://ws.bitstamp.net", bs_open, bs_msg)


def cached_prices():
    now, out = time.time(), {}
    for sym in ("ETH", "BTC"):
        v = [p for (s, _), (p, t) in list(LATEST.items()) if s == sym and now - t < 10]
        out[sym] = (statistics.median(v), len(v)) if v else (None, 0)
    return out


_open_market_rest = open_market


def kalshi_rest():
    m = None
    while True:
        t0 = time.time()
        try:
            if m is None or ts(m["close_time"]) <= time.time():
                m = _open_market_rest()
            if m:
                m = {**m, **get_market(m["ticker"])}
                with FLOCK:
                    FEED["m"], FEED["t"] = m, time.time()
        except Exception as e:
            print("kalshi rest error", e)
        time.sleep(max(0.0, (1.0 if KEY_ID else KALSHI_POLL) - (time.time() - t0)))


def _ws_headers():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    path = "/trade-api/ws/v2"
    with open(KEY_PATH, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    t = str(int(time.time() * 1000))
    sig = key.sign((t + "GET" + path).encode(),
                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                               salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": KEY_ID, "KALSHI-ACCESS-TIMESTAMP": t,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}


def kalshi_ws():
    sub = {"tk": None}

    def on_open(ws):
        with FLOCK:
            m = FEED["m"]
        if m:
            sub["tk"] = m["ticker"]
            ws.send(json.dumps({"id": 1, "cmd": "subscribe",
                                "params": {"channels": ["ticker"],
                                           "market_tickers": [m["ticker"]]}}))

    def on_msg(ws, raw):
        msg = json.loads(raw)
        if msg.get("type") != "ticker":
            return
        d = msg.get("msg", {})
        with FLOCK:
            if not FEED["m"]:
                return
            if sub["tk"] and FEED["m"]["ticker"] != sub["tk"]:
                ws.close()
                return
            cur = dict(FEED["m"])
            for k in ("yes_bid", "yes_ask", "yes_bid_dollars", "yes_ask_dollars"):
                if k in d:
                    cur[k] = d[k]
            if "price" in d:
                cur["last_price"] = d["price"]
            FEED["m"], FEED["t"] = cur, time.time()

    while FEED["m"] is None:
        time.sleep(0.2)
    run_ws("kalshi", "wss://api.elections.kalshi.com/trade-api/ws/v2",
           on_open, on_msg, headers=_ws_headers)


def feed_market():
    with FLOCK:
        m = FEED["m"]
    return m if m and ts(m["close_time"]) > time.time() else None


# ---- throttles so disk and settlement calls never slow a tick ----
_settle_orig, _save_orig = settle_pending, save_state
_last = {"settle": 0.0, "save": 0.0}


def _save_now(state):
    _last["save"] = time.time()
    _save_orig(state)


def settle_throttled(state):
    if time.time() - _last["settle"] >= 3:
        _last["settle"] = time.time()
        _settle_orig(state)


def save_throttled(state):
    if time.time() - _last["save"] >= 1:
        _save_now(state)


# ---- local live dashboard ----
PAGE = """<!doctype html><meta charset=utf-8><title>ETH 15m live</title>
<style>
*{box-sizing:border-box}
body{margin:0;height:100vh;display:flex;flex-direction:column;color:#fff;font-family:system-ui,sans-serif;
 background:linear-gradient(180deg,#0d1f1f,#2d1a1f 60%,#0a0a0a)}
#top{display:flex;gap:26px;padding:14px 20px 6px;align-items:baseline;flex-wrap:wrap}
#clk{font-size:36px;font-variant-numeric:tabular-nums;font-weight:700}
#bar{display:flex;gap:8px;padding:6px 20px 10px;flex-wrap:wrap}
.b{flex:1 1 90px;min-width:90px;border-radius:12px;padding:7px 8px 8px;text-align:center;
 border:1.5px solid rgba(255,255,255,.28);background:rgba(90,96,108,.55);transition:background .15s}
.b.up{background:#00d98e}.b.dn{background:#ff5757}
.b small{display:block;font-size:11px;opacity:.85;letter-spacing:.5px}
.b b{display:block;font-size:15px;margin-top:3px}
#info{padding:0 20px 6px;font-size:13px;color:#a0a8b2}
canvas{flex:1;width:100%;min-height:0}
</style>
<div id=top><b style="font-size:22px">ETH 15 min</b><span id=clk></span>
<span>Now <b id=px style="font-size:28px"></b></span><span>Target <b id=tg></b></span>
<span id=diff></span><span id=kal></span><span id=call></span></div>
<div id=bar></div><div id=info></div>
<canvas id=c></canvas>
<script>
const c=document.getElementById('c'),g=c.getContext('2d'),$=i=>document.getElementById(i);
const NAMES=[['mom','MOM'],['fib','FIB'],['ema','EMA'],['boll','BOLL'],['atr','ATR'],['macd','MACD']];
const bar=$('bar'),els={};
[...NAMES,['tally','TALLY'],['conf','CONF']].forEach(([k,n])=>{
 const d=document.createElement('div');d.className='b';d.innerHTML='<small>'+n+'</small><b>WAIT</b>';
 bar.appendChild(d);els[k]=d});
function setB(k,v,label,tip){
 const e=els[k];e.className='b'+(v===1?' up':v===-1?' dn':'');
 e.lastChild.textContent=(v===1?'▲ ':v===-1?'▼ ':'')+label;e.title=tip||''}
let D=null,off=0,hist=[];const WIN=120000;
const es=new EventSource('/stream');
es.onmessage=e=>{D=JSON.parse(e.data);off=D.now*1000-Date.now();
 if(D.price)hist.push([D.now*1000,D.price]);
 const cut=D.now*1000-WIN;while(hist.length&&hist[0][0]<cut)hist.shift();
 const V=D.votes||{};
 NAMES.forEach(([k])=>{const x=V[k]||[null,''],v=x[0];
  setB(k,v,v===1?'UP':v===-1?'DOWN':v===0?'NEUTRAL':'WAIT',x[1])});
 const t=D.tally;
 if(t&&(t[0]+t[1]+t[2])>0){const tv=t[0]>t[1]?1:t[1]>t[0]?-1:0;setB('tally',tv,t[0]+'U '+t[1]+'D','Confluence')}
 else setB('tally',null,'WAIT','');
 if(D.side)setB('conf',D.side==='UP'?1:-1,Math.round(D.conf||0)+'%','Live confidence');
 else setB('conf',null,'--','');
 $('info').textContent='Combined '+(D.comb==null?'--':(D.comb>=0?'+':'')+D.comb.toFixed(2))
  +' | RSI '+(D.rsi==null?'--':D.rsi.toFixed(0))
  +' | Fib '+(D.fib||'--')+(D.fib?'%':'')
  +' | Flips '+(D.flips||0)
  +(D.avg60?' | 60s avg $'+D.avg60.toFixed(2):'')};
function draw(){
 requestAnimationFrame(draw);if(!D)return;
 const r=devicePixelRatio||1,W=c.clientWidth*r,H=c.clientHeight*r;
 if(c.width!=W||c.height!=H){c.width=W;c.height=H}
 const now=Date.now()+off,left=Math.max(0,D.close_ts*1000-now);
 $('clk').textContent=Math.floor(left/60000)+':'+String(Math.floor(left/1000)%60).padStart(2,'0')+'.'+String(Math.floor(left%1000)).padStart(3,'0');
 $('px').textContent=D.price?'$'+D.price.toFixed(2):'--';
 $('tg').textContent=D.strike?'$'+D.strike.toFixed(2):'--';
 const df=D.price-D.strike;$('diff').textContent=(df>=0?'▲ $':'▼ $')+Math.abs(df).toFixed(2);
 $('diff').style.color=df>=0?'#00d98e':'#ff5757';
 $('kal').textContent='Kalshi YES '+D.yes.toFixed(0)+'% / NO '+(100-D.yes).toFixed(0)+'%';
 $('call').textContent=D.side?D.side+' call '+Math.round(D.conf||0)+'%':'No call yet';
 g.clearRect(0,0,W,H);if(hist.length<2)return;
 const t1=now,t0=now-WIN,vals=hist.map(h=>h[1]).concat(D.strike?[D.strike]:[]);
 let lo=Math.min(...vals),hi=Math.max(...vals),pad=(hi-lo||1)*.15;lo-=pad;hi+=pad;
 const X=t=>(t-t0)/(t1-t0)*(W-90*r),Y=v=>H-(v-lo)/(hi-lo)*H;
 if(D.strike){g.setLineDash([8*r,6*r]);g.strokeStyle='#cdd2dc';g.beginPath();g.moveTo(0,Y(D.strike));g.lineTo(W,Y(D.strike));g.stroke();g.setLineDash([])}
 g.strokeStyle=df>=0?'#00d98e':'#ff5757';g.lineWidth=2*r;g.beginPath();
 hist.forEach((h,i)=>{i?g.lineTo(X(h[0]),Y(h[1])):g.moveTo(X(h[0]),Y(h[1]))});g.stroke();
}
draw();
</script>"""


def dash_payload(m):
    st = STATE["s"] or {}
    cur = st.get("current") or {}
    out = {"now": time.time(), "price": cached_prices()["ETH"][0],
           "strike": float(m.get("floor_strike") or m.get("cap_strike") or 0),
           "close_ts": ts(m["close_time"]), "yes": yes_pct(m),
           "side": cur.get("side"),
           "conf": cur.get("live_conf") or cur.get("conf"),
           "votes": cur.get("votes"), "tally": cur.get("tally"),
           "comb": cur.get("comb"), "rsi": cur.get("rsi"), "fib": cur.get("fib"),
           "flips": cur.get("flips", 0), "avg60": cur.get("avg60")}
    return json.dumps(out, default=str)


class Dash(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path != "/stream":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            while True:
                m = feed_market()
                if m:
                    try:
                        data = dash_payload(m)
                        self.wfile.write(f"data: {data}\n\n".encode())
                        self.wfile.flush()
                    except RuntimeError:
                        pass  # state changed mid-read; next frame is fine
                time.sleep(1 / DASH_HZ)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


# ---- swap in the live versions (tick reads these globals at call time) ----
open_market = feed_market
all_prices = cached_prices
settle_pending = settle_throttled
save_state = save_throttled


def main():
    threading.Thread(target=kalshi_rest, daemon=True).start()
    start_price_feeds()
    if KEY_ID and KEY_PATH:
        threading.Thread(target=kalshi_ws, daemon=True).start()
    else:
        print("No Kalshi key set: polling Kalshi every", KALSHI_POLL, "s instead of push")
    srv = ThreadingHTTPServer(("127.0.0.1", DASH_PORT), Dash)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"Live dashboard: http://localhost:{DASH_PORT}")

    while feed_market() is None or not LATEST:
        time.sleep(0.05)
    state = load_state()
    STATE["s"] = state
    end = time.time() + RUN_SECONDS
    nxt = time.time()
    while time.time() < end:
        try:
            tick(state)
        except Exception as e:
            print("tick error", e)
        nxt += POLL
        d = nxt - time.time()
        if d > 0:
            time.sleep(d)
        else:
            nxt = time.time()


if __name__ == "__main__":
    main()
