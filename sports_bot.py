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
ML_URL = os.getenv("MONEYLINE_URL", "https://mlapi.bet/v1/edge/ev")
ML_BASE = os.getenv("MONEYLINE_BASE", "https://mlapi.bet")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"

# Kalshi series scanned every tick (free, no key).
SERIES = [s.strip() for s in os.getenv(
    "SERIES",
    "KXNBAGAME,KXNFLGAME,KXNHLGAME,KXNCAAFGAME,KXNCAAMBGAME,KXEPLGAME,KXMLSGAME,KXATPMATCH,KXWTAMATCH"
).split(",") if s.strip()]

# League label shown next to team names so "Atlanta" or "Ohio" is never ambiguous.
SPORT = {"KXNBAGAME": "NBA", "KXNFLGAME": "NFL", "KXNHLGAME": "NHL", "KXNCAAFGAME": "NCAAF",
         "KXNCAAMBGAME": "NCAAM", "KXEPLGAME": "EPL", "KXMLSGAME": "MLS",
         "KXATPMATCH": "ATP", "KXWTAMATCH": "WTA"}

# ---------- full team names (city + nickname, school + mascot) ----------
TEAMS_FILE = os.getenv("TEAMS_FILE", "team_names.json")   # optional: {"NCAAM": {"Ohio": "Ohio Bobcats"}}


def _norm(s):
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"['’.]", "", s)
    s = re.sub(r"[()\-,]", " ", s)
    s = re.sub(r"\bstate\b", "st", s)
    s = re.sub(r"\bsaint\b", "st", s)
    return re.sub(r"\s+", " ", s).strip()


NBA = ("Atlanta Hawks;Boston Celtics;Brooklyn Nets;Charlotte Hornets;Chicago Bulls;Cleveland Cavaliers;"
       "Dallas Mavericks;Denver Nuggets;Detroit Pistons;Golden State Warriors;Houston Rockets;Indiana Pacers;"
       "Los Angeles Lakers;Los Angeles Clippers;Memphis Grizzlies;Miami Heat;Milwaukee Bucks;"
       "Minnesota Timberwolves;New Orleans Pelicans;New York Knicks;Oklahoma City Thunder;Orlando Magic;"
       "Philadelphia 76ers;Phoenix Suns;Portland Trail Blazers;Sacramento Kings;San Antonio Spurs;"
       "Toronto Raptors;Utah Jazz;Washington Wizards")
NFL = ("Arizona Cardinals;Atlanta Falcons;Baltimore Ravens;Buffalo Bills;Carolina Panthers;Chicago Bears;"
       "Cincinnati Bengals;Cleveland Browns;Dallas Cowboys;Denver Broncos;Detroit Lions;Green Bay Packers;"
       "Houston Texans;Indianapolis Colts;Jacksonville Jaguars;Kansas City Chiefs;Las Vegas Raiders;"
       "Los Angeles Chargers;Los Angeles Rams;Miami Dolphins;Minnesota Vikings;New England Patriots;"
       "New Orleans Saints;New York Giants;New York Jets;Philadelphia Eagles;Pittsburgh Steelers;"
       "San Francisco 49ers;Seattle Seahawks;Tampa Bay Buccaneers;Tennessee Titans;Washington Commanders")
NHL = ("Anaheim Ducks;Boston Bruins;Buffalo Sabres;Calgary Flames;Carolina Hurricanes;Chicago Blackhawks;"
       "Colorado Avalanche;Columbus Blue Jackets;Dallas Stars;Detroit Red Wings;Edmonton Oilers;"
       "Florida Panthers;Los Angeles Kings;Minnesota Wild;Montreal Canadiens;Nashville Predators;"
       "New Jersey Devils;New York Islanders;New York Rangers;Ottawa Senators;Philadelphia Flyers;"
       "Pittsburgh Penguins;San Jose Sharks;Seattle Kraken;St. Louis Blues;Tampa Bay Lightning;"
       "Toronto Maple Leafs;Utah Mammoth;Vancouver Canucks;Vegas Golden Knights;Washington Capitals;"
       "Winnipeg Jets")
EPL = ("Arsenal;Aston Villa;AFC Bournemouth;Brentford;Brighton and Hove Albion;Burnley;Chelsea;Crystal Palace;"
       "Everton;Fulham;Leeds United;Liverpool;Manchester City;Manchester United;Newcastle United;"
       "Nottingham Forest;Sunderland;Tottenham Hotspur;West Ham United;Wolverhampton Wanderers")
MLS = ("Atlanta United FC;Austin FC;Charlotte FC;Chicago Fire FC;FC Cincinnati;Colorado Rapids;Columbus Crew;"
       "D.C. United;FC Dallas;Houston Dynamo FC;Inter Miami CF;LA Galaxy;Los Angeles FC;Minnesota United FC;"
       "CF Montreal;Nashville SC;New England Revolution;New York City FC;New York Red Bulls;Orlando City SC;"
       "Philadelphia Union;Portland Timbers;Real Salt Lake;San Diego FC;San Jose Earthquakes;"
       "Seattle Sounders FC;Sporting Kansas City;St. Louis City SC;Toronto FC;Vancouver Whitecaps FC")

# school=mascot (full name is built as "school mascot")
COLLEGE = (
    "Boston College=Eagles;California=Golden Bears;Clemson=Tigers;Duke=Blue Devils;Florida State=Seminoles;"
    "Georgia Tech=Yellow Jackets;Louisville=Cardinals;Miami (FL)=Hurricanes;NC State=Wolfpack;"
    "North Carolina=Tar Heels;Notre Dame=Fighting Irish;Pittsburgh=Panthers;SMU=Mustangs;Stanford=Cardinal;"
    "Syracuse=Orange;Virginia=Cavaliers;Virginia Tech=Hokies;Wake Forest=Demon Deacons;"
    "Illinois=Fighting Illini;Indiana=Hoosiers;Iowa=Hawkeyes;Maryland=Terrapins;Michigan=Wolverines;"
    "Michigan State=Spartans;Minnesota=Golden Gophers;Nebraska=Cornhuskers;Northwestern=Wildcats;"
    "Ohio State=Buckeyes;Oregon=Ducks;Penn State=Nittany Lions;Purdue=Boilermakers;Rutgers=Scarlet Knights;"
    "UCLA=Bruins;USC=Trojans;Washington=Huskies;Wisconsin=Badgers;"
    "Arizona=Wildcats;Arizona State=Sun Devils;Baylor=Bears;BYU=Cougars;Cincinnati=Bearcats;Colorado=Buffaloes;"
    "Houston=Cougars;Iowa State=Cyclones;Kansas=Jayhawks;Kansas State=Wildcats;Oklahoma State=Cowboys;"
    "TCU=Horned Frogs;Texas Tech=Red Raiders;UCF=Knights;Utah=Utes;West Virginia=Mountaineers;"
    "Alabama=Crimson Tide;Arkansas=Razorbacks;Auburn=Tigers;Florida=Gators;Georgia=Bulldogs;Kentucky=Wildcats;"
    "LSU=Tigers;Mississippi State=Bulldogs;Missouri=Tigers;Oklahoma=Sooners;Ole Miss=Rebels;"
    "South Carolina=Gamecocks;Tennessee=Volunteers;Texas=Longhorns;Texas A&M=Aggies;Vanderbilt=Commodores;"
    "Butler=Bulldogs;Creighton=Bluejays;DePaul=Blue Demons;Georgetown=Hoyas;Marquette=Golden Eagles;"
    "Providence=Friars;Seton Hall=Pirates;St. John's=Red Storm;UConn=Huskies;Villanova=Wildcats;"
    "Xavier=Musketeers;Gonzaga=Bulldogs;Memphis=Tigers;San Diego State=Aztecs;Saint Mary's=Gaels;"
    "Dayton=Flyers;VCU=Rams;Nevada=Wolf Pack;Boise State=Broncos;Colorado State=Rams;Utah State=Aggies;"
    "New Mexico=Lobos;UNLV=Rebels;Wyoming=Cowboys;Fresno State=Bulldogs;Air Force=Falcons;Army=Black Knights;"
    "Navy=Midshipmen;Tulane=Green Wave;Tulsa=Golden Hurricane;Temple=Owls;South Florida=Bulls;"
    "East Carolina=Pirates;Charlotte=49ers;North Texas=Mean Green;Rice=Owls;UTSA=Roadrunners;"
    "Florida Atlantic=Owls;Ohio=Bobcats;Toledo=Rockets;Miami (OH)=RedHawks;Bowling Green=Falcons;"
    "Kent State=Golden Flashes;Akron=Zips;Buffalo=Bulls;Ball State=Cardinals;Central Michigan=Chippewas;"
    "Eastern Michigan=Eagles;Northern Illinois=Huskies;Western Michigan=Broncos;UMass=Minutemen;"
    "App State=Mountaineers;Coastal Carolina=Chanticleers;Georgia Southern=Eagles;Georgia State=Panthers;"
    "James Madison=Dukes;Marshall=Thundering Herd;Old Dominion=Monarchs;South Alabama=Jaguars;"
    "Southern Miss=Golden Eagles;Texas State=Bobcats;Troy=Trojans;Arkansas State=Red Wolves;"
    "Louisiana=Ragin' Cajuns;Liberty=Flames;Jacksonville State=Gamecocks;Kennesaw State=Owls;"
    "Middle Tennessee=Blue Raiders;New Mexico State=Aggies;Sam Houston=Bearcats;UTEP=Miners;"
    "Western Kentucky=Hilltoppers;FIU=Panthers;Oregon State=Beavers;Washington State=Cougars;"
    "Hawaii=Rainbow Warriors;San Jose State=Spartans;Saint Joseph's=Hawks;La Salle=Explorers;"
    "Davidson=Wildcats;Richmond=Spiders;George Mason=Patriots;George Washington=Revolutionaries;"
    "Fordham=Rams;Loyola Chicago=Ramblers;Drake=Bulldogs;Bradley=Braves;Illinois State=Redbirds;"
    "Indiana State=Sycamores;Murray State=Racers;Belmont=Bruins;Duquesne=Dukes;Rhode Island=Rams;"
    "Penn=Quakers;Princeton=Tigers;Harvard=Crimson;Yale=Bulldogs;Cornell=Big Red;Columbia=Lions;"
    "Brown=Bears;Dartmouth=Big Green;Oral Roberts=Golden Eagles;Grand Canyon=Lopes;Santa Clara=Broncos;"
    "San Francisco=Dons;Pepperdine=Waves;Saint Louis=Billikens;Wichita State=Shockers;"
    "Northern Iowa=Panthers;Furman=Paladins;Chattanooga=Mocs;Delaware=Blue Hens;Towson=Tigers")

ALIASES = {
    "NBA": {"Los Angeles L": "Los Angeles Lakers", "Los Angeles C": "Los Angeles Clippers",
            "LA Lakers": "Los Angeles Lakers", "LA Clippers": "Los Angeles Clippers"},
    "NFL": {"Los Angeles C": "Los Angeles Chargers", "Los Angeles R": "Los Angeles Rams",
            "New York G": "New York Giants", "New York J": "New York Jets"},
    "NHL": {"New York I": "New York Islanders", "New York R": "New York Rangers"},
    "EPL": {"Man City": "Manchester City", "Man United": "Manchester United", "Man Utd": "Manchester United",
            "Spurs": "Tottenham Hotspur", "Wolves": "Wolverhampton Wanderers", "Bournemouth": "AFC Bournemouth",
            "Nottm Forest": "Nottingham Forest", "Forest": "Nottingham Forest"},
    "MLS": {"LAFC": "Los Angeles FC", "NYCFC": "New York City FC", "Kansas City": "Sporting Kansas City",
            "Montreal": "CF Montreal", "Miami": "Inter Miami CF", "Inter Miami": "Inter Miami CF",
            "Columbus": "Columbus Crew", "Salt Lake": "Real Salt Lake"},
}
COLLEGE_ALIASES = {"Connecticut": "UConn", "Pitt": "Pittsburgh", "Mississippi": "Ole Miss", "Cal": "California",
                   "Southern California": "USC", "Appalachian State": "App State", "UL Lafayette": "Louisiana",
                   "Florida International": "FIU", "Central Florida": "UCF", "Massachusetts": "UMass",
                   "Southern Methodist": "SMU", "Louisiana State": "LSU", "Brigham Young": "BYU"}


def _prefix_map(names):
    """city / nickname-less prefixes -> full name; prefixes shared by two teams are dropped."""
    m, bad = {}, set()
    for full in names:
        w = _norm(full).split()
        for i in range(1, len(w) + 1):
            key = " ".join(w[:i])
            if key in m and m[key] != full:
                bad.add(key)
            m.setdefault(key, full)
    for b in bad:
        m.pop(b, None)
    return m


def _build_names():
    out = {}
    for lg, raw in (("NBA", NBA), ("NFL", NFL), ("NHL", NHL), ("EPL", EPL), ("MLS", MLS)):
        mp = _prefix_map(raw.split(";"))
        for a, full in ALIASES.get(lg, {}).items():
            mp[_norm(a)] = full
        out[lg] = mp
    col = {}
    schools = dict(x.split("=", 1) for x in COLLEGE.split(";"))
    for school, mascot in schools.items():
        col[_norm(school)] = f"{school} {mascot}"
    for a, school in COLLEGE_ALIASES.items():
        if school in schools:
            col[_norm(a)] = f"{school} {schools[school]}"
    out["NCAAF"], out["NCAAM"] = col, dict(col)
    try:
        with open(TEAMS_FILE) as f:
            for lg, mp in json.load(f).items():
                out.setdefault(lg, {}).update({_norm(k): v for k, v in mp.items()})
    except Exception:
        pass
    return out


NAME_MAP = _build_names()
MISSING = set()


def full_name(short, league, book=None):
    """Full team name: city+nickname for pro teams, school+mascot for colleges. Tennis keeps player names."""
    short = (short or "").strip()
    if not short or _norm(short) in ("tie", "draw"):
        return short
    if league not in NAME_MAP:   # tennis: players, no mascots
        return book if book and len(book.split()) > len(short.split()) else short
    hit = NAME_MAP.get(league, {}).get(_norm(short))
    if hit:
        return hit
    if book and len(book.split()) > len(short.split()):
        return book
    if (league, short) not in MISSING:
        MISSING.add((league, short))
        print(f"no full name for {league} '{short}': add it to {TEAMS_FILE}", flush=True)
    return short


def full_game(title, league):
    """'Atlanta at Boston' -> 'Atlanta Hawks at Boston Celtics'."""
    parts = re.split(r"\s+(?:at|vs\.?|v)\s+", title, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return title
    sep = " at " if re.search(r"\sat\s", title, re.I) else " vs "
    return f"{full_name(parts[0], league)}{sep}{full_name(parts[1], league)}"


SRC_LABEL = "MoneyLine"
POLL = float(os.getenv("POLL_SECONDS", "15"))
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.10"))
PMIN, PMAX = float(os.getenv("PMIN", "0.15")), float(os.getenv("PMAX", "0.92"))
HOURS_AHEAD = float(os.getenv("HOURS_AHEAD", "48"))
BOOK_W = float(os.getenv("BOOK_W", "0.75"))
TOP_N = int(os.getenv("TOP_N", "3"))                      # max new picks per scan
MAX_OPEN = int(os.getenv("MAX_OPEN", "25"))               # max open picks overall
KELLY_FRAC = float(os.getenv("KELLY_FRAC", "0.25"))
MAX_STAKE = float(os.getenv("MAX_STAKE", "0.05"))
MIN_GROWTH = float(os.getenv("MIN_GROWTH", "0.0001"))
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.01"))
UNCERT = float(os.getenv("UNCERT", "0.005"))
PICK_FLOOR = float(os.getenv("PICK_FLOOR_SECONDS", "300"))  # post a low-confidence pick if none in this long
STATUS_EVERY = float(os.getenv("STATUS_SECONDS", "3600"))   # status update interval
WATCH_EVERY = float(os.getenv("WATCH_SECONDS", "3600"))     # hourly watchlist interval
WATCH_N = int(os.getenv("WATCH_N", "3"))                    # games on the watchlist
REQUIRE_BOOK = os.getenv("REQUIRE_BOOK", "0") == "1"
ARB_MIN = float(os.getenv("ARB_MIN", "0.01"))          # min locked profit per set, in dollars
ARB_REPEAT = float(os.getenv("ARB_REPEAT", "1800"))    # seconds before re-alerting the same game
RUN_SECONDS = int(os.getenv("RUN_SECONDS", str(345 * 60)))
STATE_FILE = os.getenv("STATE_FILE", "sports_state.json")
EDIT_SECONDS = float(os.getenv("EDIT_SECONDS", "120"))
LIVE_SCALE = float(os.getenv("LIVE_SCALE", "1.0"))
FINAL_SCALE = float(os.getenv("FINAL_SCALE", "2"))
MAX_BYTES = int(float(os.getenv("MAX_MB", "7.5")) * 1024 * 1024)
GREEN, RED, GREY = 0x00FF00, 0xFF3B30, 0x808080

# MoneyLine credit controls
ODDS_TTL = float(os.getenv("ODDS_TTL", "2700"))           # seconds between MoneyLine refreshes
DAILY_CREDITS = int(os.getenv("DAILY_CREDITS", "32"))     # hard cap per UTC day
MONTHLY_CREDITS = int(os.getenv("MONTHLY_CREDITS", "1000"))
CALL_COST = int(os.getenv("CALL_COST", "1"))              # credits per page fetched
MIN_REMAINING = int(os.getenv("MIN_REMAINING", "20"))     # stop calling when account is this low
ML_PAGES = int(os.getenv("ML_PAGES", "1"))                # pages (25 bets each) fetched per refresh
STALE_MAX = float(os.getenv("STALE_MAX", "7200"))         # reuse old odds this long when over budget or erroring
RETRY_AFTER = float(os.getenv("RETRY_AFTER", "300"))      # seconds before retrying a failed MoneyLine call
EDGE_MAX_AGE = float(os.getenv("EDGE_MAX_AGE", "10800"))  # ignore MoneyLine bets calculated longer ago
MAX_DISAGREE = float(os.getenv("MAX_DISAGREE", "0.20"))   # distrust fair values this far from Kalshi
BUDGET = {"day": "", "used": 0, "month": "", "month_used": 0, "remaining": None}
API_DOWN = [False]
LOADED = [0]
RETRY_AT = [0.0]
EMPTY_SERIES = set()


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


def ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


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
    return math.ceil(round(0.07 * p * (1 - p) * 100, 6)) / 100


# ---------- sportsbook fair values (MoneyLine EV bets, credit-capped) ----------
ODDS_CACHE = {}

STOP = {"state", "university", "college", "the", "city", "united", "fc", "sc", "new", "los", "san",
        "saint", "north", "south", "east", "west", "tech", "univ", "and", "vs"}


def _words(s):
    return {w for w in re.findall(r"[a-z]+", (s or "").lower()) if len(w) >= 3 and w not in STOP}


def _find_list(x, depth=0):
    if isinstance(x, list):
        return x
    if isinstance(x, dict) and depth < 4:
        for k in ("items", "results", "bets", "edges", "events", "data"):
            if k in x:
                r = _find_list(x[k], depth + 1)
                if r is not None:
                    return r
        for v in x.values():
            r = _find_list(v, depth + 1)
            if r is not None:
                return r
    return None


def _budget_roll():
    now = datetime.now(timezone.utc)
    d, m = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    if BUDGET["day"] != d:
        BUDGET["day"], BUDGET["used"] = d, 0
        API_DOWN[0] = False
    if BUDGET["month"] != m:
        BUDGET["month"], BUDGET["month_used"] = m, 0


def _parse_edges(items, now):
    """Each MoneyLine EV bet carries the consensus (de-vigged) win probability of one team."""
    best = {}
    for it in items:
        try:
            ev = it.get("evBet") or {}
            fair = ev.get("fairProb")
            if fair is None or it.get("market") != "moneyline":
                continue
            team = it.get("team") or it.get("outcome")
            words = _words(team)
            if not words:
                continue
            if it.get("calculatedAt") and now - ts(it["calculatedAt"]) > EDGE_MAX_AGE:
                continue
            key = (it.get("eventId"), team)
            cand = {"start": ts(it["startTime"]), "team": words, "name": str(team).strip(),
                    "fair": float(fair), "sport": it.get("sport"),
                    "books": ev.get("consensusBooks") or 0}
            if key not in best or cand["books"] > best[key]["books"]:
                best[key] = cand
        except Exception:
            continue
    return list(best.values())


def odds_games(series):
    """One MoneyLine refresh covers every sport. Hard-capped by daily/monthly credits and real balance."""
    if not ODDS_KEY:
        return []
    _budget_roll()
    c = ODDS_CACHE.get("ALL")
    age = time.time() - c[0] if c else None
    if c and age < ODDS_TTL:
        return c[1]
    if time.time() < RETRY_AT[0]:
        # recent failure: keep using older fair values until they go stale
        return c[1] if c and age < STALE_MAX else []
    rem = BUDGET.get("remaining")
    over = (BUDGET["used"] + CALL_COST > DAILY_CREDITS or
            BUDGET["month_used"] + CALL_COST > MONTHLY_CREDITS or
            (rem is not None and rem <= MIN_REMAINING))
    if over:
        if c and c[1] and age < STALE_MAX:
            return c[1]                      # recent fair values are still fine
        if not API_DOWN[0]:
            print("moneyline budget used up: running on Kalshi data only", flush=True)
        API_DOWN[0] = True
        return []
    games, now = [], time.time()
    try:
        for page in range(1, ML_PAGES + 1):
            BUDGET["used"] += CALL_COST
            BUDGET["month_used"] += CALL_COST
            r = DATA.get(ML_URL, params={"page": page} if page > 1 else None,
                         headers={"x-api-key": ODDS_KEY}, timeout=(3.05, 15))
            r.raise_for_status()
            rh = r.headers.get("x-credits-remaining")
            if rh is not None:
                try:
                    BUDGET["remaining"] = int(float(rh))
                except ValueError:
                    pass
            body = r.json()
            games.extend(_parse_edges(_find_list(body) or [], now))
            pages = ((body.get("meta") or {}).get("pages") or 1) if isinstance(body, dict) else 1
            if page >= pages:
                break
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (401, 402, 403, 429):
            BUDGET["used"] = DAILY_CREDITS   # provider refused: stop for today
        API_DOWN[0] = True
        RETRY_AT[0] = time.time() + RETRY_AFTER   # keep the old cache, just retry soon
        print("moneyline error", repr(e), "-> retry in", int(RETRY_AFTER), "s", flush=True)
        return c[1] if c and age < STALE_MAX else []
    API_DOWN[0] = False
    RETRY_AT[0] = 0.0
    LOADED[0] = len(games)
    print(f"moneyline ok: {len(games)} fair values | credits today {BUDGET['used']}/{DAILY_CREDITS} | "
          f"account remaining {BUDGET.get('remaining')}", flush=True)
    ODDS_CACHE["ALL"] = (time.time(), games)
    return games


def match_game(m, games, end):
    """Return (fair YES probability, full team name) if exactly one MoneyLine bet matches this market.
    Games that already started are skipped: their pre-game odds are stale against live Kalshi prices."""
    sub = (m.get("yes_sub_title") or "").lower().strip()
    subt = _words(sub)
    if not subt or sub in ("tie", "draw"):
        return None, None
    now = time.time()
    found = None
    for g in games:
        if g["start"] <= now + 120:
            continue
        if not (-3600 < end - g["start"] < 48 * 3600):
            continue
        if not (subt <= g["team"] or g["team"] <= subt):
            continue
        if found is not None:
            return None, None  # ambiguous
        found = (g["fair"], g["name"])
    return found if found else (None, None)


def injury_probe():
    """One-time probe: NBA team list plus one NBA team's injuries. Logs raw responses."""
    team = os.getenv("INJURY_PROBE_TEAM", "nba-boston-celtics")
    for path in ("/v1/leagues/nba/teams", f"/v1/teams/{team}/injuries"):
        try:
            r = DATA.get(f"{ML_BASE}{path}", headers={"x-api-key": ODDS_KEY}, timeout=(3.05, 15))
            print("probe", path, "status", r.status_code, "| cost", r.headers.get("x-credits-cost"),
                  "| remaining", r.headers.get("x-credits-remaining"), flush=True)
            print("probe raw:", json.dumps(r.json())[:1200], flush=True)
        except Exception as e:
            print("probe error", path, repr(e), flush=True)


# ---------- pick scoring ----------
def evaluate(m, p_yes, margin, yes_only=False):
    """yes_only=True for two-team games: the opposing team's own YES market covers the other side,
    so a pick is always 'buy YES on the team we like' instead of 'NO against the other team'."""
    ya, yb = dollars(m, "yes_ask"), dollars(m, "yes_bid")
    sides = [("YES", p_yes, ya)]
    if not yes_only:
        sides.append(("NO", 1 - p_yes, None if yb is None else 1 - yb))
    best = None
    for side, p, price in sides:
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


def lean(m, p_yes, yes_only=False):
    """Best side to lean on, even if EV is negative (watchlist only)."""
    ya, yb = dollars(m, "yes_ask"), dollars(m, "yes_bid")
    sides = [("YES", p_yes, ya)]
    if not yes_only:
        sides.append(("NO", 1 - p_yes, None if yb is None else 1 - yb))
    best = None
    for side, p, price in sides:
        if price is None or not (PMIN <= price <= PMAX):
            continue
        ev = p - (price + fee(price))
        if best is None or ev > best["ev"]:
            best = {"side": side, "price": price, "p": p, "ev": ev}
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
    """Returns (picks, relaxed, marks, arbs, leans).
    marks[ticker] = (fair_yes, margin, yes_ask, yes_bid) for live updates of open picks."""
    picks, relaxed, marks, arbs, now = [], [], {}, [], time.time()
    leans = []
    st = {"markets": 0, "no_quote": 0, "wide": 0, "no_book": 0, "no_edge": 0, "thin": 0, "matched": 0}
    near = None
    for series in SERIES:
        try:
            mkts = kalshi_markets(series)
        except Exception as e:
            print("scan error", series, repr(e), flush=True)
            continue
        if not mkts and series not in EMPTY_SERIES:
            EMPTY_SERIES.add(series)
            print(f"no open markets for series {series}: check the ticker or it may be off-season", flush=True)
        games = odds_games(series) if any(in_window(m, now) for m in mkts) else []
        league = SPORT.get(series, series)
        events = {}
        for m in mkts:
            events.setdefault(m.get("event_ticker") or m["ticker"], []).append(m)
        for ev_key, ms in events.items():
            arb = find_arb(ms, now)
            if arb:
                arb["event"] = ev_key
                arb["title"] = full_game((ms[0].get("title") or ev_key).replace(" Winner?", ""), league)
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
            # two-team game (no draw): a "NO" on one team is shown as a YES on the other team
            subs = [(x.get("yes_sub_title") or "").lower().strip() for x in ms]
            two_way = len(ms) == 2 and not any(s in ("tie", "draw") for s in subs)
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
                book_p, book_name = match_game(m, games, end)
                if book_p is not None and abs(book_p - kfair) > MAX_DISAGREE:
                    book_p, book_name = None, None   # too far apart: likely stale or mismatched
                if book_p is not None:
                    fair = BOOK_W * book_p + (1 - BOOK_W) * kfair
                    margin = UNCERT + 0.5 * abs(book_p - kfair)
                    src = SRC_LABEL + "+Kalshi"
                    st["matched"] += 1
                elif REQUIRE_BOOK and not API_DOWN[0]:
                    st["no_book"] += 1
                    continue
                marks[m["ticker"]] = (fair, margin, a, b)
                label = full_name(m.get("yes_sub_title"), league, book_name) if m.get("yes_sub_title") else m["ticker"]
                title = full_game((m.get("title") or "").replace(" Winner?", ""), league)
                game = f"{league} | {title}" if title else f"{league} | {m['ticker']}"
                lz = lean(m, fair, two_way)
                if lz:
                    leans.append({**lz, "team": label, "game": game,
                                  "ticker": m["ticker"], "event": ev_key, "src": src})
                raw = max(fair - a, b - fair)
                if near is None or raw > near[0]:
                    near = (raw, m["ticker"], src)
                r = evaluate(m, fair, margin, two_way)
                base_pick = {"ticker": m["ticker"], "series": series, "event": ev_key,
                             "team": label, "title": title,
                             "game": game, "commence": end,
                             "p_yes": fair, "books": len(ms), "src": src}
                if r and r["growth"] >= MIN_GROWTH and r["edge"] >= MIN_EDGE:
                    picks.append({**r, **base_pick})
                    continue
                r0 = evaluate(m, fair, 0.0, two_way)   # relaxed: no safety margin, EV must still be positive
                if r0 and r0["ev"] > 0:
                    relaxed.append({**r0, **base_pick, "src": src + " (low confidence)",
                                    "lowconf": True})
                    st["thin"] += 1
                else:
                    st["no_edge"] += 1
    if now - LAST_LOG[0] >= 120:
        LAST_LOG[0] = now
        nr = f"{near[0] * 100:+.1f}c on {near[1]} ({near[2]})" if near else "n/a"
        print(f"scan: {st['markets']} markets | book-matched {st['matched']} | no quote {st['no_quote']} | "
              f"wide {st['wide']} | no book {st['no_book']} | no edge {st['no_edge']} | "
              f"thin {st['thin']} | PICKS {len(picks)} | LOWCONF {len(relaxed)} | ARBS {len(arbs)} | "
              f"api {'DOWN' if API_DOWN[0] else 'ok'} | best raw edge {nr}", flush=True)
    return (sorted(picks, key=lambda x: -x["growth"]),
            sorted(relaxed, key=lambda x: -x["growth"]), marks, arbs, leans)


# ---------- chart ----------
CW, CH = 800, 480
WHITE, MUTED = (255, 255, 255, 255), (160, 168, 178, 255)
C_UP, C_DN = (0, 217, 142), (255, 87, 87)
AMBER = (255, 190, 40)
NEON_C, NEON_M, NEON_P, NEON_G = (34, 229, 240), (232, 58, 216), (255, 61, 139), (34, 229, 138)


@functools.lru_cache(maxsize=64)
def _mono(size, bold=True):
    names = ([os.getenv("CHART_FONT", "")] +
             (["DejaVuSansMono-Bold.ttf", "LiberationMono-Bold.ttf", "courbd.ttf"] if bold
              else ["DejaVuSansMono.ttf", "LiberationMono-Regular.ttf", "cour.ttf"]))
    for nm in names:
        if nm:
            try:
                return ImageFont.truetype(nm, size)
            except OSError:
                pass
    return ImageFont.load_default()


def _sp(d, xy, s, font, fill, sp=0.0, anchor="l"):
    """Letter-spaced text. anchor: l, m or r (vertically centred)."""
    w = sum(d.textlength(c, font=font) + sp for c in s) - sp
    x, y = xy
    x = x - w if anchor == "r" else x - w / 2 if anchor == "m" else x
    for c in s:
        d.text((x, y), c, font=font, fill=fill, anchor="lm")
        x += d.textlength(c, font=font) + sp


def _vgrad(w, h, top, bot):
    g = Image.linear_gradient("L").resize((max(1, int(w)), max(1, int(h))))
    return ImageOps.colorize(g, top, bot).convert("RGBA")


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
    lw = lambda v: max(1, int(k(v)))
    M = lambda sz, b=True: _mono(max(6, int(k(sz))), b)
    W, H = int(CW * S), int(CH * S)
    accent = {"win": C_UP, "loss": C_DN}.get(outcome)
    price, fair = p["price"], max(p["p"], 0.01)
    src = p.get("src", "Kalshi")
    edge = (fair - price) * 100
    ev_c = NEON_G if edge >= 0 else (255, 87, 87)
    side_col = NEON_G if p["side"] == "YES" else NEON_P
    pts = p.get("pts") or [[time.time(), price, fair]]
    t0 = pts[0][0]
    t1 = max(pts[-1][0], t0 + 120)
    ang = lambda q: (-90 + 360 * q) % 360

    # background, grid, margin dots
    base = _vgrad(W, H, (42, 10, 58), (100, 12, 36))
    d = ImageDraw.Draw(base, "RGBA")
    for x in range(0, CW + 1, 25):
        d.line([(k(x), 0), (k(x), H)], fill=(255, 255, 255, 13), width=1)
    for y in range(0, CH + 1, 25):
        d.line([(0, k(y)), (W, k(y))], fill=(255, 255, 255, 13), width=1)
    rng = random.Random(sum(map(ord, p["team"])))
    for _ in range(10):
        x = rng.randint(4, CW - 4)
        y = rng.choice((rng.randint(4, 18), rng.randint(CH - 18, CH - 4)))
        d.ellipse([k(x - 1.5), k(y - 1.5), k(x + 1.5), k(y + 1.5)], fill=NEON_C + (150,))

    # card with gradient outline
    CX0, CY0, CX1, CY1 = 24, 24, 776, 456
    d.rounded_rectangle([k(CX0), k(CY0), k(CX1), k(CY1)], radius=k(16), fill=(26, 5, 34, 238))
    gw, gh = int(k(CX1 - CX0)), int(k(CY1 - CY0))
    grad = ImageOps.colorize(Image.linear_gradient("L").rotate(90, expand=True).resize((gw, gh)),
                             NEON_C, NEON_M).convert("RGBA")
    ring = Image.new("L", (gw, gh), 0)
    ImageDraw.Draw(ring).rounded_rectangle([0, 0, gw - 1, gh - 1], radius=int(k(16)), outline=255,
                                           width=lw(2.5))
    base.paste(grad, (int(k(CX0)), int(k(CY0))), ring)
    d = ImageDraw.Draw(base, "RGBA")

    # header: shows the team we are backing (a NO only appears on 3-way markets like soccer)
    cx, cy = k(50), k(52)
    d.polygon([(cx, cy - k(7)), (cx + k(7), cy), (cx, cy + k(7)), (cx - k(7), cy)], fill=NEON_C + (255,))
    head = p["team"] if p["side"] == "YES" else f"{p['team']} NO"
    _sp(d, (k(66), k(52)), head.upper()[:40], M(16), NEON_C + (255,), k(1))
    _sp(d, (k(44), k(76)), p["game"][:72], M(9, False), MUTED, k(0.5))
    left = p["commence"] - time.time()
    if outcome is None:
        _sp(d, (k(734), k(52)), "LIVE", M(10), NEON_M + (255,), k(1.5), "r")
        _sp(d, (k(756), k(76)), "CLOSES " + _hhmm(left), M(9, False), MUTED, k(0.5), "r")
    else:
        _sp(d, (k(756), k(52)), outcome.upper(), M(14), accent + (255,), k(1.5), "r")
    _sp(d, (k(44), k(104)), "EDGE HISTORY", M(10, False), (214, 58, 208, 255), k(1.5))
    _sp(d, (k(444), k(104)), "LIVE ODDS", M(10, False), (214, 58, 208, 255), k(1.5))

    # 7 edge bars
    vals = [(b - a) * 100 for _, a, b in pts][-7:]
    slots = [None] * (7 - len(vals)) + vals
    emax = max([abs(v) for v in vals] + [0.5])
    BY, bw_, pitch = 206, 38, 52
    for i, v in enumerate(slots):
        x = 44 + i * pitch
        if v is None:
            d.rounded_rectangle([k(x), k(BY - 3), k(x + bw_), k(BY)], radius=k(1), fill=(255, 255, 255, 22))
        else:
            h_ = max(6, abs(v) / emax * 78)
            top, bot = (NEON_C, (255, 45, 168)) if v >= 0 else ((255, 87, 87), (140, 20, 60))
            gi = _vgrad(k(bw_), k(h_), top, bot)
            mk = Image.new("L", gi.size, 0)
            ImageDraw.Draw(mk).rounded_rectangle([0, 0, gi.size[0] - 1, gi.size[1] - 1], radius=int(k(3)), fill=255)
            base.paste(gi, (int(k(x)), int(k(BY - h_))), mk)
            d = ImageDraw.Draw(base, "RGBA")
            d.text((k(x + bw_ / 2), k(BY - h_ - 7)), f"{v:+.1f}", font=M(8, False), fill=WHITE, anchor="mm")
        d.text((k(x + bw_ / 2), k(BY + 12)), f"{i + 1:02d}", font=M(8, False), fill=(138, 127, 168, 255), anchor="mm")
    d.line([(k(44), k(BY)), (k(394), k(BY))], fill=(110, 106, 142, 255), width=lw(1))

    # price vs fair panel
    d.rounded_rectangle([k(44), k(232), k(404), k(352)], radius=k(4), fill=(20, 3, 28, 255),
                        outline=(42, 168, 184, 180), width=lw(1))
    _sp(d, (k(54), k(244)), "PRICE vs FAIR", M(8, False), MUTED, k(1))
    _sp(d, (k(160), k(244)), "● KALSHI", M(8), NEON_M + (255,), k(0.5))
    _sp(d, (k(232), k(244)), "● FAIR", M(8), NEON_C + (255,), k(0.5))
    pv, fv = [a * 100 for _, a, _ in pts], [b * 100 for _, _, b in pts]
    entry = (p.get("entry_price") or price) * 100
    allv = pv + fv + [entry]
    lo, hi = min(allv), max(allv)
    if hi - lo < 4:
        mid = (hi + lo) / 2
        lo, hi = mid - 2, mid + 2
    pad = (hi - lo) * 0.15
    lo, hi = lo - pad, hi + pad
    PX0, PX1, PY0, PY1 = k(60), k(366), k(262), k(340)
    X = lambda t: PX0 + (t - t0) / (t1 - t0) * (PX1 - PX0)
    Y = lambda v: PY1 - (v - lo) / (hi - lo) * (PY1 - PY0)
    xs = [X(t) for t, _, _ in pts] if len(pts) > 1 else [(PX0 + PX1) / 2]
    for i in range(3):
        v = lo + (hi - lo) * i / 2
        _dots(d, PX0, Y(v), PX1, Y(v), (150, 165, 200, 40), lw(1), k(2), k(5))
        d.text((PX1 + k(5), Y(v)), f"{v:.0f}c", font=M(8, False), fill=MUTED, anchor="lm")
    ey = Y(entry)
    _dots(d, PX0, ey, PX1, ey, AMBER + (200,), lw(1), k(5), k(4))
    _sp(d, (PX0 + k(4), ey - k(8)), f"ENTRY {entry:.0f}c", M(8), AMBER + (255,))
    if len(pts) > 1:
        d.polygon([(x, Y(v)) for x, v in zip(xs, fv)] + [(x, Y(v)) for x, v in zip(xs[::-1], pv[::-1])],
                  fill=ev_c + (45,))
        _glow(base, lambda gd: gd.line([(x, Y(v)) for x, v in zip(xs, pv)], fill=255, width=lw(3), joint="curve"),
              NEON_M, k(4), 0.8)
        d = ImageDraw.Draw(base, "RGBA")
        d.line([(x, Y(v)) for x, v in zip(xs, pv)], fill=NEON_M + (255,), width=lw(2), joint="curve")
        for i in range(len(xs) - 1):
            _dots(d, xs[i], Y(fv[i]), xs[i + 1], Y(fv[i + 1]), NEON_C + (230,), lw(1.4), k(4), k(3))
    step = max(1, len(xs) // 14)
    for i in list(range(0, len(xs), step)) + [len(xs) - 1]:
        d.ellipse([xs[i] - k(3), Y(pv[i]) - k(3), xs[i] + k(3), Y(pv[i]) + k(3)], fill=NEON_C + (255,))
    lx, py_, fy_ = xs[-1], Y(pv[-1]), Y(fv[-1])
    d.ellipse([lx - k(3), fy_ - k(3), lx + k(3), fy_ + k(3)], fill=NEON_C + (255,))
    if outcome:
        d.text(((PX0 + PX1) / 2, (PY0 + PY1) / 2), outcome.upper(), font=M(56), fill=accent + (80,), anchor="mm")

    # badges
    bwid = (360 - 20) / 3
    for bi, (lab, val, col) in enumerate((("EV", f"{p['ev'] * 100:+.1f}c", ev_c),
                                          ("ROI", f"{p['roi'] * 100:+.1f}%", ev_c),
                                          ("STAKE", f"{p['kelly'] * 100:.1f}%", NEON_C))):
        x0 = 44 + bi * (bwid + 10)
        d.rounded_rectangle([k(x0), k(364), k(x0 + bwid), k(394)], radius=k(8), fill=(255, 255, 255, 14),
                            outline=col + (170,), width=lw(1.5))
        _sp(d, (k(x0 + 10), k(373)), lab, M(8, False), MUTED, k(1))
        d.text((k(x0 + bwid - 10), k(384)), val, font=M(13), fill=col + (255,), anchor="rm")

    # odds cards
    cards = [(114, 164, (232, 48, 122), (74, 10, 58, 220), f"KALSHI {p['side']} ASK",
              f"{price * 100:.0f}c to enter", f"{1 / price:.2f}", NEON_G, (217, 184, 208, 255)),
             (172, 222, (34, 200, 224), (16, 42, 74, 220), f"{src.upper()[:20]} FAIR",
              f"{fair * 100:.1f}% win prob", f"{1 / fair:.2f}", NEON_C, (184, 208, 224, 255))]
    for y0, y1, oc, fc, lab, sub, val, vc, lc in cards:
        d.rounded_rectangle([k(444), k(y0), k(756), k(y1)], radius=k(8), fill=fc, outline=oc + (255,), width=lw(1.8))
        _sp(d, (k(456), k(y0 + 20)), lab, M(10, False), lc, k(1))
        d.text((k(456), k(y0 + 38)), sub, font=M(8, False), fill=MUTED, anchor="lm")
        d.text((k(744), k(y0 + 27)), val, font=M(26), fill=vc + (255,), anchor="rm")

    # edge block + gauge
    _sp(d, (k(452), k(282)), "EDGE", M(9, False), MUTED, k(1.5))
    d.text((k(452), k(312)), f"{edge:+.1f}", font=M(30), fill=ev_c + (255,), anchor="lm")
    _arrow(d, k(574), k(312), edge >= 0, k(11), ev_c + (255,))
    d.text((k(452), k(340)), f"EV {p['ev'] * 100:+.1f}c after fees", font=M(8, False), fill=MUTED, anchor="lm")
    gx, gy, gr = k(672), k(312), k(50)
    box = [gx - gr, gy - gr, gx + gr, gy + gr]
    d.ellipse(box, outline=(106, 90, 122, 150), width=lw(1.2))
    lo_q, hi_q = sorted((price, fair))
    _glow(base, lambda gd: gd.arc(box, ang(0), ang(fair), fill=255, width=lw(6)), NEON_G, k(4), 0.7)
    d = ImageDraw.Draw(base, "RGBA")
    d.arc(box, ang(0), ang(fair), fill=NEON_G + (255,), width=lw(5))
    if hi_q - lo_q > 0.003:
        d.arc(box, ang(lo_q), ang(hi_q), fill=ev_c + (255,), width=lw(9))
    pa = math.radians(-90 + 360 * price)
    ex, ey2 = gx + math.cos(pa) * gr, gy + math.sin(pa) * gr
    d.line([(gx, gy), (ex, ey2)], fill=NEON_P + (255,), width=lw(1.8))
    d.ellipse([ex - k(4), ey2 - k(4), ex + k(4), ey2 + k(4)], fill=NEON_P + (255,))
    d.ellipse([gx - k(4), gy - k(4), gx + k(4), gy + k(4)], fill=WHITE)
    d.text((gx + math.cos(pa) * (gr + k(16)), gy + math.sin(pa) * (gr + k(12))), f"{price * 100:.0f}c",
           font=M(9), fill=NEON_P + (255,), anchor="mm")
    d.text((gx, k(380)), f"{fair * 100:.1f}%", font=M(13), fill=NEON_G + (255,), anchor="mm")
    _sp(d, (gx, k(393)), "WIN PROB", M(8, False), (207, 207, 224, 255), k(1), "m")

    # buy bar
    tc = (10, 10, 20, 255) if p["side"] == "YES" else WHITE
    d.rounded_rectangle([k(44), k(404), k(756), k(446)], radius=k(14), fill=side_col + (238,),
                        outline=(accent + (255,)) if accent else None, width=lw(3))
    _arrow(d, k(72), k(425), p["side"] == "YES", k(11), tc)
    if p["side"] == "YES":
        bar = f"BET {p['team'].upper()[:26]} at {price * 100:.0f}c  |  {1 / price:.2f}x payout"
    else:
        bar = f"BUY NO at {price * 100:.0f}c  |  {1 / price:.2f}x payout"
    d.text((k(410), k(418)), bar, font=M(15), fill=tc, anchor="mm")
    d.text((k(410), k(435)), p["ticker"][-26:], font=M(8, False), fill=tc, anchor="mm")

    frames = []
    for f in range(n):
        t = f / n if n > 1 else 0.25
        sn = math.sin(2 * math.pi * t)
        im = base.copy()
        fd = ImageDraw.Draw(im, "RGBA")
        r = k(5 + 1.8 * sn)
        if outcome is None:
            fd.ellipse([k(746) - r, k(52) - r, k(746) + r, k(52) + r], fill=NEON_P + (int(150 + 90 * sn),))
        fd.ellipse([lx - r * 1.6, py_ - r * 1.6, lx + r * 1.6, py_ + r * 1.6], outline=NEON_C + (200,), width=lw(1.5))
        fd.ellipse([ex - r * 1.3, ey2 - r * 1.3, ex + r * 1.3, ey2 + r * 1.3], outline=NEON_P + (200,), width=lw(1.5))
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
    if p["side"] == "YES":
        pick_line = f"**Pick:** Bet on **{p['team']}** (Buy YES at {p['price'] * 100:.0f}c)"
    else:   # only 3-way markets (soccer) and old open picks can still land here
        pick_line = f"**Pick:** Buy **NO** at {p['price'] * 100:.0f}c ({p['team']} not to win)"
    lines = [f"**Game:** {p['game']}",
             pick_line,
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
        title = f"🆙 PICK: {p['team']}" if p["side"] == "YES" else f"⬇️ PICK: {p['team']} NOT to win"
        color = GREEN
        if p.get("lowconf"):
            title, color = "⚠️ LOW CONFIDENCE " + title, GREY
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


def post_status(state, best, posted):
    rec, pnl, avg, last, calib = stats(state["history"])
    lean_txt = (f"Best lean: {best['team']} {best['side']} at {best['price'] * 100:.0f}c "
                f"(EV {best['ev']:+.3f})") if best else "No positive-EV market right now."
    extra = f"\nPosted a low-confidence pick: {posted['team']} {posted['side']}" if posted else ""
    if not ODDS_KEY or API_DOWN[0]:
        odds_txt = "Pricing: Kalshi only (MoneyLine off or out of credits)"
    else:
        odds_txt = (f"Pricing: MoneyLine fair values for {LOADED[0]} teams | "
                    f"credits left {BUDGET.get('remaining', '?')}")
    embed = {"title": "🕐 Status",
             "description": f"Open picks: {len(state['open'])}\n{odds_txt}\n"
                            f"Arbs found: {state['arb_count']}\n{lean_txt}{extra}",
             "color": GREY,
             "fields": [{"name": "Record (win %)", "value": rec, "inline": True},
                        {"name": "Last 20", "value": last, "inline": True},
                        {"name": "Calibration", "value": calib, "inline": True}],
             "footer": {"text": "Signals only, not financial advice"},
             "timestamp": datetime.now(timezone.utc).isoformat()}
    send(embed)


def post_watch(top):
    """Hourly top-N watchlist. Positive EV after fees is highlighted; the rest are labeled no edge."""
    edges = sum(1 for l in top if l["ev"] > 0)
    rows = []
    for i, l in enumerate(top, 1):
        tag = "🟢 **EDGE**" if l["ev"] > 0 else "⚪ no edge"
        rows.append(f"**{i}.** {tag} | {l['game']}\n"
                    f"Lean **{l['side']}** {l['team']} at {l['price'] * 100:.0f}c | "
                    f"EV {l['ev']:+.3f} after fees | {l['src']}")
    head = (f"{edges} of {len(top)} show a positive edge after fees." if edges
            else "None show an edge after fees. These are only the closest markets.")
    embed = {"title": f"📋 Hourly top {len(top)} (leans, not bets)",
             "description": head + "\n\n" + "\n\n".join(rows),
             "color": GREEN if edges else GREY,
             "footer": {"text": "Leans are not tracked in the record | Signals only, not financial advice"},
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
        m_ = 0.0 if old.get("lowconf") else margin
        p_ = (fair if yes else 1 - fair) - m_
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
    picks, relaxed, marks, arbs, leans = scan()
    now = time.time()
    refresh_open(state, marks, now)

    sent = 0
    for a in sorted(arbs, key=lambda x: -x["profit"]):
        if sent >= 3 or now - state["arbs"].get(a["event"], 0) < ARB_REPEAT:
            continue
        first_time = a["event"] not in state["arbs"]   # count profit once per game per 24h
        try:
            post_arb(a, state)
            state["arbs"][a["event"]] = now
            if first_time:
                state["arb_count"] += 1
                state["arb_locked"] += a["profit"]
            sent += 1
        except Exception as e:
            print("arb send error", repr(e), flush=True)
    state["arbs"] = {k_: v for k_, v in state["arbs"].items() if now - v < 86400}

    # new picks: max TOP_N per scan, one pick per game, never the opposite side or the other team
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
        try:
            post(p, state)
        except Exception as e:
            print("pick send error", repr(e), flush=True)
            continue
        state["open"][t] = p
        locked.add(p["event"])
        fresh += 1

    # pick floor: if no pick in PICK_FLOOR seconds, post the best low-confidence one
    if fresh:
        state["last_pick"] = now
    posted = None
    if now - state.get("last_pick", 0) >= PICK_FLOOR:
        for p in relaxed:
            t = p["ticker"]
            if t in state["open"] or t in done or p["event"] in locked or len(state["open"]) >= MAX_OPEN:
                continue
            p["pts"] = [[round(now), round(p["price"], 4), round(p["p"], 4)]]
            p["entry_price"], p["last_edit"] = p["price"], now
            p["p0"], p["ev0"] = p["p"], p["ev"]
            try:
                post(p, state)
            except Exception as e:
                print("low-confidence send error", repr(e), flush=True)
                break
            state["open"][t] = p
            locked.add(p["event"])
            state["last_pick"] = now
            posted = p
            break

    # hourly watchlist: top WATCH_N games closest to an edge, best first
    if now - state.get("last_watch", 0) >= WATCH_EVERY:
        seen, top = set(), []
        for l in sorted(leans, key=lambda x: -x["ev"]):
            if l["event"] in seen or l["event"] in locked:
                continue
            seen.add(l["event"])
            top.append(l)
            if len(top) >= WATCH_N:
                break
        if top:
            try:
                post_watch(top)
                state["last_watch"] = now
            except Exception as e:
                print("watchlist send error", repr(e), flush=True)

    if now - state.get("last_status", 0) >= STATUS_EVERY:
        try:
            post_status(state, relaxed[0] if relaxed else None, posted)
            state["last_status"] = now
        except Exception as e:
            print("status send error", repr(e), flush=True)
    state["budget"] = dict(BUDGET)
    save_state(state)


def main():
    state = load_state()
    BUDGET.update(state.get("budget", {}))
    if not ODDS_KEY:
        print("No MONEYLINE_API_KEY set: Kalshi-only mode.", flush=True)
    elif not state.get("injury_probed_v2"):
        injury_probe()
        state["injury_probed_v2"] = True
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
