import functools
import io
import json
import math
import os
import statistics
import time
from collections import deque
from datetime import datetime, timezone

import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
SERIES = os.getenv("KALSHI_SERIES", "KXETH15M")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
POLL = float(os.getenv("POLL_SECONDS", "10"))
RUN_SECONDS = int(os.getenv("RUN_SECONDS", str(345 * 60)))
STATE_FILE = os.getenv("STATE_FILE", "state.json")
GREEN, RED, GREY = 0x00FF00, 0xFF3B30, 0x808080

W_MARKET = float(os.getenv("W_MARKET", "0.5"))        # weight on Kalshi price vs. strike model
MOM_W = float(os.getenv("MOM_W", "0.3"))              # momentum strength
MIN_CONF = float(os.getenv("MIN_CONF", "60"))         # must be strictly above this to make a call
CALL_CUTOFF = int(os.getenv("CALL_CUTOFF", "60"))     # no new calls with fewer secs than this left
DEF_SIGMA = 1.1e-4                                    # ~60% annual vol, per sqrt(second)
PRICES = deque(maxlen=120)                            # (timestamp, price)


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


# ---------- data ----------
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


def eth_price():
    px = []
    for fn in (
        lambda: float(requests.get("https://api.coinbase.com/v2/prices/ETH-USD/spot", timeout=5).json()["data"]["amount"]),
        lambda: float(requests.get("https://api.kraken.com/0/public/Ticker?pair=ETHUSD", timeout=5).json()["result"]["XETHZUSD"]["c"][0]),
        lambda: float(requests.get("https://www.bitstamp.net/api/v2/ticker/ethusd/", timeout=5).json()["last"]),
    ):
        try:
            px.append(fn())
        except Exception:
            pass
    return (statistics.median(px), len(px)) if px else (None, 0)


# ---------- prediction ----------
def record_price(price):
    if price:
        PRICES.append((time.time(), price))


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def vol_per_sqrt_sec():
    pts = list(PRICES)
    rets = []
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        dt = t1 - t0
        if dt > 0.5:
            rets.append(math.log(p1 / p0) / math.sqrt(dt))
    if len(rets) < 8:
        return DEF_SIGMA
    s = math.sqrt(sum(r * r for r in rets) / len(rets))
    return max(DEF_SIGMA * 0.4, min(s, DEF_SIGMA * 4))


def momentum():
    pts = list(PRICES)
    if len(pts) < 2:
        return 0.0
    t_now, p_now = pts[-1]
    old = [x for x in pts if t_now - x[0] >= 60]
    if not old:
        return 0.0
    t0, p0 = old[-1]
    return math.log(p_now / p0) / (t_now - t0)    # log-return per second


def model_prob(price, strike, secs_left):
    """P(ETH finishes >= strike), in percent, or None if inputs are missing."""
    if not price or not strike:
        return None
    tau = max(secs_left, 1)
    sig = vol_per_sqrt_sec()
    drift = max(-sig, min(sig, momentum())) * MOM_W
    z = (math.log(price / strike) + drift * tau) / (sig * math.sqrt(tau))
    return max(1.0, min(99.0, norm_cdf(z) * 100))


def predict(yes_mkt, price, strike, secs_left):
    pm = model_prob(price, strike, secs_left)
    if pm is None:
        return yes_mkt, None
    return W_MARKET * yes_mkt + (1 - W_MARKET) * pm, pm


# ---------- stats ----------
def stats(history):
    def rec(rows):
        w = sum(1 for h in rows if h["win"])
        n = len(rows)
        return f"{w}W-{n - w}L ({w / n * 100:.0f}%)" if n else "n/a"

    last = history[-20:]
    streak = 0
    for h in reversed(history):
        if streak == 0 or h["win"] == (streak > 0):
            streak += 1 if h["win"] else -1
        else:
            break
    streak_txt = f"{streak}W" if streak > 0 else f"{-streak}L" if streak < 0 else "n/a"
    lw = sum(1 for h in last if h["win"])
    return {
        "all": rec(history),
        "last20": f"{lw}/{len(last)} ({lw / len(last) * 100:.0f}%)" if last else "n/a",
        "streak": streak_txt,
        "up": rec([h for h in history if h["side"] == "UP"]),
        "down": rec([h for h in history if h["side"] == "DOWN"]),
    }


# ---------- chart ----------
LIVE_SCALE = float(os.getenv("LIVE_SCALE", "1.5"))    # 1.5 = 1920x1080 GIF
FINAL_SCALE = float(os.getenv("FINAL_SCALE", "3"))    # 3 = 3840x2160 still PNG
MAX_BYTES = int(float(os.getenv("MAX_MB", "7.5")) * 1024 * 1024)
LW, LH = 1280, 720
BG1, BG2 = (12, 16, 28), (26, 32, 50)
C_UP, C_DN, ORANGE = (0, 224, 130), (255, 72, 92), (255, 159, 28)
CYAN, AMBER = (0, 229, 255), (255, 190, 40)


@functools.lru_cache(maxsize=64)
def _ui(size, bold=True):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for p in (os.getenv("CHART_FONT", ""), f"/usr/share/fonts/truetype/dejavu/{name}"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _arrow(d, cx, cy, up, size, fill, outline=None):
    shape = [(0, -1.0), (0.9, -0.1), (0.35, -0.1), (0.35, 0.9),
             (-0.35, 0.9), (-0.35, -0.1), (-0.9, -0.1)]
    poly = [(cx + px * size, cy + (py if up else -py) * size) for px, py in shape]
    d.polygon(poly, fill=fill, outline=outline)


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


def _candles(pts, step=30):
    b = {}
    for t, v in pts:
        b.setdefault(min(int(t // step), 900 // step - 1), []).append(v)
    out, prev = [], None
    for i in sorted(b):
        vs = b[i]
        o = prev if prev is not None else vs[0]
        out.append((i, o, max(vs + [o]), min(vs + [o]), vs[-1]))
        prev = vs[-1]
    return out


def _ema(vals, n):
    a = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(out[-1] + a * (v - out[-1]))
    return out


def render_frames(cur, outcome, S, n):
    Wd, Hd = int(LW * S), int(LH * S)
    k = lambda v: v * S
    lw1 = max(1, int(k(1)))
    pts = cur.get("pts") or [[0, 50]]
    cs = _candles(pts)
    side = cur.get("side")
    waiting = side is None
    is_up = side == "UP"
    yes_last = pts[-1][1]
    last = yes_last if (is_up or waiting) else 100 - yes_last
    best = max(yes_last, 100 - yes_last)
    conf = cur.get("conf") or (best if waiting else last)
    full = (not waiting) and (outcome == "win" or (outcome not in ("loss", "settling") and last >= 80))
    if waiting:
        col = CYAN
    elif outcome == "settling":
        col = AMBER
    else:
        col = C_UP if is_up else C_DN
    hot = tuple(min(255, c + 150) for c in col)
    fs, fm, fl, fx_big = _ui(int(k(11)), False), _ui(int(k(13))), _ui(int(k(20))), _ui(int(k(40)))
    grey = (120, 132, 156, 255)

    # plot geometry and auto-zoomed y range
    X0, X1, Y0, Y1 = k(30), k(860), k(120), k(500)
    lo, hi = min(c[3] for c in cs) - 4, max(c[2] for c in cs) + 4
    if hi - lo < 20:
        mid = (hi + lo) / 2
        lo, hi = mid - 10, mid + 10
    lo, hi = max(0.0, lo), min(100.0, hi)
    if hi - lo < 20:
        if lo <= 0:
            hi = 20.0
        else:
            lo = hi - 20.0
    Y = lambda v: Y1 - (v - lo) / (hi - lo) * (Y1 - Y0)
    X = lambda t: X0 + (X1 - X0) * min(t, 900) / 900
    cw = (X1 - X0) / 30

    base = ImageOps.colorize(Image.linear_gradient("L").resize((Wd, Hd)), BG1, BG2).convert("RGBA")
    d = ImageDraw.Draw(base, "RGBA")

    # grid + axes
    for i in range(6):
        v = lo + (hi - lo) * i / 5
        _dots(d, X0, Y(v), X1, Y(v), (140, 160, 200, 40), lw1, k(2), k(5))
        d.text((X1 + k(10), Y(v) - k(7)), f"{v:.1f}", fill=grey, font=fs)
    for m in range(0, 16, 3):
        _dots(d, X(m * 60), Y0, X(m * 60), k(600), (140, 160, 200, 26), lw1, k(2), k(6))
        d.text((X(m * 60) - k(8), k(610)), f"{m}m", fill=grey, font=fs)

    # no-call band
    bot_v, top_v = 100 - MIN_CONF, MIN_CONF
    if bot_v < hi and top_v > lo:
        yb, ya = max(Y0, Y(min(top_v, hi))), min(Y1, Y(max(bot_v, lo)))
        d.rectangle([X0, yb, X1, ya], fill=AMBER + (12,))
        hh = max(2, int(ya - yb))
        hatch = Image.new("RGBA", (int(X1 - X0), hh), (0, 0, 0, 0))
        hd = ImageDraw.Draw(hatch)
        for xs in range(-hh, int(X1 - X0), int(k(14))):
            hd.line([(xs, hh), (xs + hh, 0)], fill=AMBER + (20,), width=lw1)
        base.alpha_composite(hatch, (int(X0), int(yb)))
        d = ImageDraw.Draw(base, "RGBA")
        if lo < top_v < hi:
            _dots(d, X0, Y(top_v), X1, Y(top_v), AMBER + (170,), lw1 * 2, k(6), k(5))
            d.text((X0 + k(8), Y(top_v) - k(15)), f"UP CALL > {MIN_CONF:.0f}", fill=AMBER + (210,), font=fs)
        if lo < bot_v < hi:
            _dots(d, X0, Y(bot_v), X1, Y(bot_v), AMBER + (170,), lw1 * 2, k(6), k(5))
            d.text((X0 + k(8), Y(bot_v) + k(3)), f"DOWN CALL < {bot_v:.0f}", fill=AMBER + (210,), font=fs)

    # activity bars
    vy0, vy1 = k(525), k(600)
    acts = [(c[2] - c[3]) + 0.3 for c in cs]
    mx = max(acts)
    d.text((X0, vy0 - k(16)), "ACTIVITY (HIGH-LOW RANGE)", fill=grey, font=fs)
    for c, a in zip(cs, acts):
        x = X0 + (c[0] + 0.5) * cw
        cc = C_UP if c[4] >= c[1] else C_DN
        d.rectangle([x - cw * 0.3, vy1 - a / mx * (vy1 - vy0), x + cw * 0.3, vy1], fill=cc + (130,))

    # candles + EMAs (with glow)
    gl = Image.new("RGBA", (Wd, Hd), (0, 0, 0, 0))
    g = ImageDraw.Draw(gl)
    for (i, o, h, lw_, c) in cs:
        x = X0 + (i + 0.5) * cw
        cc = C_UP if c >= o else C_DN
        g.line([(x, Y(h)), (x, Y(lw_))], fill=cc + (255,), width=max(1, int(k(2))))
        top, bot = Y(max(o, c)), Y(min(o, c))
        if bot - top < k(2):
            bot = top + k(2)
        g.rectangle([x - cw * 0.3, top, x + cw * 0.3, bot], fill=cc + (255,))
    if len(cs) > 1:
        xs = [X0 + (c[0] + 0.5) * cw for c in cs]
        closes = [c[4] for c in cs]
        for n_, ecol in ((3, CYAN), (8, ORANGE)):
            g.line([(x, Y(v)) for x, v in zip(xs, _ema(closes, n_))],
                   fill=ecol + (255,), width=max(1, int(k(2.5))), joint="curve")
    glow = gl.filter(ImageFilter.GaussianBlur(k(4)))
    base.alpha_composite(glow)
    base.alpha_composite(gl)
    d = ImageDraw.Draw(base, "RGBA")
    d.text((X0 + k(8), Y0 + k(4)), "EMA3", fill=CYAN + (255,), font=fs)
    d.text((X0 + k(52), Y0 + k(4)), "EMA8", fill=ORANGE + (255,), font=fs)

    # head position + price tag
    lc = cs[-1]
    lcx, ly = X0 + (lc[0] + 0.5) * cw, Y(lc[4])
    d.rectangle([X1 + k(4), ly - k(10), X1 + k(66), ly + k(10)], fill=col + (255,))
    d.text((X1 + k(9), ly - k(8)), f"{yes_last:.1f}", fill=(8, 10, 20, 255), font=fm)

    # header
    chg = yes_last - cs[0][1]
    d.rectangle([X0, k(20), X0 + k(5), k(86)], fill=col + (255,))
    d.text((X0 + k(16), k(18)), "ETH/USD", fill=(240, 246, 255, 255), font=fl)
    d.text((X0 + k(112), k(23)), "15M  |  KALSHI YES", fill=grey, font=fm)
    d.text((X0 + k(16), k(44)), f"{yes_last:.1f}%", fill=ORANGE + (255,), font=fx_big)
    d.text((X0 + k(16) + d.textlength(f"{yes_last:.1f}%", font=fx_big) + k(14), k(62)),
           f"{chg:+.1f}", fill=(C_UP if chg >= 0 else C_DN) + (255,), font=fl)

    # signal panel
    PX0, PX1, PY0, PY1 = k(910), k(1250), k(90), k(640)
    d.rounded_rectangle([PX0, PY0, PX1, PY1], radius=k(14), fill=(255, 255, 255, 10),
                        outline=(255, 255, 255, 36), width=lw1)
    pl, pr = PX0 + k(20), PX1 - k(20)
    d.text((pl, PY0 + k(14)), "SIGNAL", fill=grey, font=fs)
    d.text((PX0 + k(110), k(138)), side if side else "NO CALL", fill=col + (255,), font=fx_big if side else fl)
    d.text((PX0 + k(110), k(188)), f"{conf:.0f}% CONF" + ("  LOCKED" if side else ""), fill=col + (255,), font=fm)
    d.text((pl, k(236)), "CONFIDENCE" if side else "BEST SIDE", fill=grey, font=fs)
    gy0, gy1 = k(256), k(268)
    d.rectangle([pl, gy0, pr, gy1], fill=(255, 255, 255, 22))
    d.rectangle([pl, gy0, pl + (pr - pl) * max(0, min(conf, 100)) / 100, gy1], fill=col + (255,))
    tx = pl + (pr - pl) * MIN_CONF / 100
    d.line([(tx, gy0 - k(5)), (tx, gy1 + k(5))], fill=AMBER + (255,), width=max(1, int(k(2))))
    d.text((tx - k(8), gy1 + k(7)), f"{MIN_CONF:.0f}", fill=AMBER + (255,), font=fs)

    tl = "--:--"
    if cur.get("close_ts"):
        s_left = 0 if outcome else max(0, int(cur["close_ts"] - time.time()))
        tl = f"{s_left // 60}:{s_left % 60:02d}"
    rows = [("STRIKE", f"{cur['strike']:,.2f}" if cur.get("strike") else "--"),
            ("ETH PRICE", f"${cur['price']:,.2f}" if cur.get("price") else "--"),
            ("TIME LEFT", tl),
            ("ENTRY AT", f"{cur['called_at'] // 60}:{cur['called_at'] % 60:02d}" if cur.get("called_at") is not None else "--")]
    for i, (a, b) in enumerate(rows):
        y = k(320 + i * 34)
        d.text((pl, y), a, fill=grey, font=fs)
        d.text((pr - d.textlength(b, font=fm), y - k(2)), b, fill=(235, 242, 255, 255), font=fm)
        d.line([(pl, y + k(22)), (pr, y + k(22))], fill=(255, 255, 255, 16), width=lw1)

    d.text((pl, k(500)), "MARKET DEPTH", fill=grey, font=fs)
    by0, by1 = k(522), k(540)
    yw = (pr - pl) * max(0, min(yes_last, 100)) / 100
    d.rectangle([pl, by0, pl + yw, by1], fill=C_UP + (200,))
    d.rectangle([pl + yw, by0, pr, by1], fill=C_DN + (200,))
    d.text((pl, by1 + k(6)), f"YES {yes_last:.0f}%", fill=C_UP + (255,), font=fm)
    nt = f"NO {100 - yes_last:.0f}%"
    d.text((pr - d.textlength(nt, font=fm), by1 + k(6)), nt, fill=C_DN + (255,), font=fm)

    # progress bar
    d.line([(X0, k(702)), (X1, k(702))], fill=CYAN + (45,), width=max(1, int(k(3))))
    d.line([(X0, k(702)), (X(pts[-1][0]), k(702))], fill=col + (255,), width=max(1, int(k(3))))

    if outcome == "win":
        label = "WIN"
    elif outcome == "loss":
        label = "LOSS"
    elif outcome == "settling":
        label = "SETTLING"
    elif waiting:
        label = "SCANNING"
    elif full:
        label = "FULL SEND" if is_up else "FULL SINK"
    else:
        label = "HOLDING"
    chip_w = d.textlength("SETTLING...", font=fl) + k(28)
    sgn = -1 if is_up else 1
    frames = []
    for f in range(n):
        t = f / n if n > 1 else 0.25
        sn = math.sin(2 * math.pi * t)
        im = base.copy()
        fx = Image.new("RGBA", (Wd, Hd), (0, 0, 0, 0))
        fd = ImageDraw.Draw(fx)
        sx = X0 + (X1 - X0) * t
        for i in range(28):
            x = sx - i * k(2)
            if x >= X0:
                fd.line([(x, Y0), (x, Y1)], fill=col + (int(38 * (1 - i / 28)),), width=max(1, int(k(2))))
        fd.line([(sx, Y0), (sx, Y1)], fill=hot + (110,), width=lw1)
        _dots(fd, lcx, ly, X1, ly, col + (210,), max(1, int(k(1.5))), k(4), k(5), phase=t * k(9))
        r = k(9) + k(3) * math.sin(2 * math.pi * 2 * t)
        fd.ellipse([lcx - r, ly - r, lcx + r, ly + r], outline=col + (255,), width=max(1, int(k(2))))
        fd.ellipse([lcx - k(3), ly - k(3), lcx + k(3), ly + k(3)], fill=(255, 255, 255, 255))
        fd.rectangle([lcx - cw * 0.5, Y(lc[2]) - k(4), lcx + cw * 0.5, Y(lc[3]) + k(4)],
                     outline=col + (int(110 + 90 * sn),), width=lw1)
        pcx, pcy = PX0 + k(56), k(172)
        if side:
            bob = k(4) * sn if outcome is None else 0
            _arrow(fd, pcx, pcy + bob, is_up, k(34), col + (255,), (255, 255, 255, 255))
            if outcome is None:
                ay = (Y(lc[2]) - k(28) - bob) if is_up else (Y(lc[3]) + k(28) + bob)
                ay = max(Y0 + k(14), min(Y1 - k(14), ay))
                _arrow(fd, lcx, ay, is_up, k(16 if full else 11), col + (255,), (255, 255, 255, 255))
        else:
            for ph in (0.0, 0.5):
                rr = k(8 + 32 * ((t + ph) % 1))
                fd.ellipse([pcx - rr, pcy - rr, pcx + rr, pcy + rr],
                           outline=col + (int(230 * (1 - ((t + ph) % 1))),), width=max(1, int(k(2))))
        if full and outcome is None:
            for j in range(8):
                ph = (t + j * 0.125) % 1
                x = lcx + (j - 4) * k(5)
                y0 = ly + sgn * (k(14) + ph * k(70))
                fd.line([(x, y0), (x, y0 + sgn * k(11))], fill=col + (int(220 * (1 - ph)),), width=max(1, int(k(2))))
        if outcome in ("win", "loss"):
            fd.rectangle([k(2), k(2), Wd - k(3), Hd - k(3)], outline=col + (int(100 + 90 * sn),), width=max(2, int(k(3))))
        im.alpha_composite(fx.filter(ImageFilter.GaussianBlur(k(5))))
        im.alpha_composite(fx)
        d2 = ImageDraw.Draw(im, "RGBA")
        txt = label + ("." * (f % 4) if label in ("SCANNING", "SETTLING") else "")
        x0 = Wd - k(30) - chip_w
        d2.rectangle([x0, k(24), Wd - k(30), k(60)], fill=col + (40,), outline=col + (255,), width=max(1, int(k(2))))
        tx2 = x0 + k(14)
        if outcome in ("win", "loss") and n > 1 and f % 6 == 0:
            d2.text((tx2 - k(2), k(28)), txt, fill=(255, 0, 200, 200), font=fl)
            d2.text((tx2 + k(2), k(28)), txt, fill=(0, 240, 255, 200), font=fl)
        d2.text((tx2, k(28)), txt, fill=(255, 255, 255, 255) if outcome else col + (255,), font=fl)
        frames.append(im.convert("RGB"))
    return frames


def chart_file(cur, outcome=None):
    """Returns (bytes, filename, mime). Live = 1080p GIF, final = 4K PNG, auto-shrinks if too big."""
    S = FINAL_SCALE if outcome in ("win", "loss") else LIVE_SCALE
    while True:
        frames = render_frames(cur, outcome, S, 1 if S > 2 else 12)
        buf = io.BytesIO()
        if len(frames) == 1:
            frames[0].save(buf, format="PNG", optimize=True)
            res = (buf.getvalue(), "chart.png", "image/png")
        else:
            frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=80, loop=0)
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
        {"name": "Last 20", "value": s["last20"], "inline": True},
        {"name": "Streak", "value": s["streak"], "inline": True},
        {"name": "UP calls", "value": s["up"], "inline": True},
        {"name": "DOWN calls", "value": s["down"], "inline": True},
    ]

    if side is None:
        best = max(p_up, 100 - p_up)
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
        lines = [
            f"**Call:** {arrow} {side} ({conf:.0f}% confidence at entry, 🔒 locked)",
            f"**Action:** {'HOLD (if holding ' + ('YES' if side == 'UP' else 'NO') + ')' if not outcome else tag}",
            f"**Kalshi** YES {yes:.0f}% / NO {100 - yes:.0f}%",
        ]
        title = f"{arrow} KALSHI ETH 15m: {side} | {tag}"

    if pm is not None:
        lines.append(f"**Strike model** UP {pm:.0f}% / DOWN {100 - pm:.0f}%")
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
            state["history"].append({"ticker": p["ticker"], "side": p["side"], "win": win})
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
    price, n_ex = eth_price()
    record_price(price)
    strike = float(m.get("floor_strike") or m.get("cap_strike") or 0)
    blend, pm = predict(yes, price, strike, secs_left)

    if cur is None:
        cur = {"ticker": m["ticker"], "side": None, "strike": strike, "close_ts": close_ts,
               "message_id": None, "pts": [], "locked": False, "conf": None}
        state["current"] = cur

    if cur["side"] is None and secs_left > CALL_CUTOFF:
        if blend > MIN_CONF:
            cur.update(side="UP", conf=blend, locked=True, called_at=round(elapsed))
        elif (100 - blend) > MIN_CONF:
            cur.update(side="DOWN", conf=100 - blend, locked=True, called_at=round(elapsed))

    cur["price"] = price
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
        time.sleep(POLL)
    save_state(state)


if __name__ == "__main__":
    main()
