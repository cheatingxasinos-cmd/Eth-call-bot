import io, json, math, os, random, statistics, time
from datetime import datetime, timezone
import requests
from PIL import Image, ImageDraw, ImageFont

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
SERIES = os.getenv("KALSHI_SERIES", "KXETH15M")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
POLL = float(os.getenv("POLL_SECONDS", "10"))
RUN_SECONDS = int(os.getenv("RUN_SECONDS", str(345 * 60)))
MAX_LATE = int(os.getenv("MAX_LATE_ENTRY", "90"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")
GREEN, RED, GREY = 0x00FF00, 0xFF3B30, 0x808080


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
W, H = 800, 360
PL, PR, PT, PB = 50, 30, 56, 40
BG = (18, 20, 28)


def _font(size):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _xy(t, v):
    return PL + (W - PL - PR) * min(t / 900, 1), PT + (H - PT - PB) * (1 - v / 100)


def _rocket(d, x, y, ang, sc, flame, body_col):
    c, s = math.cos(ang), math.sin(ang)

    def r(px, py):
        return (x + (px * c - py * s) * sc, y + (px * s + py * c) * sc)

    if flame > 0:
        d.polygon([r(-10, -5), r(-10 - flame, 0), r(-10, 5)], fill=(255, 140, 0))
        d.polygon([r(-10, -3), r(-10 - flame * 0.6, 0), r(-10, 3)], fill=(255, 235, 120))
    d.polygon([r(-10, -6), r(-17, -13), r(-3, -6)], fill=body_col)
    d.polygon([r(-10, 6), r(-17, 13), r(-3, 6)], fill=body_col)
    d.polygon([r(18, 0), r(8, -6), r(-10, -6), r(-10, 6), r(8, 6)], fill=(235, 238, 245))
    d.polygon([r(18, 0), r(8, -6), r(8, 6)], fill=body_col)
    cx, cy = r(1, 0)
    d.ellipse([cx - 3 * sc, cy - 3 * sc, cx + 3 * sc, cy + 3 * sc], fill=(60, 140, 255))


def _ship(d, x, y, ang, sc, f, full, outcome):
    c, s = math.cos(ang), math.sin(ang)

    def r(px, py):
        return (x + (px * c - py * s) * sc, y + (px * s + py * c) * sc)

    def box(x0, y0, x1, y1, fill):
        d.polygon([r(x0, y0), r(x1, y0), r(x1, y1), r(x0, y1)], fill=fill)

    d.line([r(-27, -2), r(-27, -17)], fill=(200, 200, 205), width=2)
    d.line([r(29, -2), r(29, -15)], fill=(200, 200, 205), width=2)
    d.polygon([r(-32, -2), r(36, -2), r(28, 8), r(-28, 8)], fill=(25, 25, 32))
    d.polygon([r(-29.5, 5), r(31, 5), r(28, 8), r(-28, 8)], fill=(185, 35, 35))
    box(-22, -10, 24, -2, (238, 240, 246))
    for px in range(-19, 23, 6):
        a = r(px, -6)
        d.ellipse([a[0] - 1, a[1] - 1, a[0] + 1, a[1] + 1], fill=(60, 90, 140))
    for px in (-14, -5, 4, 13):
        box(px - 2.5, -22, px + 2.5, -10, (214, 160, 80))
        box(px - 2.5, -22, px + 2.5, -19, (20, 20, 24))
    if outcome != "win":
        for px in (-14, -5, 4, 13):
            sx, sy = r(px, -24)
            off = ((f * 1.5 + px) % 16) / 16
            rad = 2 + off * 4
            g = int(150 - off * 70)
            d.ellipse([sx - off * 14 - rad, sy - off * 10 - rad, sx - off * 14 + rad, sy - off * 10 + rad], fill=(g, g, g + 8))
    wy = y + 5 * sc
    wpts = [(x - 50 * sc + i * 5 * sc, wy + math.sin(f / 16 * 6.28 * 2 + i * 0.9) * 3) for i in range(21)]
    d.line(wpts, fill=(70, 135, 205), width=3)
    if outcome != "loss":
        for k in range(8):
            age = ((f + k * 2) % 16) / 16
            bx = x + (k - 4) * 4 * sc + math.sin(k + f * 0.4) * 3
            by = y + 10 * sc - age * 45
            rad = 1 + int(3 * (1 - age))
            d.ellipse([bx - rad, by - rad, bx + rad, by + rad], outline=(150, 200, 240))


def render_chart(cur, outcome=None):
    pts = cur.get("pts") or [[0, 50]]
    if len(pts) < 2:
        pts = [pts[0], [pts[0][0] + 2, pts[0][1]]]
    co = [_xy(t, v) for t, v in pts]
    is_up = cur["side"] == "UP"
    yes_last = pts[-1][1]
    last = yes_last if is_up else 100 - yes_last
    full = outcome == "win" or (outcome not in ("loss", "settling") and last >= 80)
    col = (0, 255, 90) if is_up else (255, 59, 48)
    dim = tuple(int(col[i] * 0.16 + BG[i] * 0.84) for i in range(3))
    hx, hy = co[-1]
    j = max(0, len(co) - 6)
    ang = max(-0.7, min(0.7, math.atan2(hy - co[j][1], max(hx - co[j][0], 1))))
    if outcome == "loss" and is_up:
        ang = 1.2
    if not is_up:
        if outcome == "win":
            ang = 1.1
        elif outcome == "loss":
            ang = 0
        elif full:
            ang = max(ang, 0.5)
    sc = 1.6 if full else 1.2
    f_big, f_sm = _font(22), _font(12)
    base_y = _xy(0, 0)[1]
    rng = random.Random(7)
    stars = [(rng.randint(0, W), rng.randint(0, H), rng.random() * 6.28) for _ in range(45)]
    N, frames = 16, []
    for f in range(N):
        im = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(im)
        for sx, sy, ph in stars:
            b = int(70 + 60 * math.sin(f / N * 12.56 + ph))
            d.point((sx, sy), fill=(b, b, b + 20))
        for v in (0, 25, 50, 75, 100):
            y = _xy(0, v)[1]
            d.line([(PL, y), (W - PR, y)], fill=(95, 99, 115) if v == 50 else (40, 44, 58))
            d.text((10, y - 7), f"{v}%", fill=(130, 134, 150), font=f_sm)
        for m in (0, 5, 10, 15):
            d.text((_xy(m * 60, 0)[0] - 8, H - PB + 10), f"{m}m", fill=(130, 134, 150), font=f_sm)
        d.polygon(co + [(hx, base_y), (co[0][0], base_y)], fill=dim)
        d.line(co, fill=col, width=3, joint="curve")
        if is_up and outcome != "loss":
            for k in range(10):
                age = ((f + k * 1.6) % N) / N
                dist = (16 * sc) + age * 75
                sx = hx - math.cos(ang) * dist
                sy = hy - math.sin(ang) * dist + math.sin(k * 2.1 + f * 0.5) * age * 12
                rad = max(1, int((6 if full else 4) * (1 - age)))
                fade = 1 - age
                d.ellipse([sx - rad, sy - rad, sx + rad, sy + rad],
                          fill=(int(255 * fade), int(150 * fade), int(20 * fade)))
        pulse = 5 + 2 * math.sin(f / N * 6.28 * 2)
        d.ellipse([hx - pulse, hy - pulse, hx + pulse, hy + pulse], outline=col, width=2)
        flick = 0.75 + 0.25 * math.sin(f * 1.7) + 0.15 * rng.random()
        bob = math.sin(f / N * 6.28) * 3
        if is_up:
            flame = 0 if outcome == "loss" else (28 if full else 14) * flick
            _rocket(d, hx + math.cos(ang) * 10 * sc, hy + math.sin(ang) * 10 * sc + bob, ang, sc, flame, col)
        else:
            sink = 14 if outcome == "win" else (6 if full else 0)
            _ship(d, hx, hy + bob + sink, ang, sc, f, full, outcome)
        d.text((PL, 14), f"ETH 15m  {cur['side']}  {last:.0f}%", fill=(240, 242, 250), font=f_big)
        if outcome == "win":
            label, lcol = "WIN", (0, 255, 90)
        elif outcome == "loss":
            label, lcol = "LOSS", (255, 59, 48)
        elif full:
            word = "FULL SEND" if is_up else "FULL SINK"
            label, lcol = (word if f % 4 < 2 else word + " >>>"), col
        else:
            label, lcol = "HOLDING", col
        d.text((W - PR - d.textlength(label, font=f_big), 14), label, fill=lcol, font=f_big)
        frames.append(im)
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=70, loop=0, disposal=2)
    return buf.getvalue()


# ---------- discord ----------
def build_embed(c, state, yes, price, n_ex, secs_left, outcome=None):
    side = c["side"]
    conf = yes if side == "UP" else 100 - yes
    if outcome == "win":
        tag, color = "✅ WIN", GREEN
    elif outcome == "loss":
        tag, color = "❌ LOSS", RED
    elif outcome == "settling":
        tag, color = "⏳ SETTLING", GREY
    else:
        tag, color = "HOLD", GREEN if side == "UP" else RED
    lines = [
        f"**Call:** {side} ({conf:.0f}% confidence)",
        f"**Action:** {'HOLD (if holding ' + ('YES' if side == 'UP' else 'NO') + ')' if not outcome else tag}",
        f"**Kalshi** YES {yes:.0f}% / NO {100 - yes:.0f}%",
    ]
    if price:
        lines.append(f"**Price** ${price:,.2f} (CF constituents median ({n_ex} exch))")
    lines.append(f"**Strike** {c['strike']:,.2f} | {max(int(secs_left), 0)}s left")
    s = stats(state["history"])
    fields = [
        {"name": "All-time", "value": s["all"], "inline": True},
        {"name": "Last 20", "value": s["last20"], "inline": True},
        {"name": "Streak", "value": s["streak"], "inline": True},
        {"name": "UP calls", "value": s["up"], "inline": True},
        {"name": "DOWN calls", "value": s["down"], "inline": True},
    ]
    return {
        "title": f"KALSHI ETH 15m: {side} | {tag}",
        "description": "\n".join(lines),
        "color": color,
        "fields": fields,
        "footer": {"text": "Signals only, not financial advice"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def send(embed, message_id=None, cur=None, outcome=None):
    payload = {"embeds": [embed]}
    gif = None
    if cur is not None:
        embed["image"] = {"url": "attachment://chart.gif"}
        if message_id:
            payload["attachments"] = []
        gif = render_chart(cur, outcome)
    for _ in range(3):
        kw = {"timeout": 20}
        if gif:
            kw["files"] = {"files[0]": ("chart.gif", gif, "image/gif")}
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
        state["pending"].append(cur)
        state["current"] = cur = None
        p = state["pending"][-1]
        send(build_embed(p, state, 50, None, 0, 0, "settling"), p["message_id"], cur=p, outcome="settling")
        save_state(state)
    if m is None:
        return

    close_ts = ts(m["close_time"])
    secs_left = close_ts - now
    yes = yes_pct(m)
    price, n_ex = eth_price()

    if cur is None:
        if close_ts - 900 + MAX_LATE < now:
            return
        strike = float(m.get("floor_strike") or m.get("cap_strike") or 0)
        if abs(yes - 50) < 0.01 and price:
            side = "UP" if price >= strike else "DOWN"
        else:
            side = "UP" if yes > 50 else "DOWN"
        cur = {"ticker": m["ticker"], "side": side, "strike": strike,
               "close_ts": close_ts, "message_id": None, "pts": []}
        state["current"] = cur

    cur["pts"].append([round(900 - secs_left), round(yes, 1)])
    new_id = send(build_embed(cur, state, yes, price, n_ex, secs_left), cur["message_id"], cur=cur)
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

If you keep eth.py as its own file, the workflow gets much shorter. Use this for .github/workflows/eth.yml, and you can ignore the long version with the embedded code:
name: ETH 15m Predictions

on:
  schedule:
    - cron: "0 */6 * * *"
  workflow_dispatch:

permissions:
  contents: write

concurrency:
  group: eth-predictions
  cancel-in-progress: false

jobs:
  run-bot:
    runs-on: ubuntu-latest
    timeout-minutes: 355
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - run: pip install requests pillow

      - name: Run bot
        env:
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
          RUN_SECONDS: "20700"
          POLL_SECONDS: "10"
        run: python eth.py

      - name: Save stats
        if: always()
        run: |
          git config user.name "prediction-bot"
          git config user.email "bot@users.noreply.github.com"
          git add state.json
          git diff --cached --quiet || (git commit -m "update stats [skip ci]" && git push)
