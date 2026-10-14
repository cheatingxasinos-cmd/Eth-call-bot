"""Kalshi ETH 15m signal bot v3.1 (single file).
  python bot.py                 live
  python bot.py replay FILE     backtest recorded ticks (no Discord)
  python bot.py fit             fit the probability blend on samples.jsonl
  python bot.py report [STATE]  ROI, calibration, baselines
  python bot.py audit           list recent calls with Kalshi results
"""
import base64, functools, io, json, logging, math, multiprocessing, os, random, statistics, sys, threading, time
from collections import deque
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

try:
    import websocket
except ImportError:
    websocket = None

try:
    from zoneinfo import ZoneInfo
    CHART_TZ = ZoneInfo(os.getenv("CHART_TZ", "America/New_York"))
except Exception:
    CHART_TZ = timezone.utc

log = logging.getLogger("bot")


# ======================================================================
# CONFIG (every value overridable by env var)
# ======================================================================
def _e(name, default):
    v = os.getenv(name)
    if v is None:
        return default
    if isinstance(default, int):
        return int(float(v))
    if isinstance(default, float):
        return float(v)
    return v


class C:
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "")
    series = _e("KALSHI_SERIES", "KXETH15M")
    poll = _e("POLL_SECONDS", 0.5)
    edit_s = _e("EDIT_SECONDS", 1.0)
    img_s = _e("IMG_SECONDS", 1.0)
    new_msg_s = _e("NEW_MSG_SECONDS", 30.0)
    run_s = _e("RUN_SECONDS", 345 * 60)
    save_s = _e("SAVE_SECONDS", 2.0)
    state_file = _e("STATE_FILE", "state.json")
    windows_file = _e("WINDOWS_FILE", "windows.jsonl")
    tick_file = _e("TICK_FILE", "ticks.jsonl")
    record = _e("RECORD", 1)
    stale_s = _e("STALE_SECONDS", 5.0)
    price_max_age = _e("PRICE_MAX_AGE", 45.0)   # exchange price counts for this long
    min_exch = _e("MIN_EXCH", 2)                # no new calls with fewer feeds
    min_conf = _e("MIN_CONF", 55.0)
    max_jump = _e("MAX_JUMP", 0.015)            # ignore single-tick jumps bigger than 1.5%
    fresh_stats = _e("FRESH_STATS", 0)          # 1 = archive old win/loss record at startup
    wide_spread = _e("WIDE_SPREAD", 10.0)
    abstain = _e("ABSTAIN", 0.04)
    instant = _e("INSTANT_CALL", 1)
    call_cutoff = max(_e("CALL_CUTOFF", 60), 60)
    confirm_s = _e("CONFIRM_SECONDS", 3.0)
    lock_pm = _e("LOCK_PM", 55.0)
    tilt_w = _e("TILT_W", 0.25)
    mom_min = _e("MOM_MIN", 0.10)
    fib_agree = _e("FIB_AGREE", 0.15)
    rsi_n = _e("RSI_N", 14)
    rsi_block = _e("RSI_BLOCK", 20.0)
    combo_min = _e("COMBO_MIN", 0.40)
    combo_opp = _e("COMBO_OPP", 0)
    conf_full = _e("CONF_FULL", 0.6)
    conf_vote_w = _e("CONF_VOTE_W", 0.4)
    ema_dead = _e("EMA_DEAD", 0.00005)
    macd_dead = _e("MACD_DEAD", 0.00002)
    bb_up = _e("BB_UP", 0.65)
    bb_dn = _e("BB_DN", 0.35)
    atr_n = _e("ATR_N", 14)
    atr_z = _e("ATR_Z", 0.5)
    w_market = _e("W_MARKET", 0.5)
    w_market_final = _e("W_MARKET_FINAL", 0.35)
    w_core = _e("W_CORE", 0.5)
    w_vote = _e("W_VOTE", 0.5)
    w_mom, w_rsi = _e("W_MOM", 0.35), _e("W_RSI", 0.10)
    w_fib, w_trend, w_prob = _e("W_FIB", 0.35), _e("W_TREND", 0.10), _e("W_PROB", 0.10)
    w_i_pm, w_i_mkt, w_i_mom = _e("W_I_PM", 0.25), _e("W_I_MKT", 0.25), _e("W_I_MOM", 0.20)
    w_i_trend, w_i_fib, w_i_comb = _e("W_I_TREND", 0.15), _e("W_I_FIB", 0.10), _e("W_I_COMB", 0.05)
    final_call_pct = _e("FINAL_CALL_PCT", 70.0)
    final_hold = _e("FINAL_HOLD", 2.0)
    final_cutoff = _e("FINAL_CUTOFF", 5)
    # edge-based calling
    min_edge = _e("MIN_EDGE", 1.0)        # cents of expected value after fee; -99 = always call
    min_p = _e("MIN_P", 0.62)
    edge_hold = _e("EDGE_HOLD", 2.0)
    call_max = _e("CALL_MAX", 150.0)      # only call in the last N seconds; 900 = whole window
    calib_file = _e("CALIB_FILE", "calib.json")
    samples_file = _e("SAMPLES_FILE", "samples.jsonl")
    sample_s = _e("SAMPLE_SECONDS", 10.0)
    dash_port = _e("DASH_PORT", 8000)
    dash_hz = _e("DASH_HZ", 30.0)
    kalshi_poll = _e("KALSHI_POLL", 0.5)
    key_id = os.getenv("KALSHI_KEY_ID")
    key_path = os.getenv("KALSHI_KEY_PATH")
    logo_path = os.getenv("LOGO_PATH", "eth_logo.png")
    live_scale = _e("LIVE_SCALE", 1.0)
    final_scale = _e("FINAL_SCALE", 3.0)
    max_bytes = int(_e("MAX_MB", 7.5) * 1024 * 1024)
    live_frames = _e("LIVE_FRAMES", 4)
    timer_lead = _e("TIMER_LEAD", 0.7)


KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
FINAL_SECONDS = 60
FIB_LEVELS = (0.236, 0.382, 0.5, 0.618, 0.786)
GREEN, RED, GREY = 0x00FF00, 0xFF3B30, 0x808080

LOCK = threading.RLock()
SEND_LOCK = threading.Lock()
CLOCK = {"t": None}


def now():
    return CLOCK["t"] if CLOCK["t"] is not None else time.time()


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ======================================================================
# KALSHI HELPERS
# ======================================================================
SESSION = requests.Session()


def kget(path, **params):
    r = SESSION.get(KALSHI + path, params=params or None, timeout=10)
    r.raise_for_status()
    return r.json()


def ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def fetch_open_market():
    ms = kget("/markets", series_ticker=C.series, status="open", limit=20).get("markets", [])
    t = time.time()
    ms = [m for m in ms if ts(m["close_time"]) > t]
    return min(ms, key=lambda m: ts(m["close_time"])) if ms else None


def fetch_market(ticker):
    return kget(f"/markets/{ticker}")["market"]


def cents(m, key):
    if m.get(key) is not None:
        return float(m[key])
    if m.get(key + "_dollars") is not None:
        return float(m[key + "_dollars"]) * 100
    return None


def quote(m):
    """Mid price, falling back to the last trade when the spread is wide."""
    bid, ask, last = cents(m, "yes_bid"), cents(m, "yes_ask"), cents(m, "last_price")
    if bid is not None and ask is not None and ask > 0:
        mid, spread = (bid + ask) / 2, ask - bid
        if spread > C.wide_spread and last is not None:
            mid = clamp(last, bid, ask)
        return {"yes": mid, "bid": bid, "ask": ask, "spread": spread}
    return {"yes": last if last is not None else 50.0, "bid": None, "ask": None, "spread": None}


def entry_price(q, side):
    """What a buyer actually pays, in cents."""
    if side == "UP":
        return q["ask"] if q["ask"] is not None else q["yes"]
    return 100 - q["bid"] if q["bid"] is not None else 100 - q["yes"]


# ======================================================================
# LIVE FEEDS
# ======================================================================
QUOTE_KEYS = ("yes_bid", "yes_ask", "yes_bid_dollars", "yes_ask_dollars",
              "last_price", "last_price_dollars")


def run_ws(name, url, on_open, on_msg, headers=None, holder=None):
    def loop():
        backoff = 1
        while True:
            t0 = time.time()
            try:
                ws = websocket.WebSocketApp(
                    url, header=headers() if headers else None, on_open=on_open,
                    on_message=on_msg, on_error=lambda w, e: log.warning("%s error: %s", name, e))
                if holder is not None:
                    holder["ws"] = ws
                ws.run_forever(ping_interval=15, ping_timeout=8)
            except Exception:
                log.exception("%s crashed", name)
            backoff = 1 if time.time() - t0 > 30 else min(backoff * 2, 30)
            time.sleep(backoff)
    threading.Thread(target=loop, daemon=True).start()


class LiveFeed:
    def __init__(self):
        self.lock = threading.Lock()
        self.m, self.t, self.ws_t = None, 0.0, 0.0
        self.latest = {}

    def market(self):
        with self.lock:
            m, t = self.m, self.t
        if m and ts(m["close_time"]) > time.time():
            return m, time.time() - t
        return None, 0.0

    def ticker(self):
        with self.lock:
            return self.m["ticker"] if self.m else None

    def result(self, ticker):
        r = fetch_market(ticker).get("result")
        return r if r in ("yes", "no") else None

    def apply_rest(self, m):
        with self.lock:
            old = self.m
            if old and old["ticker"] == m["ticker"] and time.time() - self.ws_t < 3:
                for k in QUOTE_KEYS:       # websocket is fresher: keep its quotes
                    if k in old:
                        m[k] = old[k]
                    else:
                        m.pop(k, None)
            self.m, self.t = m, time.time()

    def apply_ws(self, d):
        with self.lock:
            if not self.m:
                return
            cur = dict(self.m)
            for base in ("yes_bid", "yes_ask"):
                if base in d or base + "_dollars" in d:
                    cur.pop(base, None)
                    cur.pop(base + "_dollars", None)
                    for k in (base, base + "_dollars"):
                        if k in d:
                            cur[k] = d[k]
            if "price" in d:
                cur["last_price"] = d["price"]
                cur.pop("last_price_dollars", None)
            self.m, self.t, self.ws_t = cur, time.time(), time.time()

    def rest_loop(self):
        m, fails = None, 0
        while True:
            t0 = time.time()
            try:
                if m is None or ts(m["close_time"]) <= t0:
                    m = fetch_open_market()
                if m:
                    m = {**m, **fetch_market(m["ticker"])}
                    self.apply_rest(dict(m))
                fails = 0
            except Exception as e:
                fails += 1
                log.warning("kalshi rest error: %s", e)
            period = 1.0 if C.key_id else C.kalshi_poll
            wait = min(30.0, period * 2 ** fails) if fails else period
            time.sleep(max(0.0, wait - (time.time() - t0)))

    def prices(self):
        t, out = time.time(), {}
        for sym in ("ETH", "BTC"):
            v = [p for (s, _), (p, ts_) in list(self.latest.items())
                 if s == sym and t - ts_ < C.price_max_age]
            if len(v) >= 3:
                med = statistics.median(v)
                v = [p for p in v if abs(p / med - 1) < 0.005]   # drop outlier exchange
            out[sym] = (statistics.median(v), len(v)) if v else (None, 0)
        return out

    def start_price_feeds(self):
        L = self.latest

        def cb_open(ws):
            ws.send(json.dumps({"type": "subscribe", "product_ids": ["ETH-USD", "BTC-USD"],
                                "channels": ["ticker"]}))

        def cb_msg(ws, m):
            try:
                d = json.loads(m)
                if d.get("type") == "ticker" and d.get("price"):
                    L[(d["product_id"][:3], "cb")] = (float(d["price"]), time.time())
            except Exception:
                pass

        def kr_open(ws):
            ws.send(json.dumps({"method": "subscribe", "params": {
                "channel": "ticker", "symbol": ["ETH/USD", "BTC/USD"]}}))

        def kr_msg(ws, m):
            try:
                d = json.loads(m)
                if d.get("channel") == "ticker":
                    for x in d.get("data", []):
                        L[(x["symbol"].split("/")[0], "kr")] = (float(x["last"]), time.time())
            except Exception:
                pass

        def bs_open(ws):
            for p in ("ethusd", "btcusd"):
                ws.send(json.dumps({"event": "bts:subscribe", "data": {"channel": f"live_trades_{p}"}}))

        def bs_msg(ws, m):
            try:
                d = json.loads(m)
                if d.get("event") == "trade":
                    sym = d["channel"].replace("live_trades_", "")[:3].upper()
                    L[(sym, "bs")] = (float(d["data"]["price"]), time.time())
            except Exception:
                pass

        run_ws("coinbase", "wss://ws-feed.exchange.coinbase.com", cb_open, cb_msg)
        run_ws("kraken", "wss://ws.kraken.com/v2", kr_open, kr_msg)
        run_ws("bitstamp", "wss://ws.bitstamp.net", bs_open, bs_msg)

    def start_kalshi_ws(self):
        sub, holder = {"tk": None}, {}

        def headers():
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            path = "/trade-api/ws/v2"
            with open(C.key_path, "rb") as f:
                key = serialization.load_pem_private_key(f.read(), password=None)
            t = str(int(time.time() * 1000))
            sig = key.sign((t + "GET" + path).encode(),
                           padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                       salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
            return {"KALSHI-ACCESS-KEY": C.key_id, "KALSHI-ACCESS-TIMESTAMP": t,
                    "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}

        def on_open(ws):
            tk = self.ticker()
            if tk:
                sub["tk"] = tk
                ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {
                    "channels": ["ticker"], "market_tickers": [tk]}}))

        def on_msg(ws, raw):
            try:
                msg = json.loads(raw)
                if msg.get("type") == "ticker":
                    self.apply_ws(msg.get("msg", {}))
            except Exception:
                log.exception("kalshi ws message")

        def watcher():                      # resubscribe at window rollover
            while True:
                time.sleep(1)
                ws, tk = holder.get("ws"), self.ticker()
                if ws and tk and sub["tk"] and tk != sub["tk"]:
                    try:
                        ws.close()
                    except Exception:
                        pass

        while self.ticker() is None:
            time.sleep(0.2)
        run_ws("kalshi", "wss://api.elections.kalshi.com/trade-api/ws/v2",
               on_open, on_msg, headers=headers, holder=holder)
        threading.Thread(target=watcher, daemon=True).start()


class ReplayFeed:
    def __init__(self, path):
        with open(path) as f:
            self.rows = [json.loads(x) for x in f if x.strip()]
        self.row = None
        self.results = {}
        by = {}
        for r in self.rows:
            by.setdefault(r["m"]["ticker"], []).append(r)
        for tk, rs in by.items():
            m = rs[0]["m"]
            close = ts(m["close_time"])
            strike = float(m.get("floor_strike") or m.get("cap_strike") or 0)
            v = [r["px"]["ETH"][0] for r in rs
                 if close - 60 <= r["t"] <= close and r["px"].get("ETH") and r["px"]["ETH"][0]]
            if v and strike:
                self.results[tk] = "yes" if statistics.fmean(v) >= strike else "no"

    def step(self, row):
        self.row = row
        CLOCK["t"] = row["t"]

    def market(self):
        m = self.row["m"]
        return (m, 0.0) if ts(m["close_time"]) > now() else (None, 0.0)

    def prices(self):
        return {s: (v[0], v[1]) for s, v in self.row["px"].items()}

    def result(self, ticker):
        return self.results.get(ticker) if now() > ts(self.row["m"]["close_time"]) else None


class Recorder:
    KEYS = ("ticker", "close_time", "floor_strike", "cap_strike", "yes_bid", "yes_ask",
            "yes_bid_dollars", "yes_ask_dollars", "last_price", "last_price_dollars")

    def __init__(self, path):
        self.f, self.last = open(path, "a", buffering=1 << 16), 0.0

    def write(self, m, px):
        row = {"t": round(now(), 3), "m": {k: m[k] for k in self.KEYS if k in m},
               "px": {s: [p, n] for s, (p, n) in px.items()}}
        self.f.write(json.dumps(row, separators=(",", ":")) + "\n")
        if time.time() - self.last > 5:
            self.f.flush()
            self.last = time.time()


REC = None


# ======================================================================
# PRICE HISTORY AND MATH
# ======================================================================
PRICES = {s: deque(maxlen=6000) for s in ("ETH", "BTC")}
WEIGHTS = {"ETH": 0.5, "BTC": 0.5}
DEF_SIG = {"ETH": 1.1e-4, "BTC": 0.9e-4}
LASTGOOD = {}


def sanitize(px):
    """Hold the last good price when a feed jumps more than MAX_JUMP; n=0 pauses calls."""
    out, t = {}, now()
    for s, (p, n) in px.items():
        g = LASTGOOD.get(s)
        if p and g and abs(p / g[0] - 1) > C.max_jump and t - g[1] < 15:
            p, n = g[0], 0
        elif p:
            LASTGOOD[s] = (p, t)
        out[s] = (p, n)
    return out


def record_price(sym, price):
    """Store only changes, so held quotes do not fake zero returns."""
    d = PRICES[sym]
    if price and (not d or d[-1][1] != price):
        d.append((now(), price))


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def series_10s(sym="ETH", n=90, step=10):
    pts = list(PRICES[sym])
    if not pts:
        return []
    end = now()
    out, j, last = [], 0, None
    for i in range(n):
        tgt = end - step * (n - 1 - i)
        while j < len(pts) and pts[j][0] <= tgt:
            last = pts[j][1]
            j += 1
        if last is not None:
            out.append(last)
    return out


def vol(sym):
    pts = list(PRICES[sym])
    rets = [math.log(p1 / p0) / math.sqrt(t1 - t0)
            for (t0, p0), (t1, p1) in zip(pts, pts[1:]) if t1 - t0 > 0.05]
    base = DEF_SIG[sym]
    if len(rets) < 8:
        return base
    s = math.sqrt(sum(r * r for r in rets) / len(rets))
    return clamp(s, base * 0.4, base * 4)


def z60(sym):
    pts = list(PRICES[sym])
    if len(pts) < 2:
        return 0.0
    t_now, p_now = now(), pts[-1][1]
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
    zs = {}
    for s in WEIGHTS:
        z = 0.6 * z60(s) + 0.4 * z_open(s, prices.get(s), opens.get(s), since_open)
        zs[s] = clamp(z, -3.0, 3.0)
    basket = sum(WEIGHTS[s] * zs[s] for s in WEIGHTS)
    agree = sum(WEIGHTS[s] * (0 if abs(zs[s]) < 0.15 else (1 if zs[s] > 0 else -1)) for s in WEIGHTS)
    breadth = abs(agree) / sum(WEIGHTS.values())
    return basket * (0.5 + 0.5 * breadth), breadth, zs


def window_avg(sym, close_ts, t):
    """Time-weighted sum and seconds already inside the final 60s window."""
    start, end = close_ts - FINAL_SECONDS, min(t, close_ts)
    pts = list(PRICES[sym])
    if end <= start or not pts:
        return 0.0, 0.0
    px, inside = None, []
    for tt, p in pts:
        if tt <= start:
            px = p
        elif tt <= end:
            inside.append((tt, p))
    if px is None:
        px = inside[0][1] if inside else pts[-1][1]
    total, t0 = 0.0, start
    for tt, p in inside:
        total += px * (tt - t0)
        t0, px = tt, p
    total += px * (end - t0)
    return total, end - start


def settle_stats(price, strike, close_ts, t, tilt=0.0):
    """Odds that the 60s average finishes at or above strike."""
    if not price or not strike:
        return None
    tau0 = max(close_ts - t, 0.0)
    total, e = window_avg("ETH", close_ts, t)
    rem = min(tau0, float(FINAL_SECONDS))
    a, b = max(tau0 - FINAL_SECONDS, 0.0), tau0
    exp_avg = (total + price * rem) / FINAL_SECONDS
    V = -(b ** 3 - a ** 3) / 6 - a * a * (b - a) / 2 + b * (b * b - a * a) / 2
    std = price * vol("ETH") * math.sqrt(max(V, 0.0)) / FINAL_SECONDS
    fade = min(1.0, math.sqrt(tau0 / 300)) * rem / FINAL_SECONDS
    shift = C.tilt_w * clamp(tilt, -3.0, 3.0) * fade
    if std <= 1e-9:
        pm = 99.0 if exp_avg >= strike else 1.0
    else:
        pm = clamp(norm_cdf((exp_avg - strike) / std + shift) * 100, 1.0, 99.0)
    return {"pm": pm, "exp_avg": exp_avg, "std": std, "rem": rem,
            "obs_avg": (total / e) if e > 1 else None,
            "need": ((strike * FINAL_SECONDS - total) / rem) if rem > 0.5 else None}


def predict(yes, ss, secs_left):
    if ss is None:
        return yes, None
    w = C.w_market_final if secs_left <= FINAL_SECONDS else C.w_market
    return w * yes + (1 - w) * ss["pm"], ss["pm"]


# ======================================================================
# PROBABILITY AND EDGE-BASED CALLING
# ======================================================================
def logit(p):
    p = clamp(p, 0.005, 0.995)
    return math.log(p / (1 - p))


def sigmoid(x):
    return 1 / (1 + math.exp(-clamp(x, -30, 30)))


CAL = {"a": 0.5, "b": 0.5, "c": 0.0, "fitted": False}


def load_calib():
    try:
        with open(C.calib_file) as f:
            CAL.update(json.load(f))
        CAL["fitted"] = True
        log.info("calibration loaded: %s", CAL)
    except FileNotFoundError:
        log.info("no calib file, using time-weighted prior")
    except Exception:
        log.exception("calib load")


def p_up(yes, ss, secs_left):
    if not ss:
        return None
    if CAL["fitted"]:
        a, b, c = CAL["a"], CAL["b"], CAL["c"]
    else:                                   # prior: trust the model more as the window closes
        a = 0.5 + 0.5 * clamp(1 - secs_left / 300, 0.0, 1.0)
        b, c = 1 - a, 0.0
    return sigmoid(a * logit(ss["pm"] / 100) + b * logit(yes / 100) + c)


def edge_for(side, pu, q):
    """(win prob, expected value in cents after fee, entry price in cents)."""
    ep = entry_price(q, side)
    ps = pu if side == "UP" else 1 - pu
    fee = 7 * (ep / 100) * (1 - ep / 100)        # approx Kalshi taker fee per contract, cents
    return ps, ps * 100 - ep - fee, ep


def maybe_call(cur, ctx, q, secs_left, elapsed, t):
    pu = p_up(q["yes"], ctx["ss"], secs_left)
    if pu is None:
        return
    cur["p_up"] = round(pu, 4)
    best = None
    for side in ("UP", "DOWN"):
        ps, edge, _ = edge_for(side, pu, q)
        if ps >= C.min_p and edge >= C.min_edge and (best is None or edge > best[1]):
            best = (side, edge, ps)
    if best is None:
        cur.pop("edge_dir", None)
        return
    side, edge, ps = best
    if cur.get("edge_dir") != side:
        cur["edge_dir"], cur["edge_t"] = side, t
    elif t - cur["edge_t"] >= C.edge_hold:
        cur["edge"] = round(edge, 2)
        make_call(cur, side, ps * 100, "EDGE", elapsed, q)


# ======================================================================
# INDICATORS (display only, they do not decide calls)
# ======================================================================
def _ema(vals, n):
    a = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(out[-1] + a * (v - out[-1]))
    return out


def _fib_score(d):
    for lim, s in ((0.236, 1.0), (0.382, 0.7), (0.5, 0.5), (0.618, 0.2), (0.786, -0.3)):
        if d < lim:
            return s
    return -0.8


def fib_signal(prices):
    if len(prices) < 8:
        return 0.0, None
    hi, lo, last = max(prices), min(prices), prices[-1]
    if hi - lo < last * 0.0003:
        return 0.0, None
    up_swing = prices.index(lo) < len(prices) - 1 - prices[::-1].index(hi)
    d = (hi - last) / (hi - lo) if up_swing else (last - lo) / (hi - lo)
    d = clamp(d, 0.0, 1.0)
    lvl = min(FIB_LEVELS, key=lambda r: abs(r - d))
    return (1 if up_swing else -1) * _fib_score(d), f"{lvl * 100:.1f}".rstrip("0").rstrip(".")


def trend_signal(prices, tilt):
    ema = 0.0
    if len(prices) >= 8:
        e3, e8 = _ema(prices, 3)[-1], _ema(prices, 8)[-1]
        ema = math.tanh(math.log(e3 / e8) / (vol("ETH") * math.sqrt(30)))
    return 0.5 * ema + 0.5 * math.tanh(tilt / 1.5)


def rsi(prices, n=None):
    n = n or C.rsi_n
    if len(prices) < n + 1:
        return None
    ch = [b - a for a, b in zip(prices, prices[1:])]
    ag = sum(max(c, 0) for c in ch[:n]) / n
    al = sum(max(-c, 0) for c in ch[:n]) / n
    for c in ch[n:]:
        ag = (ag * (n - 1) + max(c, 0)) / n
        al = (al * (n - 1) + max(-c, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def vote_ema(p):
    if len(p) < 21:
        return None, "warming up"
    e9, e21 = _ema(p, 9)[-1], _ema(p, 21)[-1]
    gap = (e9 - e21) / p[-1]
    v = 1 if gap > C.ema_dead else -1 if gap < -C.ema_dead else 0
    return v, f"EMA9 {'above' if e9 > e21 else 'below'} EMA21 ({gap * 1e4:+.1f}bp)"


def vote_boll(p, n=20, k=2.0):
    if len(p) < n:
        return None, "warming up"
    w = p[-n:]
    m, sd = statistics.fmean(w), statistics.pstdev(w)
    if sd == 0:
        return 0, "flat"
    pb = (p[-1] - (m - k * sd)) / (2 * k * sd)
    return (1 if pb >= C.bb_up else -1 if pb <= C.bb_dn else 0), f"%B {pb:.2f}"


def atr(p, n=None):
    n = n or C.atr_n
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
        v = 1 if z >= C.atr_z else -1 if z <= -C.atr_z else 0
        return v, f"60s avg proj ${ss['exp_avg']:,.2f}, {z:+.1f}σ from strike"
    a = atr(p)
    if a is None or not price or not strike or a <= 0:
        return None, "warming up"
    z = (price - strike) / (a * math.sqrt(max(secs_left, 10) / 10))
    v = 1 if z >= C.atr_z else -1 if z <= -C.atr_z else 0
    return v, f"ATR ${a:.2f}, {z:+.1f}x reach from strike"


def vote_macd(p):
    if len(p) < 35:
        return None, "warming up"
    line = [a - b for a, b in zip(_ema(p, 12), _ema(p, 26))]
    sig = _ema(line, 9)
    hist, prev = line[-1] - sig[-1], line[-2] - sig[-2]
    dead = p[-1] * C.macd_dead
    v = 1 if hist > dead else -1 if hist < -dead else 0
    return v, f"hist {hist:+.2f} ({'rising' if hist > prev else 'falling'})"


def _pressure(p):
    ups = sum(max(b - a, 0) for a, b in zip(p, p[1:]))
    dns = sum(max(a - b, 0) for a, b in zip(p, p[1:]))
    return (ups - dns) / (ups + dns) if ups + dns > 0 else 0.0


def momentum_fast(sym="ETH", win=10):
    pts = list(PRICES[sym])
    if len(pts) < 6:
        return 0.0
    t_now = now()
    cur = [p for t, p in pts if t_now - t <= win]
    prev = [p for t, p in pts if win < t_now - t <= 2 * win]
    if len(cur) < 4:
        return 0.0
    p_now = _pressure(cur)
    gain = (p_now - _pressure(prev)) / 2 if len(prev) >= 4 else 0.0
    return clamp(0.65 * p_now + 0.35 * gain, -1.0, 1.0)


def composite(mom, fib, trend, blend, rsi_s=0.0):
    prob = clamp((blend - 50) / 50, -1.0, 1.0)
    return (C.w_mom * mom + C.w_fib * fib + C.w_trend * trend + C.w_prob * prob + C.w_rsi * rsi_s), prob


# ======================================================================
# STATE, STATS, REPORT, AUDIT
# ======================================================================
LAST_SAVE = {"t": 0.0}


def load_state():
    for path in (C.state_file, C.state_file + ".tmp"):
        try:
            with open(path) as f:
                s = json.load(f)
            s.setdefault("history", [])
            s.setdefault("history_old", [])
            s.setdefault("pending", [])
            s.setdefault("current", None)
            return s
        except FileNotFoundError:
            continue
        except Exception:
            log.exception("could not read %s", path)
    return {"history": [], "history_old": [], "current": None, "pending": []}


def _slim(c):
    if not c:
        return None
    d = dict(c)
    for k in ("eth", "pts"):
        d[k] = c.get(k, [])[::4]
    return d


def save_state(state, force=False):
    if not force and time.time() - LAST_SAVE["t"] < C.save_s:
        return
    LAST_SAVE["t"] = time.time()
    with LOCK:
        state["history"] = state["history"][-500:]
        blob = json.dumps({"history": state["history"],
                           "history_old": state.get("history_old", [])[-2000:],
                           "pending": [_slim(p) for p in state["pending"]],
                           "current": _slim(state["current"])}, separators=(",", ":"), default=str)
    tmp = C.state_file + ".tmp"
    with open(tmp, "w") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, C.state_file)


def log_window(row):
    try:
        with open(C.windows_file, "a") as f:
            f.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
    except Exception:
        log.exception("window log")


def snapshot(c):
    s = dict(c)
    s["eth"], s["pts"] = list(c.get("eth", [])), list(c.get("pts", []))
    return s


def stats(h):
    def wl(xs):
        n, w = len(xs), sum(1 for x in xs if x["win"])
        return f"{w}W-{n - w}L ({w / n * 100:.0f}%)" if n else "n/a"
    priced = [x for x in h if x.get("entry_px")]
    cost = sum(x["entry_px"] for x in priced)
    pnl = sum((100 - x["entry_px"]) if x["win"] else -x["entry_px"] for x in priced)
    roi = f"{pnl / cost * 100:+.1f}% on {len(priced)}" if cost else "n/a"
    return {"all": wl(h), "last": wl(h[-50:]), "roi": roi}


def report(history):
    n = len(history)
    if not n:
        print("no history")
        return
    wins = sum(1 for h in history if h["win"])
    print(f"calls {n}, win rate {wins / n * 100:.1f}%")
    print("ROI per contract (before fees):", stats(history)["roi"])
    conf = [h for h in history if h.get("conf") is not None]
    if conf:
        brier = statistics.fmean((h["conf"] / 100 - (1 if h["win"] else 0)) ** 2 for h in conf)
        print(f"Brier score {brier:.3f} (0.250 = coin flip)")
        print("confidence bucket: calls, claimed, actual")
        for lo in range(50, 100, 10):
            b = [h for h in conf if lo <= h["conf"] < lo + 10]
            if b:
                print(f"  {lo}-{lo + 9}%: {len(b):4d}  {statistics.fmean(h['conf'] for h in b):5.1f}%  "
                      f"{sum(1 for h in b if h['win']) / len(b) * 100:5.1f}%")
    base = [h for h in history if h.get("yes_entry") is not None]
    if base:
        def up_won(h):
            return h["win"] == (h["side"] == "UP")
        fav = sum(1 for h in base if (h["yes_entry"] >= 50) == up_won(h))
        up = sum(1 for h in base if up_won(h))
        print(f"baselines on {len(base)} calls: follow Kalshi favorite {fav / len(base) * 100:.1f}%, "
              f"always UP {up / len(base) * 100:.1f}%")


def audit(n=30):
    try:
        with open(C.windows_file) as f:
            rows = [json.loads(x) for x in f if x.strip()]
    except FileNotFoundError:
        print("no windows file yet; it logs windows settled since the v2 upgrade")
        return
    called = [r for r in rows if r.get("side")]
    bad = [r for r in called if r.get("win") != ((r["res"] == "yes") == (r["side"] == "UP"))]
    print(f"{len(rows)} windows logged, {len(called)} with a call, {len(bad)} inconsistent")
    for r in called[-n:]:
        when = datetime.fromtimestamp(r["close_ts"]).strftime("%m-%d %H:%M")
        print(f"{when} {r['ticker']} call {r['side']:4s} result {r['res']:3s} "
              f"{'WIN ' if r['win'] else 'LOSS'} entry {r.get('entry_px')}c conf {r.get('conf')}")
    print("Check a few of these tickers against Kalshi's own results page.")


# ======================================================================
# FITTING (optional: replaces the prior with measured weights)
# ======================================================================
def _fit(rows, iters=500, lr=1.0, l2=1e-3):
    X = [(logit(r["pm"] / 100), logit(r["yes"] / 100), 1.0) for r in rows]
    w = [0.5, 0.5, 0.0]
    for _ in range(iters):
        g = [0.0, 0.0, 0.0]
        for x, r in zip(X, rows):
            e = sigmoid(sum(a * b for a, b in zip(w, x))) - r["y"]
            for i in range(3):
                g[i] += e * x[i]
        w = [wi - lr * (gi / len(rows) + (l2 * wi if i < 2 else 0)) for i, (wi, gi) in enumerate(zip(w, g))]
    return w


def _brier(rows, fn):
    return statistics.fmean((fn(r) - r["y"]) ** 2 for r in rows)


def fit(test_frac=0.3):
    with open(C.samples_file) as f:
        rows = [json.loads(x) for x in f if x.strip()]
    rows = [r for r in rows if 5 < r["left"] <= 880]
    tks = list(dict.fromkeys(r["tk"] for r in rows))
    if len(tks) < 100:
        print(f"only {len(tks)} windows; collect 100+ (ideally 500+) before trusting a fit")
        return
    cut = set(tks[:int(len(tks) * (1 - test_frac))])      # split by window, oldest first
    tr = [r for r in rows if r["tk"] in cut]
    te = [r for r in rows if r["tk"] not in cut]
    a, b, c = _fit(tr)
    bm = _brier(te, lambda r: r["yes"] / 100)
    bs = _brier(te, lambda r: r["pm"] / 100)
    bf = _brier(te, lambda r: sigmoid(a * logit(r["pm"] / 100) + b * logit(r["yes"] / 100) + c))
    print(f"held-out Brier: market {bm:.4f} | strike model {bs:.4f} | fitted blend {bf:.4f}")
    if bf < bm - 0.0005:
        a, b, c = _fit(rows)
        out = {"a": a, "b": b, "c": c}
        print("blend beats the market out of sample, saved", out)
    else:
        out = {"a": 0.0, "b": 1.0, "c": 0.0}
        print("no measurable edge over the Kalshi price; saved pure-market weights (no calls)")
    with open(C.calib_file, "w") as f:
        json.dump(out, f)


# ======================================================================
# CHART RENDERER (runs in a separate process)
# ======================================================================
CW, CH = 720, 860
WHITE = (255, 255, 255, 255)
MUTED = (160, 168, 178, 255)
C_UP, C_DN, ORG = (0, 217, 142), (255, 87, 87), (255, 140, 58)
CYAN, BLUE, AMBER = (0, 229, 255), (80, 120, 255), (255, 190, 40)
TGT = (205, 210, 220)

_FONT_DIRS = ["", "/usr/share/fonts/truetype/inter/", "/usr/share/fonts/truetype/roboto/",
              "/usr/share/fonts/truetype/liberation/", "/usr/share/fonts/truetype/liberation2/",
              "/usr/share/fonts/truetype/noto/", "/usr/share/fonts/truetype/dejavu/",
              "/system/fonts/", "C:/Windows/Fonts/", "/Library/Fonts/",
              "/System/Library/Fonts/Supplemental/"]
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
        im = ImageOps.fit(Image.open(C.logo_path).convert("RGBA"), (px, px), Image.LANCZOS)
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


@functools.lru_cache(maxsize=4)
def _bg_cached(Wd, Hd):
    h = Image.linear_gradient("L").transpose(Image.Transpose.ROTATE_90).resize((Wd, Hd))
    top = ImageOps.colorize(h, (13, 31, 31), (45, 26, 31))
    v = Image.linear_gradient("L").resize((Wd, Hd)).point(lambda p: min(255, int(p * 1.5)))
    base = Image.composite(Image.new("RGB", (Wd, Hd), (10, 10, 10)), top, v)
    d = ImageDraw.Draw(base, "RGBA")
    u = Wd / CW
    rng = random.Random(7)
    for _ in range(60):
        sx, sy = rng.randint(0, Wd), rng.randint(0, int(Hd * 0.3))
        r = u * rng.choice((0.6, 0.9, 1.2))
        d.ellipse([sx - r, sy - r, sx + r, sy + r], fill=(255, 255, 255, rng.randint(40, 110)))
    return base


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


def render_frames(cur, outcome, S, n):
    SS = 2 if n > 1 else 1
    u = S * SS
    k = lambda v: v * u
    F = lambda sz, b=True: _font(max(6, int(k(sz))), b)
    lw = lambda v: max(1, int(k(v)))
    Wd, Hd = int(CW * u), int(CH * u)
    outW, outH = int(CW * S), int(CH * S)
    tnow = time.time() + C.timer_lead

    pts = cur.get("pts") or [[0, 50]]
    eth = cur.get("eth") or []
    side = cur.get("side")
    waiting = side is None
    is_up = side == "UP"
    up_pct = min(max(pts[-1][1], 1), 99)
    dn_pct = 100 - up_pct
    conf = cur.get("conf") or max(up_pct, dn_pct)
    strike = cur.get("strike") or (eth[0][1] if eth else 0)
    now_px = eth[-1][1] if eth else (cur.get("price") or strike)
    close_ts = cur.get("close_ts") or (tnow + 900)
    accent = {"win": C_UP, "loss": C_DN, "settling": AMBER}.get(outcome)
    col = C_UP if is_up else C_DN

    base = _bg_cached(Wd, Hd).copy()
    d = ImageDraw.Draw(base, "RGBA")

    # header
    lg = int(k(60))
    base.paste(_logo(lg), (int(k(24)), int(k(24))), _logo(lg))
    d.ellipse([k(24), k(24), k(24) + lg, k(24) + lg], outline=(255, 255, 255, 80), width=lw(1.5))
    d.text((k(98), k(54)), "ETH 15 min", font=F(30), fill=WHITE, anchor="lm")

    ring = outcome in (None, "settling")
    if outcome == "win":
        btxt, bcol = "WIN", C_UP
    elif outcome == "loss":
        btxt, bcol = "LOSS", C_DN
    elif outcome == "settling":
        btxt, bcol = "NEXT " + _mmss(close_ts + 900 - tnow), AMBER
    else:
        btxt, bcol = _mmss(close_ts - tnow), (255, 255, 255)
    fb = F(22)
    bw = d.textlength(btxt, font=fb) / u + (68 if ring else 40)
    bx0 = 696 - bw
    d.rounded_rectangle([k(bx0), k(28), k(696), k(80)], radius=k(16), fill=(255, 255, 255, 26),
                        outline=(bcol + (190,)) if outcome else (255, 255, 255, 50), width=lw(2))
    if ring:
        cx, cy, r = k(bx0 + 28), k(54), k(12)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 255, 255, 40), width=lw(3))
        left = (close_ts - tnow) if outcome is None else (close_ts + 900 - tnow)
        frac = max(0.0, min(1.0, left / 900))
        if frac > 0.003:
            d.arc([cx - r, cy - r, cx + r, cy + r], -90, -90 + 360 * frac,
                  fill=bcol + (255,), width=lw(3))
        d.text((k(bx0 + 50), k(54)), btxt, font=fb, fill=bcol + (255,), anchor="lm")
    else:
        d.text((k(bx0 + bw / 2), k(54)), btxt, font=fb, fill=bcol + (255,), anchor="mm")

    # price row
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

    # chart panel
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
    for mm in (0, 5, 10, 15):
        x = PX0 + (PX1 - PX0) * mm / 15
        if 0 < mm < 15:
            _dots(d, x, PY0, x, IY1, (150, 165, 200, 28), lw(1), k(2), k(6))
        d.text((x, k(558)), f"{mm}m", font=F(10, False), fill=MUTED, anchor="mm")

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

    # status row
    d.rounded_rectangle([k(24), k(586), k(696), k(650)], radius=k(18), fill=(255, 255, 255, 14))
    base.paste(_logo(int(k(40))), (int(k(36)), int(k(598))), _logo(int(k(40))))
    tx = k(92)
    if waiting:
        l1, l2 = "Watching the market", "Waiting for a call"
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

    # vote badges
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

    # buttons
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

    # animated frames
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
    S = C.live_scale if animated else C.final_scale
    while True:
        frames = render_frames(cur, outcome, S, C.live_frames if animated else 1)
        buf = io.BytesIO()
        if not animated:
            frames[0].save(buf, format="PNG", optimize=True)
            res = (buf.getvalue(), "chart.png", "image/png")
        else:
            frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:],
                           duration=150, loop=0)
            res = (buf.getvalue(), "chart.gif", "image/gif")
        if len(res[0]) <= C.max_bytes or S <= 1:
            return res
        S *= 0.75


# ======================================================================
# DISCORD
# ======================================================================
def build_embed(c, state, yes, price, n_ex, secs_left, outcome=None, pm=None, stale=None):
    side = c["side"]
    s = stats(state["history"])
    fields = [{"name": "All-time", "value": s["all"], "inline": True},
              {"name": "Last 50", "value": s["last"], "inline": True},
              {"name": "ROI", "value": s["roi"], "inline": True}]
    if side is None:
        title, color = "⚪ KALSHI ETH 15m: NO CALL", GREY
        lines = ["**Call:** ⚪ NO CALL yet", "**Action:** WAIT, no position",
                 f"**Kalshi** YES {yes:.0f}% / NO {100 - yes:.0f}%"]
    else:
        arrow = "🆙" if side == "UP" else "⬇️"
        conf = c.get("conf") or 50
        tag, color = {"win": ("✅ WIN", GREEN), "loss": ("❌ LOSS", RED),
                      "settling": ("⏳ SETTLING", GREY)}.get(outcome, (None, GREEN if side == "UP" else RED))
        action = tag or ("HOLD" if c.get("buy_shown") else "BUY")
        lines = [f"**Call:** {arrow} {side} ({conf:.0f}% confidence at entry)"]
        if not outcome:
            lc = c.get("live_conf", conf)
            lines.append(f"**Updated confidence:** {lc:.0f}% ({lc - conf:+.0f} vs entry)")
        lines += [f"**Action:** {action}", f"**Kalshi** YES {yes:.0f}% / NO {100 - yes:.0f}%"]
        if c.get("entry_px") is not None:
            lines.append(f"**Entry price** {c['entry_px']:.0f}¢")
        if c.get("edge") is not None:
            lines.append(f"**Edge at entry** {c['edge']:+.1f}¢ after fee")
        title = f"{arrow} KALSHI ETH 15m: {side} | {action}"
    if pm is not None:
        lines.append(f"**Strike model** UP {pm:.0f}% / DOWN {100 - pm:.0f}%")
    if c.get("p_up") is not None and not outcome:
        lines.append(f"**Blended** UP {c['p_up'] * 100:.0f}% / DOWN {100 - c['p_up'] * 100:.0f}%")
    if c.get("avg_exp") is not None and (secs_left <= FINAL_SECONDS or outcome):
        lines.append("**Last minute** Kalshi settles on the 60s average, not the final tick")
        if c.get("avg60") is not None:
            lines.append(f"**60s average so far** ${c['avg60']:,.2f} | projected ${c['avg_exp']:,.2f} "
                         f"vs strike ${c['strike']:,.2f}")
        if c.get("avg_need") is not None and (c.get("avg_rem") or 0) >= 1:
            lines.append(f"**To finish UP** the last {c['avg_rem']:.0f}s must average ${c['avg_need']:,.2f}+")
    mk = c.get("mkt")
    if mk:
        arrows = " / ".join(f"{k} {'▲' if z >= 0 else '▼'}" for k, z in mk["z"].items())
        lines.append(f"**Basket** {arrows} (agreement {mk['breadth'] * 100:.0f}%)")
    if c.get("fib") or c.get("rsi") is not None:
        rs = f"{c['rsi']:.0f}" if c.get("rsi") is not None else "warming up"
        lines.append(f"**Signals** Fibonacci {c.get('fib') or 'forming'}% | RSI {rs}")
    done = outcome in ("win", "loss")
    vt = (c.get("votes_call") if done else None) or c.get("votes")
    tally = (c.get("tally_call") if done else None) or c.get("tally")
    if vt and tally:
        names = {"mom": "Momentum", "fib": "Fibonacci", "ema": "EMA 9/21",
                 "boll": "Bollinger", "atr": "ATR", "macd": "MACD"}
        lab = lambda v: "⚪ WARMING" if v is None else "🟢 UP" if v > 0 else "🔴 DOWN" if v < 0 else "⚪ NEUTRAL"
        lines.append("**Indicators at entry (display only)**" if done and c.get("votes_call")
                     else "**Indicators (display only)**")
        for k, nm in names.items():
            if k in vt:
                lines.append(f"{lab(vt[k][0])} · **{nm}** {vt[k][1]}")
        lean = "UP" if tally[0] > tally[1] else "DOWN" if tally[1] > tally[0] else "split"
        lines.append(f"**Confluence** UP {tally[0]} / DOWN {tally[1]} / NEUTRAL {tally[2]}"
                     f" → indicators lean {lean}"
                     + (f" | **locked call {side}**" if side else " | no call yet"))
    if stale:
        lines.append(f"**Warning** data is {stale:.0f}s old, new calls paused")
    if price:
        lines.append(f"**Price** ${price:,.2f} (median of {n_ex} exch)"
                     + (" ⚠ too few feeds, new calls paused" if n_ex < C.min_exch else ""))
    lines.append(f"**Strike** {c['strike']:,.2f} | {max(int(secs_left), 0)}s left")
    return {"title": title, "description": "\n".join(lines), "color": color, "fields": fields,
            "footer": {"text": "Signals only, not financial advice"},
            "timestamp": datetime.now(timezone.utc).isoformat()}


HTTP = requests.Session()
IO = ThreadPoolExecutor(4)
RENDER_POOL = None


def render_chart(snap, outcome):
    if RENDER_POOL is not None:
        try:
            return RENDER_POOL.submit(chart_file, snap, outcome).result(timeout=30)
        except Exception:
            log.exception("render pool failed, rendering inline")
    return chart_file(snap, outcome)


def send(embed, message_id=None, snap=None, outcome=None, text_only=False):
    payload, f = {"embeds": [embed]}, None
    if text_only and message_id:
        embed["image"] = {"url": "attachment://chart.gif"}
    elif snap is not None:
        f = render_chart(snap, outcome)
        embed["image"] = {"url": f"attachment://{f[1]}"}
        if message_id:
            payload["attachments"] = []
    for attempt in range(4):
        kw = {"timeout": 60}
        if f:
            kw["files"] = {"files[0]": (f[1], f[0], f[2])}
            kw["data"] = {"payload_json": json.dumps(payload)}
        else:
            kw["json"] = payload
        if message_id:
            r = HTTP.patch(f"{C.webhook}/messages/{message_id}", **kw)
        else:
            r = HTTP.post(C.webhook, params={"wait": "true"}, **kw)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 1)) + 0.1)
            continue
        if r.status_code == 404 and message_id:     # message deleted: post a new one
            message_id = None
            continue
        if r.status_code >= 500:
            time.sleep(1 + attempt)
            continue
        r.raise_for_status()
        return r.json()["id"]
    raise RuntimeError("discord send failed")


class LiveSink:
    def live(self, embed, cur, new_due, text_only):
        if not SEND_LOCK.acquire(blocking=False):
            return False
        snap = None if text_only else snapshot(cur)
        mid = None if new_due else cur["message_id"]

        def run():
            try:
                new_id = send(embed, mid, snap, None, text_only)
                if new_due:
                    with LOCK:
                        cur["message_id"] = new_id
            except Exception:
                log.exception("send error")
            finally:
                SEND_LOCK.release()
        IO.submit(run)
        return True

    def final(self, embed, ref, outcome):
        snap = snapshot(ref)

        def run():
            try:
                with SEND_LOCK:
                    with LOCK:
                        mid = ref.get("message_id")
                    send(embed, mid, snap, outcome)
            except Exception:
                log.exception("final send error")
        IO.submit(run)


class NullSink:
    def live(self, embed, cur, new_due, text_only):
        if new_due:
            cur["message_id"] = "replay"
        return True

    def final(self, embed, ref, outcome):
        pass


# ======================================================================
# WINDOW LIFECYCLE
# ======================================================================
def new_window(state, m):
    strike = float(m.get("floor_strike") or m.get("cap_strike") or 0)
    cur = {"ticker": m["ticker"], "side": None, "strike": strike, "close_ts": ts(m["close_time"]),
           "message_id": None, "pts": [], "eth": [], "locked": False, "conf": None,
           "open": {}, "open_t": now()}
    state["current"] = cur
    return cur


def roll_window(state, m, sink):
    cur = state["current"]
    if not cur or (m and m["ticker"] == cur["ticker"]):
        return
    state["current"] = None
    state["pending"].append(cur)
    if cur["side"] is None:
        last = cur["pts"][-1][1] if cur["pts"] else 50
        sink.final(build_embed(cur, state, last, None, 0, 0), cur, None)
    else:
        sink.final(build_embed(cur, state, 50, None, 0, 0, "settling"), cur, "settling")
    save_state(state, force=True)


def make_call(cur, side, conf, src, elapsed, q):
    cur.update(side=side, conf=conf, locked=True, called_at=round(elapsed), src=src,
               votes_call=dict(cur["votes"]), tally_call=list(cur["tally"]),
               comb_call=cur["comb"], rsi_call=cur["rsi"],
               entry_px=round(entry_price(q, side), 1), yes_entry=round(q["yes"], 1),
               feat_call=dict(cur["feat"]))


def update_signals(cur, px, strike, yes, close_ts, t):
    secs_left = close_ts - t
    price = px["ETH"][0]
    for s, (p, _) in px.items():
        if p:
            cur["open"].setdefault(s, p)
    tilt, breadth, zs = market_signal({s: p for s, (p, _) in px.items()}, cur["open"], t - cur["open_t"])
    ss = settle_stats(price, strike, close_ts, t, tilt)
    blend, pm = predict(yes, ss, secs_left)
    if ss:
        cur.update(avg60=round(ss["obs_avg"], 2) if ss["obs_avg"] else None,
                   avg_exp=round(ss["exp_avg"], 2), avg_rem=round(ss["rem"], 1),
                   avg_need=round(ss["need"], 2) if ss["need"] is not None else None)
    cur["mkt"] = {"tilt": round(tilt, 2), "breadth": round(breadth, 2),
                  "z": {s: round(z, 2) for s, z in zs.items()}}
    series = series_10s("ETH")
    fib, fib_lvl = fib_signal(series)
    trend = trend_signal(series, tilt)
    r = rsi(series)
    rsi_s = clamp((r - 50) / 50, -1.0, 1.0) if r is not None else 0.0
    mom = 0.7 * momentum_fast("ETH") + 0.3 * math.tanh(tilt / 1.5)
    score, prob = composite(mom, fib, trend, blend, rsi_s)
    if secs_left <= FINAL_SECONDS and ss:
        lock = clamp((FINAL_SECONDS - secs_left) / FINAL_SECONDS, 0.0, 1.0)
        score = (1 - lock) * score + lock * clamp((ss["pm"] - 50) / 50, -1.0, 1.0)
    votes = {"ema": vote_ema(series), "boll": vote_boll(series),
             "atr": vote_atr(series, price, strike, secs_left, ss), "macd": vote_macd(series)}
    ready = [v for v, _ in votes.values() if v is not None]
    vscore = sum(ready) / len(ready) if ready else 0.0
    comb = C.w_core * score + C.w_vote * vscore
    cur["votes"] = {k: [v, tx] for k, (v, tx) in votes.items()}
    cur["votes"]["mom"] = [1 if mom >= C.mom_min else -1 if mom <= -C.mom_min else 0, f"{mom:+.2f}"]
    cur["votes"]["fib"] = [None if fib_lvl is None else 1 if fib >= C.fib_agree
                           else -1 if fib <= -C.fib_agree else 0,
                           f"retrace {fib_lvl}% ({fib:+.2f})" if fib_lvl else "forming"]
    cur["tally"] = [ready.count(1), ready.count(-1), len(ready) - ready.count(1) - ready.count(-1)]
    cur.update(comb=round(comb, 3), score=round(score, 3), fib=fib_lvl,
               rsi=round(r, 1) if r is not None else None,
               sig={"mom": round(mom, 2), "fib": round(fib, 2), "trend": round(trend, 2), "prob": round(prob, 2)})
    cur["feat"] = {"mom": round(mom, 3), "fib": round(fib, 3), "trend": round(trend, 3),
                   "pm": round(pm, 1) if pm is not None else None, "yes": round(yes, 1),
                   "comb": round(comb, 3), "rsi": cur["rsi"], "tilt": round(tilt, 2),
                   "votes": {k: v[0] for k, v in cur["votes"].items()}}
    return {"ss": ss, "blend": blend, "pm": pm, "mom": mom, "fib": fib, "trend": trend,
            "r": r, "comb": comb, "ready": ready, "all_ready": len(ready) == 4}


def run_window(state, cur, m, px, age, t, sink):
    cur.setdefault("open", {})
    cur.setdefault("open_t", t)
    close_ts = cur["close_ts"]
    secs_left, elapsed = close_ts - t, 900 - (close_ts - t)
    q = quote(m)
    yes = q["yes"]
    price, n_ex = px["ETH"]
    strike = float(m.get("floor_strike") or m.get("cap_strike") or 0) or cur["strike"]
    cur["strike"] = strike
    stale = age > C.stale_s or not price or n_ex < C.min_exch
    prev_side = cur["side"]
    ctx = update_signals(cur, px, strike, yes, close_ts, t)
    can_call = not stale and bool(strike)
    if (cur["side"] is None and can_call and ctx["ss"]
            and C.final_cutoff < secs_left <= C.call_max):
        maybe_call(cur, ctx, q, secs_left, elapsed, t)
    pu = p_up(yes, ctx["ss"], secs_left)
    if cur["side"] and pu is not None:
        cur["live_conf"] = round(clamp((pu if cur["side"] == "UP" else 1 - pu) * 100, 1, 99), 1)
    if can_call and ctx["ss"] and t - cur.get("last_sample", 0) >= C.sample_s:
        cur["last_sample"] = t   # logged for EVERY window, called or not, so fitting has no selection bias
        cur.setdefault("samples", []).append([round(secs_left), round(ctx["ss"]["pm"], 2), round(yes, 1)])
    cur["price"] = price
    if price:
        cur["eth"].append([round(elapsed), round(price, 2)])
    cur["pts"].append([round(elapsed), round(yes, 1)])

    since_post = t - cur.get("last_post", 0)
    ev = f"New call: {cur['side']}" if cur["side"] and not prev_side else cur.get("pending_ev")
    new_due = cur["message_id"] is None or ev is not None or since_post >= C.new_msg_s
    if new_due or t - cur.get("last_edit", 0) >= C.edit_s:
        embed = build_embed(cur, state, yes, price, n_ex, secs_left, pm=ctx["pm"],
                            stale=age if age > C.stale_s else None)
        if ev:
            embed["description"] = f"**Update:** {ev}\n" + embed["description"]
        text_only = (not new_due) and (t - cur.get("last_img", 0) < C.img_s)
        if sink.live(embed, cur, new_due, text_only):
            cur.pop("pending_ev", None)
            if new_due:
                cur["last_post"] = t
            if not text_only:
                cur["last_img"] = t
            cur["last_edit"] = t
            if cur["side"]:
                cur["buy_shown"] = True
        elif ev:
            cur["pending_ev"] = ev


def tick(state, feed, sink):
    t = now()
    m, age = feed.market()
    px = sanitize(feed.prices())
    if REC and m:
        REC.write(m, px)
    with LOCK:
        roll_window(state, m, sink)
        if m is None:
            return
        for s, (p, _) in px.items():
            record_price(s, p)
        cur = state["current"] or new_window(state, m)
        run_window(state, cur, m, px, age, t, sink)


# ======================================================================
# SETTLEMENT
# ======================================================================
def settle_one(state, p, feed, sink):
    t = now()
    if t < p["close_ts"] + 1:
        return
    res = feed.result(p["ticker"])
    if res is None:
        if t < p["close_ts"] + 300:
            return
        if t > p["close_ts"] + 3600:
            with LOCK:
                if p in state["pending"]:
                    state["pending"].remove(p)
            return
        if p.get("avg_exp") is not None and (p.get("avg_rem") or 99) <= 5:
            res = "yes" if p["avg_exp"] >= p["strike"] else "no"
        else:
            price = feed.prices()["ETH"][0]
            if price is None:
                return
            res = "yes" if price >= p["strike"] else "no"
    up_won = res == "yes"
    win = None if p["side"] is None else (up_won == (p["side"] == "UP"))
    log_window({"ticker": p["ticker"], "close_ts": p["close_ts"], "strike": p["strike"], "res": res,
                "side": p["side"], "win": win, "conf": p.get("conf"), "src": p.get("src"),
                "entry_px": p.get("entry_px"), "yes_entry": p.get("yes_entry"),
                "feat": p.get("feat_call") or p.get("feat")})
    with LOCK:
        if p not in state["pending"]:
            return
        state["pending"].remove(p)
        try:
            with open(C.samples_file, "a") as sf:
                for left, pm_, y_ in p.get("samples", []):
                    sf.write(json.dumps({"tk": p["ticker"], "left": left, "pm": pm_, "yes": y_,
                                         "y": 1 if up_won else 0}, separators=(",", ":")) + "\n")
        except Exception:
            log.exception("samples log")
        if p["side"] is None:
            return
        state["history"].append({"ticker": p["ticker"], "side": p["side"], "win": win,
                                 "conf": p.get("conf"), "entry_px": p.get("entry_px"),
                                 "yes_entry": p.get("yes_entry"), "src": p.get("src"),
                                 "called_at": p.get("called_at"), "comb": p.get("comb_call")})
        p["pts"].append([900, 100 if up_won else 0])
        outcome = "win" if win else "loss"
        sink.final(build_embed(p, state, 100.0 if up_won else 0.0, None, 0, 0, outcome), p, outcome)
    save_state(state, force=True)


def settle_pending(state, feed, sink):
    with LOCK:
        todo = list(state["pending"])
    for p in todo:
        try:
            settle_one(state, p, feed, sink)
        except Exception:
            log.exception("settle error")


def settler_loop(state, feed, sink):
    last_health = 0.0
    while True:
        settle_pending(state, feed, sink)
        if time.time() - last_health > 60:
            last_health = time.time()
            ages = {f"{s}/{x}": round(time.time() - t_) for (s, x), (_, t_) in list(feed.latest.items())}
            log.info("feed ages in seconds (6 expected: ETH+BTC x cb/kr/bs): %s", ages)
        time.sleep(3)


# ======================================================================
# DASHBOARD
# ======================================================================
PAGE = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>ETH 15m live</title>
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

STATE = {"s": None, "feed": None}


def dash_payload(m):
    cur = (STATE["s"] or {}).get("current") or {}
    q = quote(m)
    return json.dumps({"now": time.time(), "price": STATE["feed"].prices()["ETH"][0],
                       "strike": float(m.get("floor_strike") or m.get("cap_strike") or 0),
                       "close_ts": ts(m["close_time"]), "yes": q["yes"], "side": cur.get("side"),
                       "conf": cur.get("live_conf") or cur.get("conf"), "votes": cur.get("votes"),
                       "tally": cur.get("tally"), "comb": cur.get("comb"), "rsi": cur.get("rsi"),
                       "fib": cur.get("fib"), "avg60": cur.get("avg60")}, default=str)


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
                m, _ = STATE["feed"].market()
                if m:
                    try:
                        data = dash_payload(m)
                    except Exception:
                        data = None
                    if data:
                        self.wfile.write(f"data: {data}\n\n".encode())
                        self.wfile.flush()
                time.sleep(1 / C.dash_hz)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


# ======================================================================
# ENTRY POINTS
# ======================================================================
def main():
    global RENDER_POOL, REC
    load_calib()
    if not C.webhook:
        sys.exit("Set DISCORD_WEBHOOK_URL")
    if websocket is None:
        sys.exit("pip install websocket-client requests pillow")
    RENDER_POOL = ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn"))
    feed, sink = LiveFeed(), LiveSink()
    STATE["feed"] = feed
    threading.Thread(target=feed.rest_loop, daemon=True).start()
    feed.start_price_feeds()
    if C.key_id and C.key_path:
        threading.Thread(target=feed.start_kalshi_ws, daemon=True).start()
    else:
        log.info("No Kalshi key set: polling every %.2fs", C.kalshi_poll)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", C.dash_port), Dash)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log.info("Dashboard: http://localhost:%d", C.dash_port)
    except OSError as e:
        log.warning("dashboard not started: %s", e)
    if C.record:
        REC = Recorder(C.tick_file)
    while feed.market()[0] is None or not feed.latest:
        time.sleep(0.05)
    state = load_state()
    if C.fresh_stats and state["history"]:
        state.setdefault("history_old", []).extend(state["history"])
        state["history"] = []
        log.info("archived old history, stats start fresh")
    STATE["s"] = state
    threading.Thread(target=settler_loop, args=(state, feed, sink), daemon=True).start()
    end, nxt, errs = time.time() + C.run_s, time.time(), 0
    try:
        while time.time() < end:
            try:
                tick(state, feed, sink)
                save_state(state)
                errs = 0
            except Exception:
                errs += 1
                log.exception("tick error")
                time.sleep(min(5.0, 0.2 * errs))
            nxt += C.poll
            d = nxt - time.time()
            if d > 0:
                time.sleep(d)
            else:
                nxt = time.time()
    except KeyboardInterrupt:
        pass
    finally:
        save_state(state, force=True)
        RENDER_POOL.shutdown(wait=False, cancel_futures=True)


def replay(path):
    C.state_file, C.windows_file = "replay_state.json", "replay_windows.jsonl"
    C.samples_file = "replay_samples.jsonl"
    for f in (C.state_file, C.windows_file, C.samples_file):
        if os.path.exists(f):
            os.remove(f)
    load_calib()
    feed, sink = ReplayFeed(path), NullSink()
    state = {"history": [], "history_old": [], "current": None, "pending": []}
    for row in feed.rows:
        feed.step(row)
        tick(state, feed, sink)
        settle_pending(state, feed, sink)
    save_state(state, force=True)
    report(state["history"])


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) > 2 and sys.argv[1] == "replay":
        replay(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] == "audit":
        audit()
    elif len(sys.argv) > 1 and sys.argv[1] == "fit":
        fit()
    elif len(sys.argv) > 1 and sys.argv[1] == "report":
        with open(sys.argv[2] if len(sys.argv) > 2 else C.state_file) as fh:
            report(json.load(fh)["history"])
    else:
        main()
