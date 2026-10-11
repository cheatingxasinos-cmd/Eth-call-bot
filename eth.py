import functools
import io
import json
import math
import os
import statistics
import time
from collections import deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageChops

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

LOGO_PATH = os.getenv("LOGO_PATH", "eth_logo.png")
CHART_TZ = ZoneInfo(os.getenv("CHART_TZ", "America/New_York"))
LIVE_SCALE = float(os.getenv("LIVE_SCALE", "1.5"))    # 1.5 = 1080x1200 animated GIF
FINAL_SCALE = float(os.getenv("FINAL_SCALE", "3"))    # 3 = 2160x2400 still PNG
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
    return math.log(p_now / p0) / (t_now - t0)


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


# ---------- chart (mobile card style) ----------
CW, CH = 720, 800
WHITE = (255, 255, 255, 255)
MUTED = (160, 168, 178, 255)
C_UP, C_DN, ORG = (0, 217, 142), (255, 87, 87), (255, 140, 58)
AMBER = (255, 190, 40)


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


@functools.lru_cache(maxsize=8)
def _logo(px):
    try:
        im = ImageOps.fit(Image.open(LOGO_PATH).convert("RGBA"), (px, px), Image.LANCZOS)
    except Exception:
        im = Image.new("RGBA", (px, px), (15, 26, 61, 255))
        g = ImageDraw.Draw(im)
        c = px / 2
        u = px / 100
        top, mid, low = (c, c - 36 * u), (c, c + 8 * u), (c, c + 38 * u)
        lft, rgt = (c - 22 * u, c - 2 * u), (c + 22 * u, c - 2 * u)
        g.polygon([top, rgt, mid], fill=(232, 159, 240, 255))
        g.polygon([top, lft, mid], fill=(155, 77, 202, 255))
        g.polygon([lft, mid, (c - 22 * u, c + 6 * u)], fill=(120, 60, 180, 255))
        g.polygon([lft, mid, low], fill=(155, 77, 202, 255))
        g.polygon([rgt, mid, low], fill=(232, 159, 240, 255))
    m = Image.new("L", (px * 4, px * 4), 0)
    ImageDraw.Draw(m).ellipse([0, 0, px * 4 - 1, px * 4 - 1], fill=255)
    m = m.resize((px, px), Image.LANCZOS)
    out = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    out.paste(im, (0, 0), m)
    return out


def _clock(t):
    dt = datetime.fromtimestamp(t, CHART_TZ)
    return f"{dt.hour % 12 or 12}:{dt.minute:02d}{'am' if dt.hour < 12 else 'pm'}"


def _bg(Wd, Hd):
    h = Image.linear_gradient("L").transpose(Image.Transpose.ROTATE_90).resize((Wd, Hd))
    top = ImageOps.colorize(h, (13, 31, 31), (45, 26, 31))
    v = Image.linear_gradient("L").resize((Wd, Hd)).point(lambda p: min(255, int(p * 1.7)))
    return Image.composite(Image.new("RGB", (Wd, Hd), (10, 10, 10)), top, v).convert("RGBA")


def render_frames(cur, outcome, S, n):
    Wd, Hd = int(CW * S), int(CH * S)
    k = lambda v: v * S
    F = lambda sz, b=True: _ui(int(k(sz)), b)
    lw = lambda v: max(1, int(k(v)))

    pts = cur.get("pts") or [[0, 50]]
    eth = cur.get("eth") or []
    side = cur.get("side")
    waiting = side is None
    is_up = side == "UP"
    yes_last = pts[-1][1]
    up_pct, dn_pct = yes_last, 100 - yes_last
    conf = cur.get("conf") or max(up_pct, dn_pct)
    strike = cur.get("strike") or (eth[0][1] if eth else 0)
    now_px = eth[-1][1] if eth else (cur.get("price") or strike)

    if outcome == "win":
        accent, label = C_UP, "WIN"
    elif outcome == "loss":
        accent, label = C_DN, "LOSS"
    elif outcome == "settling":
        accent, label = AMBER, "SETTLING"
    else:
        accent, label = None, None

    base = _bg(Wd, Hd)
    d = ImageDraw.Draw(base, "RGBA")

    # ---- header ----
    lg = int(k(60))
    base.alpha_composite(_logo(lg), (int(k(24)), int(k(24))))
    d.ellipse([k(24), k(24), k(24) + lg, k(24) + lg], outline=(255, 255, 255, 70), width=lw(1.5))
    d.text((k(98), k(54)), "ETH 15 min", font=F(30), fill=WHITE, anchor="lm")
    s_left = 0 if outcome else max(0, int((cur.get("close_ts") or time.time()) - time.time()))
    badge = label or f"{s_left // 60}:{s_left % 60:02d}"
    bw = max(104, int(len(badge) * 15 + 40))
    bcol = (accent or WHITE[:3])
    d.rounded_rectangle([k(696 - bw), k(30), k(696), k(76)], radius=k(14), fill=(255, 255, 255, 26),
                        outline=(bcol + (200,)) if accent else None, width=lw(2))
    d.text((k(696 - bw / 2), k(53)), badge, font=F(22), fill=(bcol + (255,)) if accent else WHITE, anchor="mm")

    # ---- price row ----
    close_ts = cur.get("close_ts") or time.time()
    d.text((k(24), k(124)), f"Target · {_clock(close_ts - 900)}", font=F(15, False), fill=MUTED, anchor="ls")
    d.text((k(24), k(166)), f"${strike:,.2f}" if strike else "--", font=F(32), fill=WHITE, anchor="ls")
    diff = (now_px - strike) if strike else 0
    dcol = C_UP if diff >= 0 else C_DN
    d.text((k(372), k(124)), f"Now {'↑' if diff >= 0 else '↓'} ${abs(diff):,.2f}",
           font=F(15), fill=dcol + (255,), anchor="ls")
    d.text((k(372), k(166)), f"${now_px:,.2f}" if now_px else "--", font=F(32), fill=WHITE, anchor="ls")
    d.ellipse([k(673), k(136), k(695), k(158)], outline=MUTED, width=lw(2))
    d.text((k(684), k(147)), "i", font=F(15), fill=MUTED, anchor="mm")

    # ---- chart ----
    PY0, PY1, PX0, PX1 = k(250), k(560), k(84), k(676)
    vals = [v for _, v in eth] + ([strike] if strike else [])
    if not vals:
        vals = [0.0, 1.0]
    lo, hi = min(vals), max(vals)
    span = max(hi - lo, (strike or hi or 1) * 0.0003)
    mid = (hi + lo) / 2
    lo, hi = mid - span / 2 * 1.3, mid + span / 2 * 1.3
    Y = lambda v: PY1 - (v - lo) / (hi - lo) * (PY1 - PY0)
    hx = hy = None
    if eth:
        t0, t1 = eth[0][0], eth[-1][0]
        tspan = max(t1 - t0, 30)
        co = [(PX0 + (PX1 - PX0) * (t - t0) / tspan, Y(v)) for t, v in eth]
        if len(co) < 2:
            co = [co[0], (co[0][0] + k(4), co[0][1])]
        hx, hy = co[-1]
        top_y = max(PY0, min(y for _, y in co))
        mask = Image.new("L", (Wd, Hd), 0)
        ImageDraw.Draw(mask).polygon(co + [(hx, PY1), (co[0][0], PY1)], fill=255)
        fade = Image.new("L", (Wd, Hd), 0)
        gh = max(2, int(PY1 - top_y))
        grad = ImageOps.flip(Image.linear_gradient("L").resize((Wd, gh))).point(lambda v: int(v * 0.45))
        fade.paste(grad, (0, int(top_y)))
        fill = Image.new("RGBA", (Wd, Hd), (255, 140, 58, 0))
        fill.putalpha(ImageChops.multiply(mask, fade))
        base.alpha_composite(fill)
        d = ImageDraw.Draw(base, "RGBA")
        d.line(co, fill=ORG + (255,), width=lw(3), joint="curve")
        for (x, y) in (co[0], co[-1]):
            d.ellipse([x - k(1.5), y - k(1.5), x + k(1.5), y + k(1.5)], fill=ORG + (255,))
    # left delta labels
    for fr in (0.2, 0.5, 0.8):
        v = lo + (hi - lo) * fr
        dd = v - strike
        c = C_UP if dd >= 0 else C_DN
        d.text((k(24), Y(v)), f"{'+' if dd >= 0 else '-'} ${abs(dd):,.0f}", font=F(15), fill=c + (255,), anchor="lm")

    # ---- status row ----
    d.rounded_rectangle([k(24), k(586), k(696), k(650)], radius=k(18), fill=(255, 255, 255, 14))
    base.alpha_composite(_logo(int(k(40))), (int(k(36)), int(k(598))))
    if waiting:
        l1 = "Scanning, no call yet"
        l2 = f"Best side {max(up_pct, dn_pct):.0f}%, needs above {MIN_CONF:.0f}%"
    else:
        l1 = f"{side} call locked at {conf:.0f}%"
        ca = cur.get("called_at")
        l2 = f"Entry at {ca // 60}:{ca % 60:02d} into the window" if ca is not None else "Entry locked"
    tx = k(92)
    if not waiting:
        _arrow(d, k(104), k(611), is_up, k(10), (C_UP if is_up else C_DN) + (255,))
        tx = k(122)
    d.text((tx, k(610)), l1, font=F(17), fill=WHITE, anchor="lm")
    d.text((k(92), k(633)), l2, font=F(13, False), fill=MUTED, anchor="lm")

    # ---- buttons ----
    by0, by1 = k(672), k(772)
    bwid = (720 - 48 - 14) / 2
    for i, (name, pct, col, is_up_btn) in enumerate((("Up", up_pct, C_UP, True), ("Down", dn_pct, C_DN, False))):
        x0 = k(24 + i * (bwid + 14))
        x1 = x0 + k(bwid)
        called = (not waiting) and (is_up_btn == is_up)
        dim = (not waiting) and not called
        a = 90 if dim else 255
        d.rounded_rectangle([x0, by0, x1, by1], radius=k(30), fill=col + (a,),
                            outline=(255, 255, 255, 255) if called else None, width=lw(3))
        _arrow(d, x0 + k(40), (by0 + by1) / 2, is_up_btn, k(15), (255, 255, 255, 255 if not dim else 150))
        d.text(((x0 + x1) / 2 + k(14), by0 + k(36)), f"{name} · {pct:.0f}%", font=F(24),
               fill=(255, 255, 255, 255 if not dim else 170), anchor="mm")
        d.text(((x0 + x1) / 2 + k(14), by0 + k(72)), f"{100 / max(pct, 1):.2f}x", font=F(16, False),
               fill=(255, 255, 255, 220 if not dim else 120), anchor="mm")

    # ---- animated frames ----
    ty = Y(strike) if strike else (PY0 + PY1) / 2
    frames = []
    for f in range(n):
        t = f / n if n > 1 else 0.25
        sn = math.sin(2 * math.pi * t)
        im = base.copy()
        fd = ImageDraw.Draw(im, "RGBA")
        _dots(fd, k(24), ty, k(696), ty, (190, 196, 206, 190), lw(1.5), k(6), k(5), phase=t * k(11))
        fd.text((k(696), ty - k(10)), "TARGET ↓", font=F(13), fill=(190, 196, 206, 255), anchor="rs")
        if hx is not None:
            r = k(6 + 1.2 * sn)
            fd.ellipse([hx - r, hy - r, hx + r, hy + r], fill=ORG + (255,))
        if accent and outcome in ("win", "loss"):
            fd.rounded_rectangle([k(3), k(3), Wd - k(4), Hd - k(4)], radius=k(24),
                                 outline=accent + (int(110 + 90 * sn),), width=lw(4))
        frames.append(im.convert("RGB"))
    return frames


def chart_file(cur, outcome=None):
    """Returns (bytes, filename, mime). Live = animated GIF, final = still PNG, shrinks if too big."""
    S = FINAL_SCALE if outcome in ("win", "loss") else LIVE_SCALE
    while True:
        frames = render_frames(cur, outcome, S, 1 if S > 2 else 10)
        buf = io.BytesIO()
        if len(frames) == 1:
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
               "message_id": None, "pts": [], "eth": [], "locked": False, "conf": None}
        state["current"] = cur

    if cur["side"] is None and secs_left > CALL_CUTOFF:
        if blend > MIN_CONF:
            cur.update(side="UP", conf=blend, locked=True, called_at=round(elapsed))
        elif (100 - blend) > MIN_CONF:
            cur.update(side="DOWN", conf=100 - blend, locked=True, called_at=round(elapsed))

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
        time.sleep(POLL)
    save_state(state)


if __name__ == "__main__":
    main()
