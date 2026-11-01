import functools
import io
import json
import math
import os
import time
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from PIL import Image, ImageDraw, ImageFont, ImageOps

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"

# Kalshi game-winner series. VERIFY these tickers on Kalshi. Override with SERIES env (comma list).
SERIES = [s.strip() for s in os.getenv(
    "SERIES", "KXNBAGAME,KXNFLGAME,KXMLBGAME,KXNHLGAME,KXNCAAFGAME,KXNCAAMBGAME,KXEPLGAME,KXMLSGAME"
).split(",") if s.strip()]

POLL = float(os.getenv("POLL_SECONDS", "30"))
MIN_ROI = float(os.getenv("MIN_ROI", "0.02"))          # EV / price needed to post
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.06"))    # skip thin markets (bid/ask gap in dollars)
PMIN, PMAX = float(os.getenv("PMIN", "0.08")), float(os.getenv("PMAX", "0.92"))
HOURS_AHEAD = float(os.getenv("HOURS_AHEAD", "36"))    # markets closing within this many hours
TOP_N = int(os.getenv("TOP_N", "3"))
KELLY_FRAC = float(os.getenv("KELLY_FRAC", "0.25"))
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
HOOK = _session(0)  # no blind POST retries, so a post can never double


def get_json(url, params=None, timeout=(3.05, 10)):
    r = DATA.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# ---------- state ----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        s.setdefault("history", [])
        s.setdefault("open", {})
        return s
    except Exception:
        return {"history": [], "open": {}}


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
    """Kalshi taker fee for one contract, rounded up to the cent (conservative for 1 contract)."""
    return math.ceil(0.07 * p * (1 - p) * 100) / 100


# ---------- EV from sibling markets ----------
def evaluate(m, p_yes):
    ya, yb = dollars(m, "yes_ask"), dollars(m, "yes_bid")
    best = None
    for side, p, price in (("YES", p_yes, ya), ("NO", 1 - p_yes, None if yb is None else 1 - yb)):
        if price is None or not (PMIN <= price <= PMAX):
            continue
        ev = p - price - fee(price)
        roi = ev / price
        if best is None or roi > best["roi"]:
            best = {"side": side, "p": p, "price": price, "ev": ev, "roi": roi,
                    "kelly": max(0.0, (p - price) / (1 - price)) * KELLY_FRAC}
    return best


def scan():
    """Group markets by game. Fair price = each outcome's bid/ask midpoint, rescaled so all
    outcomes of the game add to 100%. A pick is a side whose price lags that fair value."""
    picks, now = [], time.time()
    for series in SERIES:
        try:
            mkts = kalshi_markets(series)
        except Exception as e:
            print("scan error", series, repr(e), flush=True)
            continue
        events = {}
        for m in mkts:
            events.setdefault(m.get("event_ticker") or m["ticker"], []).append(m)
        for ms in events.values():
            if len(ms) < 2:
                continue
            q = []
            for m in ms:
                b, a = dollars(m, "yes_bid"), dollars(m, "yes_ask")
                if b is None or a is None or a < b or a - b > MAX_SPREAD:
                    q = None
                    break
                q.append((m, (a + b) / 2))
            if not q:
                continue
            tot = sum(mid for _, mid in q)
            for m, mid in q:
                try:
                    end = ts(m.get("expected_expiration_time") or m["close_time"])
                except Exception:
                    continue
                if not (0 < end - now < HOURS_AHEAD * 3600):
                    continue
                r = evaluate(m, mid / tot)
                if r and r["roi"] >= MIN_ROI:
                    title = (m.get("title") or "").replace(" Winner?", "")
                    picks.append({**r, "ticker": m["ticker"], "series": series,
                                  "team": m.get("yes_sub_title") or m["ticker"], "title": title,
                                  "game": title or m["ticker"], "commence": end,
                                  "p_yes": mid / tot, "books": len(ms)})
    return sorted(picks, key=lambda x: -x["roi"])


# ---------- chart ----------
CW, CH = 720, 640
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


def _hhmm(s):
    s = max(0, int(s))
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def render_frames(p, outcome, S, n):
    k = lambda v: v * S
    F = lambda sz, b=True: _font(max(6, int(k(sz))), b)
    lw = lambda v: max(1, int(k(v)))
    W, H = int(CW * S), int(CH * S)
    g = Image.linear_gradient("L").resize((W, H))
    base = ImageOps.colorize(g, (13, 31, 31), (10, 10, 10)).convert("RGBA")
    d = ImageDraw.Draw(base, "RGBA")
    accent = {"win": C_UP, "loss": C_DN}.get(outcome)
    pts = p.get("pts") or [[time.time(), p["price"], p["p"]]]
    t0 = pts[0][0]
    t1 = max(pts[-1][0], t0 + 120)
    edge = (p["p"] - p["price"]) * 100

    # header
    d.text((k(24), k(46)), f"{p['team']} · {p['side']}", font=F(26), fill=WHITE, anchor="lm")
    d.text((k(24), k(80)), p["game"][:60], font=F(14, False), fill=MUTED, anchor="lm")
    if outcome:
        btxt, bcol = outcome.upper(), accent
    else:
        btxt, bcol = "Closes " + _hhmm(p["commence"] - time.time()), (255, 255, 255)
    bw = d.textlength(btxt, font=F(20)) / S + 36
    d.rounded_rectangle([k(696 - bw), k(28), k(696), k(76)], radius=k(16), fill=(255, 255, 255, 26),
                        outline=bcol + (190,), width=lw(2))
    d.text((k(696 - bw / 2), k(52)), btxt, font=F(20), fill=bcol + (255,), anchor="mm")

    # price row
    d.text((k(24), k(124)), f"Kalshi price ({p['side']})", font=F(15, False), fill=MUTED, anchor="ls")
    d.text((k(24), k(166)), f"{p['price'] * 100:.0f}c", font=F(32), fill=ORG + (255,), anchor="ls")
    d.text((k(260), k(124)), "Implied fair", font=F(15, False), fill=MUTED, anchor="ls")
    d.text((k(260), k(166)), f"{p['p'] * 100:.1f}%", font=F(32), fill=CYAN + (255,), anchor="ls")
    ec = C_UP if edge >= 0 else C_DN
    d.text((k(500), k(124)), "Edge", font=F(15, False), fill=MUTED, anchor="ls")
    d.text((k(500), k(166)), f"{edge:+.1f} pts", font=F(32), fill=ec + (255,), anchor="ls")

    # chart panel
    d.rounded_rectangle([k(12), k(190), k(708), k(470)], radius=k(18), fill=(255, 255, 255, 14),
                        outline=(255, 255, 255, 28), width=lw(1))
    PX0, PX1, PY0, PY1 = k(70), k(640), k(222), k(436)
    vals = [v * 100 for _, a, b in pts for v in (a, b)]
    vals.append((p.get("entry_price") or p["price"]) * 100)
    lo, hi = min(vals), max(vals)
    if hi - lo < 4:
        m = (hi + lo) / 2
        lo, hi = m - 2, m + 2
    pad = (hi - lo) * 0.15
    lo, hi = lo - pad, hi + pad
    X = lambda t: PX0 + (t - t0) / (t1 - t0) * (PX1 - PX0)
    Y = lambda v: PY1 - (v - lo) / (hi - lo) * (PY1 - PY0)
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        _dots(d, PX0, Y(v), PX1, Y(v), (150, 165, 200, 40), lw(1), k(2), k(5))
        d.text((PX1 + k(8), Y(v)), f"{v:.1f}", font=F(11, False), fill=MUTED, anchor="lm")
    d.text((PX0, k(452)), "first seen", font=F(10, False), fill=MUTED, anchor="lm")
    d.text((PX1, k(452)), "now", font=F(10, False), fill=MUTED, anchor="rm")

    # shaded edge between fair and price: green when fair > price, red otherwise
    for (ta, pa, fa), (tb, pb, fb) in zip(pts, pts[1:]):
        c = C_UP if (fa + fb) >= (pa + pb) else C_DN
        d.polygon([(X(ta), Y(fa * 100)), (X(tb), Y(fb * 100)), (X(tb), Y(pb * 100)), (X(ta), Y(pa * 100))],
                  fill=c + (55,))
    if len(pts) > 1:
        d.line([(X(t), Y(pr * 100)) for t, pr, _ in pts], fill=ORG + (255,), width=lw(2.5), joint="curve")
        d.line([(X(t), Y(f * 100)) for t, _, f in pts], fill=CYAN + (255,), width=lw(2.5), joint="curve")
    if p.get("entry_price"):
        ey = Y(p["entry_price"] * 100)
        _dots(d, PX0, ey, PX1, ey, AMBER + (200,), lw(1.2), k(5), k(4))
        d.text((PX0 + k(6), ey - k(9)), f"ENTRY {p['entry_price'] * 100:.0f}c", font=F(10),
               fill=AMBER + (255,), anchor="lm")
    d.text((PX0 + k(6), PY0 - k(10)), "Kalshi price", font=F(11), fill=ORG + (255,), anchor="lm")
    d.text((PX0 + k(90), PY0 - k(10)), "Implied fair", font=F(11), fill=CYAN + (255,), anchor="lm")
    lx, lpy, lfy = X(pts[-1][0]), Y(pts[-1][1] * 100), Y(pts[-1][2] * 100)
    for cy, c in ((lpy, ORG), (lfy, CYAN)):
        d.ellipse([lx - k(4), cy - k(4), lx + k(4), cy + k(4)], fill=c + (255,))

    # badges
    blue = (60, 90, 140)
    badges = [("EV", f"{p['ev']:+.3f}", C_UP if p["ev"] > 0 else C_DN),
              ("ROI", f"{p['roi'] * 100:+.1f}%", C_UP if p["roi"] > 0 else C_DN),
              ("FAIR", f"{p['p'] * 100:.0f}%", blue), ("PRICE", f"{p['price'] * 100:.0f}c", blue),
              ("KELLY", f"{p['kelly'] * 100:.1f}%", blue)]
    gap, nb = 5, len(badges)
    bwn = (720 - 48 - (nb - 1) * gap) / nb
    for i, (nm, lab, bc) in enumerate(badges):
        x0 = k(24 + i * (bwn + gap))
        d.rounded_rectangle([x0, k(484), x0 + k(bwn), k(538)], radius=k(11), fill=bc + (255,),
                            outline=(255, 255, 255, 70), width=lw(1.5))
        d.text((x0 + k(bwn / 2), k(502)), nm, font=F(10, False), fill=(255, 255, 255, 215), anchor="mm")
        d.text((x0 + k(bwn / 2), k(522)), lab, font=F(13), fill=WHITE, anchor="mm")

    # buy button
    col = C_UP if p["side"] == "YES" else C_DN
    d.rounded_rectangle([k(24), k(552), k(696), k(616)], radius=k(26), fill=col + (255,),
                        outline=(accent + (255,)) if accent else None, width=lw(3))
    d.text((k(360), k(574)), f"BUY {p['side']} at {p['price'] * 100:.0f}c, pays 100c", font=F(20),
           fill=WHITE, anchor="mm")
    d.text((k(360), k(600)), f"{p['books']}-way market · {p['ticker'][-14:]}", font=F(12, False),
           fill=(255, 255, 255, 215), anchor="mm")

    frames = []
    for f in range(n):
        t = f / n if n > 1 else 0.25
        sn = math.sin(2 * math.pi * t)
        im = base.copy()
        fd = ImageDraw.Draw(im, "RGBA")
        r = k(8 + 1.8 * sn)
        for cy, c in ((lpy, ORG), (lfy, CYAN)):
            fd.ellipse([lx - r, cy - r, lx + r, cy + r], outline=c + (255,), width=lw(2))
        if accent:
            fd.rounded_rectangle([k(3), k(3), W - k(4), H - k(4)], radius=k(24),
                                 outline=accent + (int(110 + 90 * sn),), width=lw(4))
        frames.append(im.convert("RGB"))
    return frames


def chart_file(p, outcome=None):
    """(bytes, filename, mime). Live = animated GIF, settled = PNG. Shrinks if too big."""
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
    n = len(h)
    if not n:
        return "n/a", "n/a", "n/a"
    w = sum(1 for x in h if x["win"])
    pnl = sum(x["pnl"] for x in h)
    stake = sum(x["price"] for x in h)
    avg_ev = sum(x["ev"] for x in h) / n
    return (f"{w}W-{n - w}L ({w / n * 100:.0f}%)",
            f"{pnl:+.2f}u ({pnl / stake * 100:+.1f}% ROI)", f"{avg_ev:+.3f}u/bet predicted")


def build_embed(p, state, outcome=None):
    rec, pnl, avg = stats(state["history"])
    who = p["team"] if p["side"] == "YES" else f"against {p['team']}"
    lines = [f"**Game:** {p['game']}",
             f"**Pick:** Buy **{p['side']}** at {p['price'] * 100:.0f}c ({who})",
             f"**Implied fair prob:** {p['p'] * 100:.1f}% vs price {p['price'] * 100:.0f}c "
             f"(from {p['books']} sibling markets)",
             f"**EV:** {p['ev']:+.3f} per $1 contract after fees ({p['roi'] * 100:+.1f}% ROI)",
             f"**Suggested stake:** {p['kelly'] * 100:.1f}% of bankroll (quarter Kelly)",
             f"**Closes about:** <t:{int(p['commence'])}:R>", f"**Market:** `{p['ticker']}`"]
    if outcome in ("win", "loss"):
        title = f"{'✅ WIN' if outcome == 'win' else '❌ LOSS'} | {p['game']}"
        color = GREEN if outcome == "win" else RED
        lines.insert(0, f"**Result:** {p['pnl']:+.2f}u")
    else:
        title, color = f"{'🆙' if p['side'] == 'YES' else '⬇️'} +EV: {p['team']} {p['side']}", GREEN
    return {"title": title[:250], "description": "\n".join(lines), "color": color,
            "fields": [{"name": "Record", "value": rec, "inline": True},
                       {"name": "P&L", "value": pnl, "inline": True},
                       {"name": "Avg predicted EV", "value": avg, "inline": True}],
            "footer": {"text": "Signals only, not financial advice"},
            "timestamp": datetime.now(timezone.utc).isoformat()}


def send(embed, message_id=None, pick=None, outcome=None):
    payload, f_data = {"embeds": [embed]}, None
    if pick is not None:
        f_data = chart_file(pick, outcome)
        embed["image"] = {"url": f"attachment://{f_data[1]}"}
        if message_id:
            payload["attachments"] = []  # swap out the old image
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
        p["win"] = (res == "yes") == (p["side"] == "YES")
        p["pnl"] = round((1.0 if p["win"] else 0.0) - p["price"] - fee(p["price"]), 4)
        state["history"].append({k: p[k] for k in ("ticker", "side", "win", "pnl", "price", "ev", "p")})
        del state["open"][t]
        try:
            post(p, state, "win" if p["win"] else "loss")
        except Exception as e:
            print("settle send error", repr(e), flush=True)
        save_state(state)


def tick(state):
    settle(state)
    picks = scan()
    done = {h["ticker"] for h in state["history"]}
    fresh, now = 0, time.time()
    for p in picks:
        t = p["ticker"]
        if t in state["open"]:
            old = state["open"][t]
            if now < old["commence"]:
                old.update({k: p[k] for k in ("p", "price", "ev", "roi", "kelly", "books")})
                old["pts"] = (old.get("pts", []) + [[round(now), round(p["price"], 4), round(p["p"], 4)]])[-600:]
                if now - old.get("last_edit", 0) >= EDIT_SECONDS:
                    try:
                        post(old, state)
                        old["last_edit"] = now
                    except Exception as e:
                        print("edit error", repr(e), flush=True)
        elif t not in done and fresh < TOP_N and len(state["open"]) < 25:
            p["pts"] = [[round(now), round(p["price"], 4), round(p["p"], 4)]]
            p["entry_price"], p["last_edit"] = p["price"], now
            post(p, state)
            state["open"][t] = p
            fresh += 1
    save_state(state)


def main():
    state = load_state()
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
