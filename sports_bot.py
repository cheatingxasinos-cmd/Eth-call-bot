import functools
import io
import json
import math
import os
import re
import time
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from PIL import Image, ImageDraw, ImageFont

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
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

COLLEGE_EXTRA = (
    "North Dakota=Fighting Hawks;Central Arkansas=Bears;Florida A&M=Rattlers;UT Arlington=Mavericks;"
    "Texas A&M-Corpus Christi=Islanders;Portland=Pilots;Tennessee-Martin=Skyhawks;"
    "Southern University=Jaguars;Western Illinois=Leathernecks;Valparaiso=Beacons;"
    "Bethune-Cookman=Wildcats;Lehigh=Mountain Hawks;Siena=Saints;Bryant=Bulldogs;Binghamton=Bearcats;"
    "The Citadel=Bulldogs;West Georgia=Wolves;Kansas City=Roos;Campbell=Fighting Camels;"
    "Southeast Missouri State=Redhawks;Morehead State=Eagles;Saint Peter's=Peacocks;"
    "Southern Indiana=Screaming Eagles;Louisiana Tech=Bulldogs;Louisiana-Monroe=Warhawks;"
    "Denver=Pioneers;Canisius=Golden Griffins;Tennessee Tech=Golden Eagles;Mercyhurst=Lakers;"
    "Eastern Kentucky=Colonels;St. Thomas=Tommies;Presbyterian=Blue Hose;Howard=Bison;"
    "Northeastern=Huskies;Boston University=Terriers;Little Rock=Trojans;Vermont=Catamounts;"
    "Iona=Gaels;Elon=Phoenix;Marist=Red Foxes;UMass Lowell=River Hawks;Western Carolina=Catamounts;"
    "Wright State=Raiders;USC Upstate=Spartans;New Hampshire=Wildcats;Charleston Southern=Buccaneers;"
    "UNC Wilmington=Seahawks;NJIT=Highlanders;Florida Gulf Coast=Eagles;American=Eagles;"
    "St. Bonaventure=Bonnies;UNC Asheville=Bulldogs;Mount St. Mary's=Mountaineers;Lafayette=Leopards;"
    "New Haven=Chargers;Monmouth=Hawks;Loyola Maryland=Greyhounds;UAB=Blazers;McNeese=Cowboys;"
    "UMBC=Retrievers;Northern Kentucky=Norse;Tennessee State=Tigers;Winthrop=Eagles;"
    "Queens University=Royals;Merrimack=Warriors;Grambling State=Tigers;High Point=Panthers")

COLLEGE_EXTRA += ";" + (
    "Sacramento State=Hornets;Central Connecticut State=Blue Devils;Pacific=Tigers;"
    "Bethesda=Flames;Cal State Fullerton=Titans;Northern Arizona=Lumberjacks;"
    "North Florida=Ospreys;California Baptist=Lancers;Arkansas-Pine Bluff=Golden Lions;"
    "Lipscomb=Bisons;Alabama State=Hornets;Cleveland State=Vikings;New Orleans=Privateers;"
    "Wofford=Terriers;South Dakota State=Jackrabbits;Drexel=Dragons;"
    "Purdue Fort Wayne=Mastodons;Stetson=Hatters;Holy Cross=Crusaders;Hampton=Pirates;"
    "Coppin State=Eagles;Wagner=Seahawks;Manhattan=Jaspers;Seattle=Redhawks;"
    "Milwaukee=Panthers;Southern Utah=Thunderbirds;Radford=Highlanders;"
    "UC Riverside=Highlanders;North Alabama=Lions;UC Davis=Aggies;"
    "Eastern Washington=Eagles;East Tennessee State=Buccaneers;Colgate=Raiders;"
    "Weber State=Wildcats;Northern Colorado=Bears;Colorado College=Tigers;"
    "Idaho State=Bengals;Northwestern State=Demons;Fresno Pacific=Sunbirds;"
    "Utah Tech=Trailblazers;Long Beach State=Beach;Abilene Christian=Wildcats;"
    "University of North Texas at Dallas=Trailblazers")

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
COLLEGE_ALIASES.update({"Florida AM": "Florida A&M", "University of North Texas": "North Texas",
                        "UNT": "North Texas"})


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
    schools = dict(x.split("=", 1) for x in (COLLEGE + ";" + COLLEGE_EXTRA).split(";"))
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


def full_name(short, league):
    """Full team name: city+nickname for pro teams, school+mascot for colleges. Tennis keeps player names."""
    short = (short or "").strip()
    if not short or _norm(short) in ("tie", "draw") or league not in NAME_MAP:
        return short
    hit = NAME_MAP.get(league, {}).get(_norm(short))
    if hit:
        return hit
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


# ---------- settings ----------
POLL = float(os.getenv("POLL_SECONDS", "15"))
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.10"))
PMIN, PMAX = float(os.getenv("PMIN", "0.15")), float(os.getenv("PMAX", "0.92"))
MIN_WIN = float(os.getenv("MIN_WIN", "0.40"))             # minimum win probability for any call
HOURS_AHEAD = float(os.getenv("HOURS_AHEAD", "6"))        # max hours until a game starts (live games always count)
# typical game length in hours, used to estimate start = expected end - length
GAME_HOURS = {"NBA": 2.5, "NFL": 3.5, "NHL": 2.75, "NCAAF": 3.5, "NCAAM": 2.25,
              "EPL": 2.0, "MLS": 2.0, "ATP": 3.0, "WTA": 2.5}
TOP_N = int(os.getenv("TOP_N", "3"))                      # max new picks per scan
MAX_OPEN = int(os.getenv("MAX_OPEN", "25"))               # max open picks overall
STATUS_EVERY = float(os.getenv("STATUS_SECONDS", "3600"))   # status update interval
WATCH_EVERY = float(os.getenv("WATCH_SECONDS", "3600"))     # hourly watchlist interval
WATCH_N = int(os.getenv("WATCH_N", "3"))                    # games on the watchlist
ARB_MIN = float(os.getenv("ARB_MIN", "0.01"))          # min locked profit per set, in dollars
ARB_REPEAT = float(os.getenv("ARB_REPEAT", "1800"))    # seconds before re-alerting the same game
RUN_SECONDS = int(os.getenv("RUN_SECONDS", str(345 * 60)))
STATE_FILE = os.getenv("STATE_FILE", "sports_state.json")
EDIT_SECONDS = float(os.getenv("EDIT_SECONDS", "120"))
LIVE_SCALE = float(os.getenv("LIVE_SCALE", "1.0"))
FINAL_SCALE = float(os.getenv("FINAL_SCALE", "2"))
MAX_BYTES = int(float(os.getenv("MAX_MB", "7.5")) * 1024 * 1024)
GREEN, RED, GREY = 0x00FF00, 0xFF3B30, 0x808080
EMPTY_SERIES = set()


# ---------- http ----------
def _session(retries):
    s = requests.Session()
    ad = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=retries)
    s.mount("https://", ad)
    s.headers["User-Agent"] = "sports-bot/1.0"
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


# ---------- pick scoring ----------
def evaluate(m, p_yes):
    """Call YES on a team if it is likely enough to win (>= MIN_WIN). No EV or fee requirement.
    'growth' is just the win probability, used for sorting."""
    price = dollars(m, "yes_ask")
    if price is None or not (PMIN <= price <= PMAX) or p_yes < MIN_WIN:
        return None
    c = price + fee(price)
    return {"side": "YES", "p": p_yes, "price": price, "ev": p_yes - c, "roi": (p_yes - c) / price,
            "edge": p_yes - price, "kelly": 0.0, "growth": p_yes}


# ---------- game window ----------
def in_window(m, now, league=None):
    """True if the game is live (not over yet) or starts within HOURS_AHEAD hours."""
    try:
        end = ts(m.get("expected_expiration_time") or m["close_time"])
    except Exception:
        return False
    if end <= now:
        return False
    start = end - GAME_HOURS.get(league, 3.0) * 3600
    return start - now <= HOURS_AHEAD * 3600


# ---------- arbitrage (keyless) ----------
def find_arb(ms, now, league=None):
    """Mutually exclusive, exhaustive outcomes of one game that can be locked for profit after fees."""
    n = len(ms)
    if n < 2 or n > 3 or not all(in_window(m, now, league) for m in ms):
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


def scan():
    """Returns (picks, marks, arbs, leans).
    marks[ticker] = (win_prob, yes_ask) for live updates of open picks."""
    picks, marks, arbs, leans, now = [], {}, [], [], time.time()
    st = {"markets": 0, "no_quote": 0, "wide": 0, "no_call": 0}
    for series in SERIES:
        try:
            mkts = kalshi_markets(series)
        except Exception as e:
            print("scan error", series, repr(e), flush=True)
            continue
        if not mkts and series not in EMPTY_SERIES:
            EMPTY_SERIES.add(series)
            print(f"no open markets for series {series}: check the ticker or it may be off-season", flush=True)
        league = SPORT.get(series, series)
        events = {}
        for m in mkts:
            events.setdefault(m.get("event_ticker") or m["ticker"], []).append(m)
        for ev_key, ms in events.items():
            arb = find_arb(ms, now, league)
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
            for m in ms:
                if not in_window(m, now, league):
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
                win = mid / tot if tot else mid          # win probability from Kalshi prices
                marks[m["ticker"]] = (win, a)
                label = full_name(m.get("yes_sub_title"), league) if m.get("yes_sub_title") else m["ticker"]
                title = full_game((m.get("title") or "").replace(" Winner?", ""), league)
                game = f"{league} | {title}" if title else f"{league} | {m['ticker']}"
                price = a
                if win >= MIN_WIN and PMIN <= price <= PMAX:
                    leans.append({"side": "YES", "price": price, "p": win, "team": label, "game": game,
                                  "event": ev_key, "src": "Kalshi"})
                r = evaluate(m, win)
                if r:
                    picks.append({**r, "ticker": m["ticker"], "series": series, "event": ev_key,
                                  "team": label, "title": title, "game": game, "commence": end,
                                  "p_yes": win, "books": len(ms), "src": "Kalshi"})
                else:
                    st["no_call"] += 1
    if now - LAST_LOG[0] >= 120:
        LAST_LOG[0] = now
        print(f"scan: {st['markets']} markets | no quote {st['no_quote']} | wide {st['wide']} | "
              f"no call {st['no_call']} | CALLS {len(picks)} | ARBS {len(arbs)}", flush=True)
    return sorted(picks, key=lambda x: -x["growth"]), marks, arbs, leans


# ---------- chart ----------
CW, CH = 800, 480
WHITE, MUTED = (255, 255, 255, 255), (160, 168, 178, 255)
C_UP, C_DN = (0, 217, 142), (255, 87, 87)
AMBER = (255, 190, 40)
NEON_C, NEON_P, NEON_G = (34, 229, 240), (255, 61, 139), (34, 229, 138)


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
    lw = lambda v: max(1, int(k(v)))
    M = lambda sz, b=True: _mono(max(6, int(k(sz))), b)
    W, H = int(CW * S), int(CH * S)
    accent = {"win": C_UP, "loss": C_DN}.get(outcome, NEON_C)
    price = p["price"]
    pts = p.get("pts") or [[time.time(), price, p["p"]]]
    t0 = pts[0][0]
    t1 = max(pts[-1][0], t0 + 120)

    base = Image.new("RGBA", (W, H), (18, 20, 28, 255))
    d = ImageDraw.Draw(base, "RGBA")

    # header
    d.text((k(32), k(38)), p["team"].upper()[:34], font=M(22), fill=WHITE, anchor="lm")
    d.text((k(32), k(68)), p["game"][:72], font=M(11, False), fill=MUTED, anchor="lm")
    d.text((k(768), k(38)), outcome.upper() if outcome else "LIVE", font=M(18),
           fill=accent + (255,), anchor="rm")
    if outcome is None:
        d.text((k(768), k(68)), "CLOSES " + _hhmm(p["commence"] - time.time()),
               font=M(11, False), fill=MUTED, anchor="rm")

    # price chart
    X0, X1, Y0, Y1 = k(70), k(700), k(110), k(335)
    pv = [a * 100 for _, a, _ in pts]
    entry = (p.get("entry_price") or price) * 100
    lo, hi = min(pv + [entry]), max(pv + [entry])
    if hi - lo < 6:
        mid = (hi + lo) / 2
        lo, hi = mid - 3, mid + 3
    pad = (hi - lo) * 0.15
    lo, hi = lo - pad, hi + pad
    X = lambda t: X0 + (t - t0) / (t1 - t0) * (X1 - X0)
    Y = lambda v: Y1 - (v - lo) / (hi - lo) * (Y1 - Y0)
    for i in range(4):
        v = lo + (hi - lo) * i / 3
        d.line([(X0, Y(v)), (X1, Y(v))], fill=(255, 255, 255, 22), width=lw(1))
        d.text((X0 - k(8), Y(v)), f"{v:.0f}c", font=M(10, False), fill=MUTED, anchor="rm")
    ey = Y(entry)
    _dots(d, X0, ey, X1, ey, AMBER + (230,), lw(1.5), k(6), k(4))
    d.text((X1 + k(8), ey), f"ENTRY {entry:.0f}c", font=M(9), fill=AMBER + (255,), anchor="lm")
    xs = [X(t) for t, _, _ in pts] if len(pts) > 1 else [X1]
    line = [(x, Y(v)) for x, v in zip(xs, pv)]
    if len(line) > 1:
        d.polygon(line + [(xs[-1], Y1), (xs[0], Y1)], fill=accent + (35,))
        d.line(line, fill=accent + (255,), width=lw(3), joint="curve")
    lx, ly = line[-1]
    d.text((X1 + k(8), ly + (k(14) if abs(ly - ey) < k(14) else 0)), f"{price * 100:.0f}c",
           font=M(16), fill=WHITE, anchor="lm")

    # bet bar
    d.rounded_rectangle([k(32), k(362), k(768), k(432)], radius=k(12), fill=NEON_G + (240,))
    d.text((k(400), k(388)), f"BET {p['team'].upper()[:28]} at {price * 100:.0f}c",
           font=M(20), fill=(10, 10, 20, 255), anchor="mm")
    d.text((k(400), k(414)), f"{1 / price:.2f}x payout  |  {p['p'] * 100:.0f}% win prob",
           font=M(11, False), fill=(10, 10, 20, 255), anchor="mm")

    frames = []
    for f in range(n):
        t = f / n if n > 1 else 0.25
        r = k(5 + 2 * math.sin(2 * math.pi * t))
        im = base.copy()
        fd = ImageDraw.Draw(im, "RGBA")
        fd.ellipse([lx - r, ly - r, lx + r, ly + r], fill=accent + (255,))
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
    """Overall record + win %, last 20, and P&L in units."""
    n = len(h)
    if not n:
        return "n/a", "n/a", "n/a"
    w = sum(1 for x in h if x["win"])
    last = h[-20:]
    lw_ = sum(1 for x in last if x["win"])
    pnl = sum(x["pnl"] for x in h)
    stake = sum(x["price"] for x in h)
    return (f"{w}W-{n - w}L ({w / n * 100:.0f}%)",
            f"{pnl:+.2f}u ({pnl / stake * 100:+.1f}% ROI)",
            f"{lw_}W-{len(last) - lw_}L ({lw_ / len(last) * 100:.0f}%)")


def build_embed(p, state, outcome=None):
    rec, pnl, last = stats(state["history"])
    lines = [f"**Game:** {p['game']}",
             f"**Pick:** {p['team']} to win",
             f"**Price:** {p['price'] * 100:.0f}c | **Payout:** {1 / p['price']:.2f}x",
             f"**Win prob:** {p['p'] * 100:.0f}%",
             f"**Closes:** <t:{int(p['commence'])}:R>"]
    if outcome in ("win", "loss"):
        title = f"{'✅ WIN' if outcome == 'win' else '❌ LOSS'} | {p['game']}"
        color = GREEN if outcome == "win" else RED
        lines.insert(0, f"**Result:** {p['pnl']:+.2f}u")
    else:
        title = f"PICK: {p['team']}"
        color = GREEN
    return {"title": title[:250], "description": "\n".join(lines), "color": color,
            "fields": [{"name": "Record", "value": rec, "inline": True},
                       {"name": "Last 20", "value": last, "inline": True},
                       {"name": "P&L", "value": pnl, "inline": True}],
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


def post_status(state, leans):
    rec, pnl, last = stats(state["history"])
    best = max(leans, key=lambda x: x["p"]) if leans else None
    lean_txt = (f"Top call right now: {best['team']} at {best['price'] * 100:.0f}c "
                f"({best['p'] * 100:.0f}% win prob)" if best else "No qualifying game right now.")
    embed = {"title": "🕐 Status",
             "description": f"Open picks: {len(state['open'])}\nArbs found: {state['arb_count']}\n{lean_txt}",
             "color": GREY,
             "fields": [{"name": "Record", "value": rec, "inline": True},
                        {"name": "Last 20", "value": last, "inline": True},
                        {"name": "P&L", "value": pnl, "inline": True}],
             "footer": {"text": "Signals only, not financial advice"},
             "timestamp": datetime.now(timezone.utc).isoformat()}
    send(embed)


def post_watch(top):
    rows = [f"**{i}.** {l['game']}\n**{l['team']}** at {l['price'] * 100:.0f}c | "
            f"{l['p'] * 100:.0f}% win prob" for i, l in enumerate(top, 1)]
    embed = {"title": f"📋 Hourly top {len(top)}",
             "description": "\n\n".join(rows), "color": GREY,
             "footer": {"text": "Not tracked in the record | Signals only, not financial advice"},
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
        p["win"] = res == "yes"
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
    """Update open picks (live price and win probability) and re-post the chart."""
    for t, old in state["open"].items():
        if now >= old["commence"] or t not in marks:
            continue
        win, price = marks[t]
        if not (0 < price < 1):
            continue
        c = price + fee(price)
        old.update({"price": price, "p": win, "ev": win - c, "roi": (win - c) / price, "edge": win - price})
        old["pts"] = (old.get("pts", []) + [[round(now), round(price, 4), round(win, 4)]])[-600:]
        if now - old.get("last_edit", 0) >= EDIT_SECONDS:
            try:
                post(old, state)
                old["last_edit"] = now
            except Exception as e:
                print("edit error", repr(e), flush=True)


def tick(state):
    settle(state)
    picks, marks, arbs, leans = scan()
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

    # new picks: best win probability first, max TOP_N per scan, one pick per game
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

    # hourly watchlist: top WATCH_N games by win probability
    if now - state.get("last_watch", 0) >= WATCH_EVERY:
        seen, top = set(), []
        for l in sorted(leans, key=lambda x: -x["p"]):
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
            post_status(state, leans)
            state["last_status"] = now
        except Exception as e:
            print("status send error", repr(e), flush=True)
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
