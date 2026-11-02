import functools
import io
import json
import math
import os
import random
import re
import time
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
ODDS_KEY = os.environ.get("MONEYLINE_API_KEY", "")
ML_URL = os.getenv("MONEYLINE_URL", "https://mlapi.bet/v1/events/today")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"

# Kalshi series scanned every tick (free, no key).
SERIES = [s.strip() for s in os.getenv(
    "SERIES", "KXNBAGAME,KXNFLGAME,KXNHLGAME,KXNCAAFGAME,KXNCAAMBGAME,KXEPLGAME,KXMLSGAME"
).split(",") if s.strip()]

SRC_LABEL = "MoneyLine"
POLL = float(os.getenv("POLL_SECONDS", "15"))
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.10"))
PMIN, PMAX = float(os.getenv("PMIN", "0.15")), float(os.getenv("PMAX", "0.92"))
HOURS_AHEAD = float(os.getenv("HOURS_AHEAD", "48"))
BOOK_W = float(os.getenv("BOOK_W", "0.75"))
ODDS_TTL = float(os.getenv("ODDS_TTL", "300"))           # seconds between MoneyLine refreshes
DAILY_CREDITS = int(os.getenv("DAILY_CREDITS", "330"))   # hard cap per UTC day
MONTHLY_CREDITS = int(os.getenv("MONTHLY_CREDITS", "9900"))
CALL_COST = int(os.getenv("CALL_COST", "1"))             # credits one MoneyLine call uses
STALE_MAX = float(os.getenv("STALE_MAX", "7200"))        # reuse old odds this long when over budget
BUDGET = {"day": "", "used": 0, "month": "", "month_used": 0}
API_DOWN = [False]
TOP_N = int(os.getenv("TOP_N", "5"))
MAX_OPEN = int(os.getenv("MAX_OPEN", "25"))
KELLY_FRAC = float(os.getenv("KELLY_FRAC", "0.25"))
MAX_STAKE = float(os.getenv("MAX_STAKE", "0.05"))
MIN_GROWTH = float(os.getenv("MIN_GROWTH", "0.0002"))
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.015"))
UNCERT = float(os.getenv("UNCERT", "0.01"))
REQUIRE_BOOK = os.getenv("REQUIRE_BOOK", "1" if ODDS_KEY else "0") == "1"
ARB_MIN = float(os.getenv("ARB_MIN", "0.01"))          # min locked profit per set, in dollars
ARB_REPEAT = float(os.getenv("ARB_REPEAT", "1800"))    # seconds before re-alerting the same game
RUN_SECONDS = int(os.getenv("RUN_SECONDS", str(345 * 60)))
STATE_FILE = os.getenv("STATE_FILE", "sports_state.json")
EDIT_SECONDS = float(os.getenv("EDIT_SECONDS", "120"))
LIVE_SCALE = float(os.getenv("LIVE_SCALE", "1.0"))
FINAL_SCALE = float(os.getenv("FINAL_SCALE", "2"))
MAX_BYTES = int(float(os.getenv("MAX_MB", "7.5")) * 1024 * 1024)
GREEN, RED, GREY = 0x00FF00, 0xFF3B30, 0x808080


# ---------- http ----------
def _session(retries):
    s = requests.Session()
    ad = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=retries)
    s.mount("https://", ad)
    s.headers["User-Agent"] = "sports-ev-bot/1.0"
    return s


DATA = _session(Retry(total=2, backoff_factor=0.2, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset(["GET"]), respect_retry_after_header=True))
HOOK = _session(0)


def get_json(url, params=None, timeout=(3.05, 10)):
    r = DATA.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def get_json_h(url, headers, params=None):
    r = DATA.get(url, params=params, headers=headers, timeout=(3.05, 10))
    r.raise_for_status()
    return r.json()


def ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _t(v):
    return float(v) if isinstance(v, (int, float)) else ts(str(v))


# ---------- state ----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("history", [])
    s.setdefault("open", {})
    s.setdefault("arbs", {})
    s.setdefault("arb_count", 0)
    s.setdefault("arb_locked", 0.0)
    s.setdefault("budget", {})
    return s


def save_state(s):
    s["history"] = s["history"][-1000:]
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(s, f)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


# ---------- kalshi (public, no key) ----------
def dollars(m, key):
    v = m.get(key + "_dollars")
    v = float(v) if v is not None else (float(m[key]) / 100 if m.get(key) is not None else None)
    return v if v and v > 0 else None


def kalshi_markets(series):
    return get_json(f"{KALSHI}/markets", {"series_ticker": series, "status": "open",
                                          "limit": 1000}).get("markets", [])


def kalshi_market(ticker):
    return get_json(f"{KALSHI}/markets/{ticker}")["market"]


def fee(p):
    """Kalshi taker fee for one contract, rounded up to the cent."""
    return math.ceil(0.07 * p * (1 - p) * 100) / 100


# ---------- sportsbook odds (MoneyLine, optional, credit-capped) ----------
ODDS_CACHE = {}
ML_DEBUG = [True]

STOP = {"state", "university", "college", "the", "city", "united", "fc", "sc", "new", "los", "san",
        "saint", "north", "south", "east", "west", "tech", "univ", "and", "vs"}


def american_to_prob(v):
    if v is None:
        return None
    s = str(v).strip().upper()
    if s in ("EVEN", "EV", "PK"):
        return 0.5
    try:
        n = float(s.replace("+", ""))
    except ValueError:
        return None
    if n == 0:
        return None
    return 100 / (n + 100) if n > 0 else -n / (-n + 100)


def _words(s):
    return {w for w in re.findall(r"[a-z]+", (s or "").lower()) if len(w) >= 3 and w not in STOP}


def _budget_roll():
    now = datetime.now(timezone.utc)
    d, m = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    if BUDGET["day"] != d:
        BUDGET["day"], BUDGET["used"] = d, 0
        API_DOWN[0] = False
    if BUDGET["month"] != m:
        BUDGET["month"], BUDGET["month_used"] = m, 0


def odds_games(series):
    """One MoneyLine call covers every sport. Hard-capped by daily and monthly credits."""
    if not ODDS_KEY:
        return []
    _budget_roll()
    c = ODDS_CACHE.get("ALL")
    age = time.time() - c[0] if c else None
    if c and age < ODDS_TTL:
        return c[1]
    over = (BUDGET["used"] + CALL_COST > DAILY_CREDITS or
            BUDGET["month_used"] + CALL_COST > MONTHLY_CREDITS)
    if over:
        if c and c[1] and age < STALE_MAX:
            return c[1]                      # recent odds are still fine
        if not API_DOWN[0]:
            print("moneyline budget used up: running on Kalshi data only", flush=True)
        API_DOWN[0] = True
        return []
    BUDGET["used"] += CALL_COST
    BUDGET["month_used"] += CALL_COST
    try:
        data = get_json_h(ML_URL, {"x-api-key": ODDS_KEY})
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (401, 402, 403, 429):
            BUDGET["used"] = DAILY_CREDITS   # provider refused: stop for today
        API_DOWN[0] = True
        ODDS_CACHE["ALL"] = (time.time(), [])
        print("moneyline error", repr(e), "-> Kalshi-only until next refresh", flush=True)
        return []
    API_DOWN[0] = False
    print(f"moneyline call ok: {BUDGET['used']}/{DAILY_CREDITS} credits today, "
          f"{BUDGET['month_used']}/{MONTHLY_CREDITS} this month", flush=True)
    events = data if isinstance(data, list) else (data.get("events") or data.get("data") or [])
    if ML_DEBUG[0] and events:
        ML_DEBUG[0] = False
        print("moneyline sample:", json.dumps(events[0])[:1500], flush=True)
    games = []
    for ev in events:  # field names are guesses: fix them from the sample line
        try:
            probs = {"home": american_to_prob(ev["home_moneyline"]),
                     "away": american_to_prob(ev["away_moneyline"])}
            d_ = american_to_prob(ev.get("draw_moneyline"))
            if d_:
                probs["draw"] = d_
            if probs["home"] is None or probs["away"] is None:
                continue
            tot = sum(probs.values())
            games.append({"start": _t(ev["start_time"]),
                          "fair": {k_: v / tot for k_, v in probs.items()},
                          "home": _words(ev["home_team"]), "away": _words(ev["away_team"])})
        except Exception:
            continue
    ODDS_CACHE["ALL"] = (time.time(), games)
    return games


def match_game(m, games, end):
    sub = (m.get("yes_sub_title") or "").lower().strip()
    subt, title = _words(sub), _words(m.get("title"))
    best = None
    for g in games:
        if not (-3600 < end - g["start"] < 48 * 3600):
            continue
        if sub in ("tie", "draw"):
            if "draw" not in g["fair"] or not (g["home"] & title and g["away"] & title):
                continue
            cand = ("draw", g)
        else:
            sh, sa = len(subt & g["home"]), len(subt & g["away"])
            if sh == sa:
                continue
            side = "home" if sh > sa else "away"
            other = g["away"] if side == "home" else g["home"]
            if not (other & title):
                continue
            cand = (side, g)
        if best is not None:
            return None
        best = cand
    return best


# ---------- pick scoring ----------
def evaluate(m, p_yes, margin):
    ya, yb = dollars(m, "yes_ask"), dollars(m, "yes_bid")
    best = None
    for side, p, price in (("YES", p_yes, ya), ("NO", 1 - p_yes, None if yb is None else 1 - yb)):
        if price is None or not (PMIN <= price <= PMAX):
            continue
        p = p - margin
        c = price + fee(price)
        if c >= 1 or p <= c:
            continue
        w = 1 - c
        f = min(MAX_STAKE, KELLY_FRAC * (p - c) / w)
        g = p * math.log(1 + f * w / c) + (1 - p) * math.log(1 - f)
        cand = {"side": side, "p": p, "price": price, "ev": p - c, "roi": (p - c) / price,
                "edge": p - price, "kelly": f, "growth": g}
        if best is None or g > best["growth"]:
            best = cand
    return best


# ---------- arbitrage (keyless) ----------
def find_arb(ms, now):
    """Mutually exclusive, exhaustive outcomes of one game that can be locked for profit after fees."""
    n = len(ms)
    if n < 2 or n > 3 or not all(in_window(m, now) for m in ms):
        return None
    subs = [(m.get("yes_sub_title") or "").lower().strip() for m in ms]
    if n == 3 and not any(s in ("tie", "draw") for s in subs):
        return None
    asks = [dollars(m, "yes_ask") for m in ms]
    bids = [dollars(m, "yes_bid") for m in ms]
    best = None
    if all(a is not None for a in asks):
        cost = sum(a + fee(a) for a in asks)
        if 1 - cost >= ARB_MIN:
            best = {"kind": "Buy YES on every outcome", "profit": 1 - cost,
                    "legs": [(m, "YES", a) for m, a in zip(ms, asks)]}
    if all(b is not None for b in bids):
        cost = sum((1 - b) + fee(1 - b) for b in bids)
        profit = (n - 1) - cost
        if profit >= ARB_MIN and (best is None or profit > best["profit"]):
            best = {"kind": "Buy NO on every outcome", "profit": profit,
                    "legs": [(m, "NO", 1 - b) for m, b in zip(ms, bids)]}
    return best


LAST_LOG = [0.0]


def in_window(m, now):
    try:
        end = ts(m.get("expected_expiration_time") or m["close_time"])
    except Exception:
        return False
    return 0 < end - now < HOURS_AHEAD * 3600


def scan():
    """Returns (picks, marks, arbs). marks[ticker] = (fair_yes, margin, yes_ask, yes_bid) for live updates."""
    picks, marks, arbs, now = [], {}, [], time.time()
    st = {"markets": 0, "no_quote": 0, "wide": 0, "no_book": 0, "no_edge": 0, "thin": 0, "matched": 0}
    near = None
    for series in SERIES:
        try:
            mkts = kalshi_markets(series)
        except Exception as e:
            print("scan error", series, repr(e), flush=True)
            continue
        games = odds_games(series) if any(in_window(m, now) for m in mkts) else []
        events = {}
        for m in mkts:
            events.setdefault(m.get("event_ticker") or m["ticker"], []).append(m)
        for ev_key, ms in events.items():
            arb = find_arb(ms, now)
            if arb:
                arb["event"] = ev_key
                arb["title"] = (ms[0].get("title") or ev_key).replace(" Winner?", "")
                arbs.append(arb)
            mids, all_ok = {}, True
            for m in ms:
                b, a = dollars(m, "yes_bid"), dollars(m, "yes_ask")
                if b is None or a is None or a < b:
                    mids[m["ticker"]] = None
                    all_ok = False
                    continue
                mids[m["ticker"]] = (a + b) / 2
            tot = sum(v for v in mids.values() if v) if all_ok and len(ms) >= 2 else None
            for m in ms:
                if not in_window(m, now):
                    continue
                end = ts(m.get("expected_expiration_time") or m["close_time"])
                st["markets"] += 1
                mid = mids.get(m["ticker"])
                if mid is None:
                    st["no_quote"] += 1
                    continue
                a, b = dollars(m, "yes_ask"), dollars(m, "yes_bid")
                if a - b > MAX_SPREAD:
                    st["wide"] += 1
                    continue
                kfair = mid / tot if tot else mid
                fair, src, margin = kfair, "Kalshi", UNCERT
                hit = match_game(m, games, end)
                if hit:
                    side, g = hit
                    book_p = g["fair"][side]
                    fair = BOOK_W * book_p + (1 - BOOK_W) * kfair
                    margin = UNCERT + 0.5 * abs(book_p - kfair)
                    src = SRC_LABEL + "+Kalshi"
                    st["matched"] += 1
                elif REQUIRE_BOOK and not API_DOWN[0]:
                    st["no_book"] += 1
                    continue
                marks[m["ticker"]] = (fair, margin, a, b)
                raw = max(fair - a, b - fair)
                if near is None or raw > near[0]:
                    near = (raw, m["ticker"], src)
                r = evaluate(m, fair, margin)
                if not r:
                    st["no_edge"] += 1
                    continue
                if r["growth"] < MIN_GROWTH or r["edge"] < MIN_EDGE:
                    st["thin"] += 1
                    continue
                title = (m.get("title") or "").replace(" Winner?", "")
                picks.append({**r, "ticker": m["ticker"], "series": series, "event": ev_key,
                              "team": m.get("yes_sub_title") or m["ticker"], "title": title,
                              "game": title or m["ticker"], "commence": end,
                              "p_yes": fair, "books": len(ms), "src": src})
    if now - LAST_LOG[0] >= 120:
        LAST_LOG[0] = now
        nr = f"{near[0] * 100:+.1f}c on {near[1]} ({near[2]})" if near else "n/a"
        print(f"scan: {st['markets']} markets | book-matched {st['matched']} | no quote {st['no_quote']} | "
              f"wide {st['wide']} | no book {st['no_book']} | no edge {st['no_edge']} | "
              f"thin {st['thin']} | PICKS {len(picks)} | ARBS {len(arbs)} | "
              f"api {'DOWN' if API_DOWN[0] else 'ok'} | best raw edge {nr}", flush=True)
    return sorted(picks, key=lambda x: -x["growth"]), marks, arbs


# ---------- chart ----------
CW, CH = 720, 880
WHITE, MUTED = (255, 255, 255, 255), (160, 168, 178, 255)
C_UP, C_DN = (0, 217, 142), (255, 87, 87)
ORG, CYAN, AMBER = (255, 140, 58), (0, 229, 255), (255, 190, 40)


@functools.lru_cache(maxsize=64)
def _font(size, bold=True):
    names = ([os.getenv("CHART_FONT", "")] +
             (["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "arialbd.ttf"] if bold
              else ["DejaVuSans.ttf", "LiberationSans-Regular.ttf", "arial.ttf"]))
    for n in names:
        if n:
            try:
                return ImageFont.truetype(n, size)
            except OSError:
                pass
    return ImageFont.load_default()


def _dots(d, x0, y0, x1, y1, fill, w=1, on=3.0, off=5.0):
    L = math.hypot(x1 - x0, y1 - y0)
    if L <= 0:
        return
    ux, uy, p = (x1 - x0) / L, (y1 - y0) / L, 0.0
    while p < L:
        b = min(p + on, L)
        d.line([(x0 + ux * p, y0 + uy * p), (x0 + ux * b, y0 + uy * b)], fill=fill, width=w)
        p += on + off


def _arrow(d, cx, cy, up, size, fill):
    shape = [(0, -1.0), (0.9, -0.1), (0.35, -0.1), (0.35, 0.9), (-0.35, 0.9), (-0.35, -0.1), (-0.9, -0.1)]
    d.polygon([(cx + px * size, cy + (py if up else -py) * size) for px, py in shape], fill=fill)


def _glow(base, fn, color, radius, strength=0.9):
    m = Image.new("L", base.size, 0)
    fn(ImageDraw.Draw(m))
    m = m.filter(ImageFilter.GaussianBlur(radius)).point(lambda v: int(v * strength))
    base.paste(color, (0, 0), m)


def _hhmm(s):
    s = max(0, int(s))
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def render_frames(p, outcome, S, n):
    k = lambda v: v * S
    F = lambda sz, b=True: _font(max(6, int(k(sz))), b)
    lw = lambda v: max(1, int(k(v)))
    W, H = int(CW * S), int(CH * S)
    accent = {"win": C_UP, "loss": C_DN}.get(outcome)
    price, fair = p["price"], p["p"]
    src = p.get("src", "Kalshi")
    edge = (fair - price) * 100
    side_col = C_UP if p["side"] == "YES" else C_DN
    pts = p.get("pts") or [[time.time(), price, fair]]
    t0 = pts[0][0]
    t1 = max(pts[-1][0], t0 + 120)

    g = Image.linear_gradient("L").resize((W, H))
    base = ImageOps.colorize(g, (13, 31, 31), (10, 10, 10)).convert("RGBA")
    d = ImageDraw.Draw(base, "RGBA")
    rng = random.Random(7)
    for _ in range(50):
        sx, sy = rng.randint(0, W), rng.randint(0, int(H * 0.22))
        r = k(rng.choice((0.6, 0.9, 1.2)))
        d.ellipse([sx - r, sy - r, sx + r, sy + r], fill=(255, 255, 255, rng.randint(40, 110)))

    team = p["team"][:22]
    init = "".join(w[0] for w in p["team"].split()[:2]).upper() or "?"
    d.ellipse([k(24), k(24), k(88), k(88)], fill=side_col + (255,), outline=(255, 255, 255, 120), width=lw(2))
    d.text((k(56), k(56)), init, font=F(24), fill=WHITE, anchor="mm")
    d.text((k(104), k(46)), team, font=F(26), fill=WHITE, anchor="lm")
    d.text((k(104), k(74)), p["game"][:44], font=F(13, False), fill=MUTED, anchor="lm")
    left = p["commence"] - time.time()
    ring = outcome is None
    btxt, bcol = (("Closes " + _hhmm(left)), (255, 255, 255)) if ring else (outcome.upper(), accent)
    fb = F(18)
    bw = d.textlength(btxt, font=fb) / S + (62 if ring else 36)
    bx0 = 696 - bw
    d.rounded_rectangle([k(bx0), k(30), k(696), k(78)], radius=k(16), fill=(255, 255, 255, 26),
                        outline=bcol + (190,), width=lw(2))
    if ring:
        cx, cy, r = k(bx0 + 28), k(54), k(11)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 255, 255, 40), width=lw(3))
        frac = max(0.0, min(1.0, left / (HOURS_AHEAD * 3600)))
        if frac > 0.003:
            d.arc([cx - r, cy - r, cx + r, cy + r], -90, -90 + 360 * frac, fill=bcol + (255,), width=lw(3))
        d.text((k(bx0 + 46), k(54)), btxt, font=fb, fill=bcol + (255,), anchor="lm")
    else:
        d.text((k(bx0 + bw / 2), k(54)), btxt, font=fb, fill=bcol + (255,), anchor="mm")

    ev_c = C_UP if edge >= 0 else C_DN
    cards = [("KALSHI PRICE", f"{price * 100:.0f}c", ORG, p["side"] + " ask", None),
             ("IMPLIED FAIR", f"{fair * 100:.1f}%", CYAN, src + " blend", None),
             ("EDGE", f"{edge:+.1f} pts", ev_c, f"EV {p['ev'] * 100:+.1f}c after fees", edge >= 0)]
    cwid = (672 - 20) / 3
    for i, (lab, val, col, sub, arrow) in enumerate(cards):
        x0 = 24 + i * (cwid + 10)
        d.rounded_rectangle([k(x0), k(100), k(x0 + cwid), k(182)], radius=k(14), fill=(255, 255, 255, 16),
                            outline=(255, 255, 255, 30), width=lw(1))
        d.rounded_rectangle([k(x0 + 14), k(100), k(x0 + 14 + 40), k(103)], radius=k(2), fill=col + (255,))
        d.text((k(x0 + 14), k(122)), lab, font=F(11, False), fill=MUTED, anchor="lm")
        d.text((k(x0 + 14), k(150)), val, font=F(26), fill=col + (255,), anchor="lm")
        d.text((k(x0 + 14), k(171)), sub, font=F(10, False), fill=MUTED, anchor="lm")
        if arrow is not None:
            _arrow(d, k(x0 + cwid - 22), k(140), arrow, k(9), col + (255,))

    d.rounded_rectangle([k(12), k(196), k(708), k(562)], radius=k(18), fill=(255, 255, 255, 14),
                        outline=(255, 255, 255, 28), width=lw(1))
    d.text((k(26), k(214)), "PRICE vs FAIR VALUE", font=F(11), fill=MUTED, anchor="lm")
    d.text((k(190), k(214)), "● Kalshi", font=F(11), fill=ORG + (255,), anchor="lm")
    d.text((k(255), k(214)), "● Fair", font=F(11), fill=CYAN + (255,), anchor="lm")
    PX0, PX1, PY0, PY1 = k(70), k(640), k(236), k(462)
    pv = [a * 100 for _, a, _ in pts]
    fv = [b * 100 for _, _, b in pts]
    entry = (p.get("entry_price") or price) * 100
    vals = pv + fv + [entry]
    lo, hi = min(vals), max(vals)
    if hi - lo < 4:
        m = (hi + lo) / 2
        lo, hi = m - 2, m + 2
    pad = (hi - lo) * 0.15
    lo, hi = lo - pad, hi + pad
    X = lambda t: PX0 + (t - t0) / (t1 - t0) * (PX1 - PX0)
    Y = lambda v: PY1 - (v - lo) / (hi - lo) * (PY1 - PY0)
    xs = [X(t) for t, _, _ in pts]

    d.text(((PX0 + PX1) / 2, (PY0 + PY1) / 2), p["side"], font=F(120), fill=side_col + (14,), anchor="mm")
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        _dots(d, PX0, Y(v), PX1, Y(v), (150, 165, 200, 40), lw(1), k(2), k(5))
        d.text((PX1 + k(8), Y(v)), f"{v:.1f}c", font=F(10, False), fill=MUTED, anchor="lm")
    for i in range(4):
        fr = i / 3
        x = PX0 + (PX1 - PX0) * fr
        if 0 < fr < 1:
            _dots(d, x, PY0, x, k(520), (150, 165, 200, 28), lw(1), k(2), k(6))
        lbl = datetime.fromtimestamp(t0 + (t1 - t0) * fr, timezone.utc).strftime("%H:%M")
        d.text((x, k(546)), lbl + (" UTC" if i == 3 else ""), font=F(10, False), fill=MUTED, anchor="mm")

    if len(pts) > 1:
        d.polygon([(x, Y(v)) for x, v in zip(xs, pv)] + [(xs[-1], PY1), (xs[0], PY1)], fill=ORG + (26,))
        for i in range(len(pts) - 1):
            c = C_UP if (fv[i] + fv[i + 1]) >= (pv[i] + pv[i + 1]) else C_DN
            d.polygon([(xs[i], Y(fv[i])), (xs[i + 1], Y(fv[i + 1])),
                       (xs[i + 1], Y(pv[i + 1])), (xs[i], Y(pv[i]))], fill=c + (60,))

        def line_fn(vals_):
            def fn(gd):
                gd.line([(x, Y(v)) for x, v in zip(xs, vals_)], fill=255, width=lw(3), joint="curve")
            return fn
        _glow(base, line_fn(pv), ORG, k(4), 0.7)
        _glow(base, line_fn(fv), CYAN, k(4), 0.7)
        d = ImageDraw.Draw(base, "RGBA")
        d.line([(x, Y(v)) for x, v in zip(xs, pv)], fill=ORG + (255,), width=lw(2.2), joint="curve")
        d.line([(x, Y(v)) for x, v in zip(xs, fv)], fill=CYAN + (255,), width=lw(2.2), joint="curve")
        hi_i = max(range(len(pv)), key=pv.__getitem__)
        lo_i = min(range(len(pv)), key=pv.__getitem__)
        d.text((xs[hi_i], max(PY0 + k(8), Y(pv[hi_i]) - k(9))), f"H {pv[hi_i]:.1f}", font=F(9),
               fill=C_UP + (230,), anchor="mm")
        d.text((xs[lo_i], min(PY1 - k(6), Y(pv[lo_i]) + k(9))), f"L {pv[lo_i]:.1f}", font=F(9),
               fill=C_DN + (230,), anchor="mm")

    ey = Y(entry)
    _dots(d, PX0, ey, PX1, ey, AMBER + (210,), lw(1.2), k(5), k(4))
    _arrow(d, PX0 + k(10), ey - k(12), p["side"] == "YES", k(6), AMBER + (255,))
    d.text((PX0 + k(22), ey - k(12)), f"ENTRY {entry:.0f}c", font=F(10), fill=AMBER + (255,), anchor="lm")

    lx = xs[-1]
    py_, fy_ = Y(pv[-1]), Y(fv[-1])
    fy2 = fy_
    if abs(py_ - fy_) < k(18):
        fy2 = py_ + k(18) if fy_ >= py_ else py_ - k(18)
    for yy, txt, col in ((py_, f"{pv[-1]:.1f}", ORG), (fy2, f"{fv[-1]:.1f}", CYAN)):
        d.rounded_rectangle([PX1 + k(2), yy - k(9), k(706), yy + k(9)], radius=k(4), fill=col + (255,))
        d.text((PX1 + k(6), yy), txt, font=F(11), fill=(8, 10, 20, 255), anchor="lm")
    for cy_, c in ((py_, ORG), (fy_, CYAN)):
        d.ellipse([lx - k(4), cy_ - k(4), lx + k(4), cy_ + k(4)], fill=c + (255,))

    ES0, ES1 = k(484), k(520)
    em = (ES0 + ES1) / 2
    d.rounded_rectangle([PX0, ES0, PX1, ES1], radius=k(6), fill=(255, 255, 255, 10))
    _dots(d, PX0, em, PX1, em, (150, 165, 200, 50), lw(1), k(2), k(4))
    d.text((PX0 + k(6), ES0 + k(7)), "EDGE (fair - price)", font=F(8, False), fill=MUTED, anchor="lm")
    edges = [b - a for a, b in zip(pv, fv)]
    emax = max(max(abs(e) for e in edges), 0.5)
    bwid = max(k(1.2), min(k(8), (PX1 - PX0) / max(len(pts), 1) * 0.7))
    for x, e in zip(xs, edges):
        h_ = abs(e) / emax * (ES1 - ES0) / 2
        c = C_UP if e >= 0 else C_DN
        d.rectangle([x - bwid / 2, em - h_ if e >= 0 else em, x + bwid / 2, em if e >= 0 else em + h_],
                    fill=c + (200,))

    if outcome:
        d.text(((PX0 + PX1) / 2, (PY0 + PY1) / 2), outcome.upper(), font=F(96), fill=accent + (80,), anchor="mm")

    mx0, mx1 = k(24), k(696)
    zx = mx0 + (mx1 - mx0) * 0.4
    roi = p["roi"]
    mxp = mx0 + (mx1 - mx0) * ((max(-0.10, min(0.15, roi)) + 0.10) / 0.25)
    d.text((mx0, k(584)), "EV METER", font=F(11), fill=MUTED, anchor="lm")
    d.text((mx1, k(584)), f"{roi * 100:+.1f}% ROI after fees", font=F(11), fill=ev_c + (255,), anchor="rm")
    d.rectangle([mx0, k(596), zx, k(614)], fill=C_DN + (60,))
    d.rectangle([zx, k(596), mx1, k(614)], fill=C_UP + (60,))
    d.rectangle([min(zx, mxp), k(599), max(zx, mxp), k(611)], fill=ev_c + (230,))
    d.rounded_rectangle([mxp - k(2), k(592), mxp + k(2), k(618)], radius=k(2), fill=WHITE)
    d.text((mx0, k(630)), "-10%", font=F(9, False), fill=MUTED, anchor="lm")
    d.text((zx, k(630)), "0", font=F(9, False), fill=MUTED, anchor="mm")
    d.text((mx1, k(630)), "+15%", font=F(9, False), fill=MUTED, anchor="rm")

    d.text((mx0, k(652)), "PAYOFF PER CONTRACT", font=F(11), fill=MUTED, anchor="lm")
    rw = (mx1 - mx0) * price
    d.rectangle([mx0, k(662), mx0 + rw, k(688)], fill=C_DN + (200,))
    d.rectangle([mx0 + rw, k(662), mx1, k(688)], fill=C_UP + (200,))
    d.text((mx0 + k(10), k(675)), f"RISK {price * 100:.0f}c", font=F(12), fill=WHITE, anchor="lm")
    d.text((mx1 - k(10), k(675)), f"WIN {(1 - price) * 100:.0f}c", font=F(12), fill=WHITE, anchor="rm")

    blue = (60, 90, 140)
    badges = [("EV", f"{p['ev']:+.3f}", C_UP if p["ev"] > 0 else C_DN),
              ("ROI", f"{p['roi'] * 100:+.1f}%", C_UP if p["roi"] > 0 else C_DN),
              ("FAIR", f"{fair * 100:.0f}%", blue), ("PRICE", f"{price * 100:.0f}c", blue),
              ("STAKE", f"{p['kelly'] * 100:.1f}%", blue)]
    gap, nb = 5, len(badges)
    bwn = (720 - 48 - (nb - 1) * gap) / nb
    for i, (nm, lab, bc) in enumerate(badges):
        x0 = k(24 + i * (bwn + gap))
        d.rounded_rectangle([x0, k(704), x0 + k(bwn), k(758)], radius=k(11), fill=bc + (255,),
                            outline=(255, 255, 255, 70), width=lw(1.5))
        d.text((x0 + k(bwn / 2), k(722)), nm, font=F(10, False), fill=(255, 255, 255, 215), anchor="mm")
        d.text((x0 + k(bwn / 2), k(742)), lab, font=F(13), fill=WHITE, anchor="mm")

    d.rounded_rectangle([k(24), k(774), k(696), k(852)], radius=k(28), fill=side_col + (255,),
                        outline=(accent + (255,)) if accent else None, width=lw(3))
    _arrow(d, k(64), k(813), p["side"] == "YES", k(15), (255, 255, 255, 255))
    d.text((k(380), k(800)), f"BUY {p['side']} at {price * 100:.0f}c · {1 / price:.2f}x payout",
           font=F(21), fill=WHITE, anchor="mm")
    d.text((k(380), k(828)), f"{src} · {p['ticker'][-14:]}", font=F(12, False),
           fill=(255, 255, 255, 215), anchor="mm")

    frames = []
    for f in range(n):
        t = f / n if n > 1 else 0.25
        sn = math.sin(2 * math.pi * t)
        im = base.copy()
        fd = ImageDraw.Draw(im, "RGBA")
        r = k(8 + 1.8 * sn)
        for cy_, c in ((py_, ORG), (fy_, CYAN)):
            fd.ellipse([lx - r, cy_ - r, lx + r, cy_ + r], outline=c + (255,), width=lw(2))
        fd.rectangle([mxp - k(3), k(591), mxp + k(3), k(619)], outline=(255, 255, 255, int(120 + 100 * sn)),
                     width=lw(1))
        if accent:
            fd.rounded_rectangle([k(3), k(3), W - k(4), H - k(4)], radius=k(24),
                                 outline=accent + (int(110 + 90 * sn),), width=lw(4))
        frames.append(im.convert("RGB"))
    return frames


def chart_file(p, outcome=None):
    animated = outcome is None
    S = LIVE_SCALE if animated else FINAL_SCALE
    while True:
        frames = render_frames(p, outcome, S, 8 if animated else 1)
        buf = io.BytesIO()
        if animated:
            frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
            res = (buf.getvalue(), "chart.gif", "image/gif")
        else:
            frames[0].save(buf, "PNG", optimize=True)
            res = (buf.getvalue(), "chart.png", "image/png")
        if len(res[0]) <= MAX_BYTES or S <= 1:
            return res
        S *= 0.75


# ---------- discord ----------
def stats(h):
    """Overall record + win %, last 20, calibration (predicted vs actual win %), P&L, avg EV."""
    n = len(h)
    if not n:
        return "n/a", "n/a", "n/a", "n/a", "n/a"
    w = sum(1 for x in h if x["win"])
    last = h[-20:]
    lw_ = sum(1 for x in last if x["win"])
    pnl = sum(x["pnl"] for x in h)
    stake = sum(x["price"] for x in h)
    avg_ev = sum(x["ev"] for x in h) / n
    pred = sum(x["p"] for x in h) / n
    return (f"{w}W-{n - w}L ({w / n * 100:.0f}%)",
            f"{pnl:+.2f}u ({pnl / stake * 100:+.1f}% ROI)",
            f"{avg_ev:+.3f}u/bet predicted",
            f"{lw_}W-{len(last) - lw_}L ({lw_ / len(last) * 100:.0f}%)",
            f"predicted {pred * 100:.0f}% vs actual {w / n * 100:.0f}%")


def build_embed(p, state, outcome=None):
    rec, pnl, avg, last, calib = stats(state["history"])
    who = p["team"] if p["side"] == "YES" else f"against {p['team']}"
    lines = [f"**Game:** {p['game']}",
             f"**Pick:** Buy **{p['side']}** at {p['price'] * 100:.0f}c ({who})",
             f"**Fair win prob:** {p['p'] * 100:.1f}% vs price {p['price'] * 100:.0f}c "
             f"(source: {p.get('src', 'Kalshi')})",
             f"**Edge:** {p['edge'] * 100:+.1f} pts | **Growth score:** {p.get('growth', 0) * 1000:+.2f}",
             f"**EV:** {p['ev']:+.3f} per $1 contract after fees ({p['roi'] * 100:+.1f}% ROI)",
             f"**Suggested stake:** {p['kelly'] * 100:.1f}% of bankroll (capped fractional Kelly)",
             f"**Closes about:** <t:{int(p['commence'])}:R>", f"**Market:** `{p['ticker']}`"]
    if outcome in ("win", "loss"):
        title = f"{'✅ WIN' if outcome == 'win' else '❌ LOSS'} | {p['game']}"
        color = GREEN if outcome == "win" else RED
        lines.insert(0, f"**Result:** {p['pnl']:+.2f}u")
    else:
        title, color = f"{'🆙' if p['side'] == 'YES' else '⬇️'} PICK: {p['team']} {p['side']}", GREEN
    arb_txt = f"{state['arb_count']} found | {state['arb_locked']:+.2f}u locked" if state["arb_count"] else "none yet"
    return {"title": title[:250], "description": "\n".join(lines), "color": color,
            "fields": [{"name": "Record (win %)", "value": rec, "inline": True},
                       {"name": "Last 20", "value": last, "inline": True},
                       {"name": "P&L", "value": pnl, "inline": True},
                       {"name": "Calibration", "value": calib, "inline": True},
                       {"name": "Avg predicted EV", "value": avg, "inline": True},
                       {"name": "Arbs", "value": arb_txt, "inline": True}],
            "footer": {"text": "Signals only, not financial advice"},
            "timestamp": datetime.now(timezone.utc).isoformat()}


def send(embed, message_id=None, pick=None, outcome=None):
    payload, f_data = {"embeds": [embed]}, None
    if pick is not None:
        f_data = chart_file(pick, outcome)
        embed["image"] = {"url": f"attachment://{f_data[1]}"}
        if message_id:
            payload["attachments"] = []
    for attempt in range(5):
        kw = {"timeout": (5, 30)}
        if f_data:
            kw["files"] = {"files[0]": (f_data[1], f_data[0], f_data[2])}
            kw["data"] = {"payload_json": json.dumps(payload)}
        else:
            kw["json"] = payload
        try:
            if message_id:
                r = HOOK.patch(f"{WEBHOOK}/messages/{message_id}", **kw)
            else:
                r = HOOK.post(WEBHOOK, params={"wait": "true"}, **kw)
        except requests.RequestException:
            if message_id and attempt < 4:
                time.sleep(0.5 * 2 ** attempt)
                continue
            raise
        if r.status_code == 429:
            time.sleep(min(float(r.json().get("retry_after", 1)), 10) + 0.1)
            continue
        r.raise_for_status()
        return r.json()["id"]
    raise RuntimeError("discord rate limited")


def post(p, state, outcome=None):
    p["message_id"] = send(build_embed(p, state, outcome), p.get("message_id"), pick=p, outcome=outcome)


def post_arb(a, state):
    legs = "\n".join(f"• **{side}** {m.get('yes_sub_title') or m['ticker']} at {price * 100:.0f}c "
                     f"(`{m['ticker']}`)" for m, side, price in a["legs"])
    embed = {"title": f"💰 ARB: {a['title']}"[:250],
             "description": f"**{a['kind']}**\n{legs}\n"
                            f"**Locked profit:** about {a['profit'] * 100:.1f}c per set after fees\n"
                            "Top-of-book prices only. Check the size available before sizing up.",
             "color": GREEN, "footer": {"text": "Signals only, not financial advice"},
             "timestamp": datetime.now(timezone.utc).isoformat()}
    send(embed)


# ---------- core ----------
def settle(state):
    for t, p in list(state["open"].items()):
        if time.time() < p["commence"]:
            continue
        try:
            res = kalshi_market(t).get("result")
        except Exception as e:
            print("settle error", repr(e), flush=True)
            continue
        if res not in ("yes", "no"):
            continue
        entry = p.get("entry_price", p["price"])
        p["win"] = (res == "yes") == (p["side"] == "YES")
        p["pnl"] = round((1.0 if p["win"] else 0.0) - entry - fee(entry), 4)
        state["history"].append({"ticker": t, "event": p.get("event"), "side": p["side"], "win": p["win"],
                                 "pnl": p["pnl"], "price": entry, "ev": p.get("ev0", p["ev"]),
                                 "p": p.get("p0", p["p"]), "src": p.get("src")})
        del state["open"][t]
        try:
            post(p, state, "win" if p["win"] else "loss")
        except Exception as e:
            print("settle send error", repr(e), flush=True)
        save_state(state)


def refresh_open(state, marks, now):
    """Update open picks on their OWN side only, so a pick can never flip."""
    for t, old in state["open"].items():
        if now >= old["commence"] or t not in marks:
            continue
        fair, margin, a, b = marks[t]
        yes = old["side"] == "YES"
        price = a if yes else 1 - b
        if not (0 < price < 1):
            continue
        p_ = (fair if yes else 1 - fair) - margin
        c = price + fee(price)
        old.update({"price": price, "p": p_, "ev": p_ - c, "roi": (p_ - c) / price, "edge": p_ - price})
        old["pts"] = (old.get("pts", []) + [[round(now), round(price, 4), round(p_, 4)]])[-600:]
        if now - old.get("last_edit", 0) >= EDIT_SECONDS:
            try:
                post(old, state)
                old["last_edit"] = now
            except Exception as e:
                print("edit error", repr(e), flush=True)


def tick(state):
    settle(state)
    picks, marks, arbs = scan()
    now = time.time()
    refresh_open(state, marks, now)

    sent = 0
    for a in sorted(arbs, key=lambda x: -x["profit"]):
        if sent >= 3 or now - state["arbs"].get(a["event"], 0) < ARB_REPEAT:
            continue
        try:
            post_arb(a, state)
            state["arbs"][a["event"]] = now
            state["arb_count"] += 1
            state["arb_locked"] += a["profit"]
            sent += 1
        except Exception as e:
            print("arb send error", repr(e), flush=True)
    state["arbs"] = {k_: v for k_, v in state["arbs"].items() if now - v < 86400}

    # new picks: one pick per game, never the opposite side or the other team
    done = {h["ticker"] for h in state["history"]}
    locked = {h.get("event") for h in state["history"] if h.get("event")}
    locked |= {p.get("event") for p in state["open"].values() if p.get("event")}
    fresh = 0
    for p in picks:
        t = p["ticker"]
        if t in state["open"] or t in done or p["event"] in locked:
            continue
        if fresh >= TOP_N or len(state["open"]) >= MAX_OPEN:
            continue
        p["pts"] = [[round(now), round(p["price"], 4), round(p["p"], 4)]]
        p["entry_price"], p["last_edit"] = p["price"], now
        p["p0"], p["ev0"] = p["p"], p["ev"]
        post(p, state)
        state["open"][t] = p
        locked.add(p["event"])
        fresh += 1
    state["budget"] = dict(BUDGET)
    save_state(state)


def main():
    state = load_state()
    BUDGET.update(state.get("budget", {}))
    if not ODDS_KEY:
        print("No MONEYLINE_API_KEY set: Kalshi-only mode (arb alerts work, few edge picks). "
              "Add a key for sportsbook-matched picks.", flush=True)
    print("sports bot started", flush=True)
    end, fails = time.time() + RUN_SECONDS, 0
    while time.time() < end:
        t0 = time.time()
        try:
            tick(state)
            fails = 0
        except Exception as e:
            fails += 1
            print("tick error", repr(e), flush=True)
        delay = POLL if not fails else min(300, POLL * 2 ** min(fails, 3))
        time.sleep(max(0.0, delay - (time.time() - t0)))


if __name__ == "__main__":
    main()
