import functools
import io
import json
import math
import os
import random
import statistics
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

try:
    from zoneinfo import ZoneInfo
    CHART_TZ = ZoneInfo(os.getenv("CHART_TZ", "America/New_York"))
except Exception:
    CHART_TZ = timezone.utc

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
SERIES = os.getenv("KALSHI_SERIES", "KXETH15M")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
POLL = float(os.getenv("POLL_SECONDS", "10"))
RUN_SECONDS = int(os.getenv("RUN_SECONDS", str(345 * 60)))
STATE_FILE = os.getenv("STATE_FILE", "state.json")
GREEN, RED, GREY = 0x00FF00, 0xFF3B30, 0x808080

W_MARKET = float(os.getenv("W_MARKET", "0.5"))
MIN_CONF = float(os.getenv("MIN_CONF", "51"))
FLIP_CONF = float(os.getenv("FLIP_CONF", "25"))
CALL_CUTOFF = int(os.getenv("CALL_CUTOFF", "60"))
TILT_W = float(os.getenv("TILT_W", "0.25"))   # max pull of the basket, in sigmas (x3 clip)

LOGO_PATH = os.getenv("LOGO_PATH", "eth_logo.png")
LIVE_SCALE = float(os.getenv("LIVE_SCALE", "1.5"))     # 1.5 = 1080x1200 GIF
FINAL_SCALE = float(os.getenv("FINAL_SCALE", "3"))     # 3 = 2160x2400 PNG
MAX_BYTES = int(float(os.getenv("MAX_MB", "7.5")) * 1024 * 1024)


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
    r = requests.get(f"{KALSHI}/markets", params={"series_ticker": SERIES, "status": "open", "limit": 20}, timeout=10)
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
PRICES = {s: deque(maxlen=120) for s in WEIGHTS}


def _cb(pair):
    return float(requests.get(f"https://api.coinbase.com/v2/prices/{pair}/spot", timeout=5).json()["data"]["amount"])


def _kr(pair):
    r = requests.get("https://api.kraken.com/0/public/Ticker", params={"pair": pair}, timeout=5).json()["result"]
    return float(next(iter(r.values()))["c"][0])


def _bs(pair):
    return float(requests.get(f"https://www.bitstamp.net/api/v2/ticker/{pair}/", timeout=5).json()["last"])


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


def model_prob(price, strike, secs_left, tilt=0.0):
    if not price or not strike:
        return None
    tau = max(secs_left, 1)
    sig = vol("ETH")
    fade = min(1.0, math.sqrt(tau / 300))
    z = math.log(price / strike) / (sig * math.sqrt(tau)) + TILT_W * max(-3.0, min(3.0, tilt)) * fade
    return max(1.0, min(99.0, norm_cdf(z) * 100))


def predict(yes_mkt, price, strike, secs_left, tilt=0.0):
    pm = model_prob(price, strike, secs_left, tilt)
    if pm is None:
        return yes_mkt, None
    return W_MARKET * yes_mkt + (1 - W_MARKET) * pm, pm


def pick_call(readings):
    """readings: {"ETH": up_pct, "BTC": up_pct}. Strongest reading wins, must be >= MIN_CONF."""
    best = None
    for sym, p in readings.items():
        if p is None:
            continue
        side = "UP" if p >= 50 else "DOWN"
        conf = p if side == "UP" else 100 - p
        if best is None or conf > best[1]:
            best = (side, conf, sym)
    return best if best and best[1] >= MIN_CONF else None


# ---------- stats ----------
def stats(history):
    n = len(history)
    w = sum(1 for h in history if h["win"])
    return {"all": f"{w}W-{n - w}L ({w / n * 100:.0f}%)" if n else "n/a"}


# ---------- call labels ----------
def momentum_level(c):
    mk = c.get("mkt")
    if not mk:
        return "LOW"
    m = abs(mk["tilt"])
    if m >= 1.2:
        return "HIGH"
    if m >= 0.5:
        return "MODERATE"
    return "LOW"


def flip_potential(c):
    rd = c.get("read") or {}
    live = rd.get(c.get("src", "ETH"))
    if live is None:
        return None
    return 100 - live if c["side"] == "UP" else live


def fib_level(c):
    eth = c.get("eth") or []
    if len(eth) < 5:
        return None
    prices = [p for _, p in eth]
    hi, lo, now = max(prices), min(prices), prices[-1]
    if hi <= lo:
        return None
    levels = {r: hi - (hi - lo) * r for r in (0.236, 0.382, 0.5, 0.618, 0.786)}
    r = min(levels, key=lambda x: abs(levels[x] - now))
    return f"{r * 100:.1f}".rstrip("0").rstrip(".")


# ---------- chart ----------
CW, CH = 720, 800
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
_FONTS_R = ["Inter-Regular.ttf", "Roboto-Regular.ttf", "LiberationSans-Regular.ttf", "NotoSans-Regular.ttf",
            "arial.ttf", "Arial.ttf", "DejaVuSans.ttf"]


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
    now = time.time()

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
            d.arc([cx - r, cy - r, cx + r, cy + r], -90, -90 + 360 * frac, fill=bcol + (255,), width=lw(3))
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
    hi = max([c[2] for c in cs] + ([strike] if strike else []))
    lo = min([c[3] for c in cs] + ([strike] if strike else []))
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

    # high / low markers
    hc, lc_ = max(cs, key=lambda c: c[2]), min(cs, key=lambda c: c[3])
    d.text((cx_(hc[0]), max(PY0 + k(18), Y(hc[2]) - k(9))), f"H {hc[2]:,.1f}", font=F(10), fill=C_UP + (230,), anchor="mm")
    d.text((cx_(lc_[0]), min(PY1 - k(6), Y(lc_[3]) + k(9))), f"L {lc_[3]:,.1f}", font=F(10), fill=C_DN + (230,), anchor="mm")

    # delta labels (left of plot)
    fmt = ".0f" if (hi - lo) >= 20 else ".1f"
    for fr in (0.2, 0.5, 0.8):
        v = lo + (hi - lo) * fr
        dd = v - (strike or v)
        c = C_UP if dd >= 0 else C_DN
        d.text((k(24), Y(v)), f"{'+' if dd >= 0 else '-'} ${abs(dd):{fmt}}", font=F(13), fill=c + (255,), anchor="lm")

    # entry marker
    ca = cur.get("called_at")
    if ca is not None and not waiting:
        ex = PX0 + (PX1 - PX0) * min(ca, 900) / 900
        _dots(d, ex, PY0, ex, PY1, col + (150,), lw(1), k(3), k(4))
        _arrow(d, ex, PY0 + k(14), is_up, k(7), col + (255,))
        d.text((ex + k(10), PY0 + k(14)), "ENTRY", font=F(10), fill=col + (230,), anchor="lm")

    # volume strip
    acts = [(c[2] - c[3]) + 1e-9 for c in cs]
    mx = max(acts)
    for c, a in zip(cs, acts):
        cc = C_UP if c[4] >= c[1] else C_DN
        x = cx_(c[0])
        d.rectangle([x - bwid, VY1 - a / mx * (VY1 - VY0), x + bwid, VY1], fill=cc + (120,))

    # indicator strip
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

    tail = eth[-150:]
    if len(tail) >= 2:
        tt = [t for t, _ in tail]
        pp = [p for _, p in tail]
        a8, a3 = _ema(pp, 8), _ema(pp, 3)
        line(list(zip(tt, [p - (strike or pp[0]) for p in pp])), BLUE)
        line(list(zip(tt, [p - e for p, e in zip(pp, a8)])), C_DN)
        line(list(zip(tt, [x - y for x, y in zip(a3, a8)])), CYAN)
    line([(t, v) for t, v in pts[-150:]], ORG)

    # live tag + last candle position
    lc = cs[-1]
    lcx, ly = cx_(lc[0]), Y(lc[4])
    tcol = C_UP if lc[4] >= lc[1] else C_DN
    d.rectangle([PX1 + k(2), ly - k(9), k(706), ly + k(9)], fill=tcol + (255,))
    d.text((PX1 + k(6), ly), f"{lc[4]:,.1f}", font=F(11), fill=(8, 10, 20, 255), anchor="lm")

    # result tag (compact, top right of chart)
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
        l1 = "Scanning, no call yet"
        l2 = f"Best side {max(up_pct, dn_pct):.0f}%, needs above {MIN_CONF:.0f}%"
    else:
        l1 = f"{side} call · {conf:.0f}% confidence"
        l2 = f"Entry at {ca // 60}:{ca % 60:02d} into the window" if ca is not None else "Entry locked"
        _arrow(d, k(104), k(611), is_up, k(10), col + (255,))
        tx = k(122)
    d.text((tx, k(610)), l1, font=F(17), fill=WHITE, anchor="lm")
    d.text((k(92), k(634)), l2, font=F(13, False), fill=MUTED, anchor="lm")

    # ---- buttons ----
    by0, by1 = k(668), k(772)
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
        d.text(((x0 + x1) / 2 + k(14), by0 + k(38)), f"{name} · {pct:.0f}%", font=F(24), fill=(255, 255, 255, ta), anchor="mm")
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
        frames = render_frames(cur, outcome, S, 10 if animated else 1)
        buf = io.BytesIO()
        if not animated:
            frames[0].save(buf, format="PNG", optimize=True)
            res = (buf.getvalue(), "chart.png", "image/png")
        else:
            frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=90, loop=0)
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
        rd = c.get("read") or {"ETH": p_up}
        best = max(max(v, 100 - v) for v in rd.values())
        lines = [
            f"**Call:** ⚪ NO CALL (best side {best:.0f}%, needs above {MIN_CONF:.0f}%)",
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
        elif c.get("flipped"):
            action = "RESULT FLIPPED, CASHOUT"
        else:
            action = "HOLD (if holding " + ("YES" if side == "UP" else "NO") + ")"
        fp = flip_potential(c)
        fp_txt = f" | flip potential {fp:.0f}%" if fp is not None and not outcome else ""
        fl = fib_level(c)
        fib_txt = f" | based on recent Fibonacci sequence, price near {fl}% level" if fl and not outcome else ""
        lines = [
            f"**Call:** {arrow} {side} ({conf:.0f}% confidence at entry, 🔒 locked){fib_txt} | momentum is {momentum_level(c)}{fp_txt}",
            f"**Action:** {action}",
            f"**Kalshi** YES {yes:.0f}% / NO {100 - yes:.0f}%",
        ]
        title = f"{arrow} KALSHI ETH 15m: {side} | {tag}"
    if pm is not None:
        lines.append(f"**Strike model** UP {pm:.0f}% / DOWN {100 - pm:.0f}%")
    mk = c.get("mkt")
    if mk:
        arrows = " / ".join(f"{sym} {'▲' if z >= 0 else '▼'}" for sym, z in mk["z"].items())
        lines.append(f"**Basket** {arrows} (agreement {mk['breadth'] * 100:.0f}%)")
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


def send(embed, message_id=None, cur=None, outcome=None):
    payload = {"embeds": [embed]}
    f_data = None
    if cur is not None:
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
            r = requests.patch(f"{WEBHOOK}/messages/{message_id}", **kw)
        else:
            r = requests.post(WEBHOOK, params={"wait": "true"}, **kw)
        if r.status_code == 429:
            time.sleep(r.json().get("retry_after", 1) + 0.1)
            continue
        r.raise_for_status()
        return r.json()["id"]
    raise RuntimeError("discord rate limited")


# ---------- core ----------
def settle_pending(state):
    for p in list(state["pending"]):
        try:
            m = get_market(p["ticker"])
            res = m.get("result")
            if res not in ("yes", "no"):
                if time.time() < p["close_ts"] + 300:
                    continue
                price, _ = eth_price()
                if price is None:
                    continue
                res = "yes" if price >= p["strike"] else "no"
            win = (res == "yes") == (p["side"] == "UP")
            state["history"].append({"ticker": p["ticker"], "side": p["side"], "win": win,
                                     "tilt": (p.get("mkt") or {}).get("tilt")})
            state["pending"].remove(p)
            yes_final = 100.0 if res == "yes" else 0.0
            p.setdefault("pts", []).append([900, 100 if res == "yes" else 0])
            send(build_embed(p, state, yes_final, None, 0, 0, "win" if win else "loss"),
                 p["message_id"], cur=p, outcome="win" if win else "loss")
            save_state(state)
        except Exception as e:
            print("settle error", e)


def tick(state):
    settle_pending(state)
    m = open_market()
    cur = state["current"]
    now = time.time()

    if cur and (m is None or m["ticker"] != cur["ticker"]):
        state["current"] = None
        if cur["side"] is None:
            last_yes = cur["pts"][-1][1] if cur["pts"] else 50
            send(build_embed(cur, state, last_yes, None, 0, 0), cur["message_id"], cur=cur)
        else:
            state["pending"].append(cur)
            send(build_embed(cur, state, 50, None, 0, 0, "settling"), cur["message_id"], cur=cur, outcome="settling")
        cur = None
        save_state(state)
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
    blend, pm = predict(yes, price, strike, secs_left, tilt)
    cur["mkt"] = {"tilt": round(tilt, 2), "breadth": round(breadth, 2),
                  "z": {s: round(z, 2) for s, z in zs.items()}}

    blend_btc, _ = predict(yes, price, strike, secs_left, zs.get("BTC", 0.0))
    readings = {"ETH": blend, "BTC": blend_btc}
    cur["read"] = readings

    if cur["side"] is None and secs_left > CALL_CUTOFF:
        pick = pick_call(readings)
        if pick:
            side, conf, src = pick
            cur.update(side=side, conf=conf, locked=True, called_at=round(elapsed), src=src)

    if cur["side"] and not cur.get("flipped"):
        live = readings.get(cur.get("src", "ETH"))
        if live is not None and (live if cur["side"] == "UP" else 100 - live) < FLIP_CONF:
            cur["flipped"] = True

    cur["price"] = price
    if price:
        cur.setdefault("eth", []).append([round(elapsed), round(price, 2)])
    cur["pts"].append([round(elapsed), round(yes, 1)])
    new_id = send(build_embed(cur, state, yes, price, n_ex, secs_left, blend=blend, pm=pm),
                  cur["message_id"], cur=cur)
    if cur["message_id"] is None:
        cur["message_id"] = new_id
    save_state(state)


def main():
    state = load_state()
    end = time.time() + RUN_SECONDS
    while time.time() < end:
        try:
            tick(state)
        except Exception as e:
            print("tick error", e)
        save_state(state)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
