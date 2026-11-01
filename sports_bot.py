from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
ODDS_KEY = os.environ["ODDS_API_KEY"]
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
ODDS = "https://api.the-odds-api.com/v4"

# Kalshi series ticker -> Odds API sport key. VERIFY the Kalshi tickers against your account.
SPORTS = json.loads(os.getenv("SPORTS_JSON", json.dumps({
    "KXNBAGAME": "basketball_nba", "KXNFLGAME": "americanfootball_nfl",
    "KXMLBGAME": "baseball_mlb", "KXNHLGAME": "icehockey_nhl",
    "KXNCAAFGAME": "americanfootball_ncaaf", "KXNCAAMBGAME": "basketball_ncaab",
    "KXEPLGAME": "soccer_epl", "KXMLSGAME": "soccer_usa_mls"})))

POLL = float(os.getenv("POLL_SECONDS", "60"))
ODDS_TTL = float(os.getenv("ODDS_TTL", "600"))      # each refresh costs ~2 API credits per sport
REGIONS = os.getenv("ODDS_REGIONS", "us,eu")        # eu brings in Pinnacle
MIN_ROI = float(os.getenv("MIN_ROI", "0.04"))       # EV / price needed to post
MIN_BOOKS = int(os.getenv("MIN_BOOKS", "4"))
PMIN, PMAX = float(os.getenv("PMIN", "0.08")), float(os.getenv("PMAX", "0.92"))
HOURS_AHEAD = float(os.getenv("HOURS_AHEAD", "24"))
TOP_N = int(os.getenv("TOP_N", "3"))
KELLY_FRAC = float(os.getenv("KELLY_FRAC", "0.25"))
RUN_SECONDS = int(os.getenv("RUN_SECONDS", str(345 * 60)))
STATE_FILE = os.getenv("STATE_FILE", "sports_state.json")
GREEN, RED, GREY = 0x00FF00, 0xFF3B30, 0x808080


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
        s.setdefault("history", []); s.setdefault("open", {})
        return s
    except Exception:
        return {"history": [], "open": {}}


def save_state(s):
    s["history"] = s["history"][-1000:]
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(s, f)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


# ---------- kalshi ----------
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


# ---------- fair odds ----------
ODDS_CACHE = {}


def odds_events(sport):
    c = ODDS_CACHE.get(sport)
    if c and time.time() - c[0] < ODDS_TTL:
        return c[1]
    ev = get_json(f"{ODDS}/sports/{sport}/odds",
                  {"apiKey": ODDS_KEY, "regions": REGIONS, "markets": "h2h", "oddsFormat": "decimal"})
    ODDS_CACHE[sport] = (time.time(), ev)
    return ev


def fair_probs(event):
    """De-vig every book, then blend Pinnacle (if present) with the cross-book median."""
    books = {}
    for b in event.get("bookmakers", []):
        mk = next((m for m in b["markets"] if m["key"] == "h2h"), None)
        if not mk:
            continue
        inv = {o["name"]: 1 / o["price"] for o in mk["outcomes"] if o["price"] > 1}
        tot = sum(inv.values())
        if len(inv) >= 2 and tot > 0:
            books[b["key"]] = {n: v / tot for n, v in inv.items()}
    if len(books) < MIN_BOOKS:
        return {}
    out = {}
    for n in set().union(*[set(v) for v in books.values()]):
        med = statistics.median(v[n] for v in books.values() if n in v)
        sharp = books.get("pinnacle", {}).get(n)
        out[n] = 0.6 * sharp + 0.4 * med if sharp is not None else med
    return out


def toks(s):
    return {t for t in re.findall(r"[a-z]+", (s or "").lower()) if len(t) >= 4}


def match_team(m, ev):
    """Which team does this YES contract pay on? None if unclear. THE fragile step, so test it."""
    sub, title = toks(m.get("yes_sub_title")), toks(m.get("title"))
    h, a = ev["home_team"], ev["away_team"]
    hit_h, hit_a = bool(sub & toks(h)), bool(sub & toks(a))
    if hit_h == hit_a:
        return None  # no match or ambiguous
    name, other = (h, a) if hit_h else (a, h)
    return name if (title & toks(other)) else None


# ---------- EV ----------
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
    picks = []
    for series, sport in SPORTS.items():
        try:
            mkts, evs = kalshi_markets(series), odds_events(sport)
        except Exception as e:
            print("scan error", series, repr(e), flush=True)
            continue
        evs = [e for e in evs if 0 < ts(e["commence_time"]) - time.time() < HOURS_AHEAD * 3600]
        fairs = {e["id"]: fair_probs(e) for e in evs}
        for m in mkts:
            hits = [(e, n) for e in evs if (n := match_team(m, e))]
            if len(hits) != 1:
                continue
            e, name = hits[0]
            p = fairs[e["id"]].get(name)
            r = evaluate(m, p) if p is not None else None
            if r and r["roi"] >= MIN_ROI:
                picks.append({**r, "ticker": m["ticker"], "series": series, "team": name,
                              "title": m.get("title", ""), "game": f'{e["away_team"]} at {e["home_team"]}',
                              "commence": ts(e["commence_time"]), "p_yes": p,
                              "books": len(e.get("bookmakers", []))})
    return sorted(picks, key=lambda x: -x["roi"])


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
    act = f"Buy **{p['side']}** at {p['price'] * 100:.0f}c"
    who = p["team"] if p["side"] == "YES" else f"against {p['team']}"
    lines = [f"**Game:** {p['game']}", f"**Pick:** {act} ({who})",
             f"**Fair win prob:** {p['p'] * 100:.1f}% vs price {p['price'] * 100:.0f}c ({p['books']} books)",
             f"**EV:** {p['ev']:+.3f} per $1 contract after fees ({p['roi'] * 100:+.1f}% ROI)",
             f"**Suggested stake:** {p['kelly'] * 100:.1f}% of bankroll (quarter Kelly)",
             f"**Starts:** <t:{int(p['commence'])}:R>", f"**Market:** `{p['ticker']}`"]
    if outcome == "win":
        title, color = f"✅ WIN | {p['game']}", GREEN
        lines.insert(0, f"**Result:** {p['pnl']:+.2f}u")
    elif outcome == "loss":
      
