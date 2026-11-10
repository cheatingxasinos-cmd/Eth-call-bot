import colorsys
import functools
import hashlib
import io
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from PIL import Image, ImageDraw, ImageFont

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"

# Kalshi series scanned every tick (free, no key). Add KXMLBGAME here for baseball.
SERIES = [s.strip() for s in os.getenv(
    "SERIES",
    "KXNBAGAME,KXNFLGAME,KXNHLGAME,KXNCAAFGAME,KXNCAAMBGAME,KXEPLGAME,KXMLSGAME,KXATPMATCH,KXWTAMATCH"
).split(",") if s.strip()]

# League label shown next to team names so "Atlanta" or "Ohio" is never ambiguous.
SPORT = {"KXNBAGAME": "NBA", "KXNFLGAME": "NFL", "KXNHLGAME": "NHL", "KXNCAAFGAME": "NCAAF",
         "KXNCAAMBGAME": "NCAAM", "KXEPLGAME": "EPL", "KXMLSGAME": "MLS", "KXATPMATCH": "ATP",
         "KXWTAMATCH": "WTA", "KXMLBGAME": "MLB"}

# ---------- full team names (city + nickname, school + mascot) ----------
TEAMS_FILE = os.getenv("TEAMS_FILE", "team_names.json")  # optional: {"NCAAM": {"Ohio": "Ohio Bobcats"}}


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
    "Alabama A&M=Bulldogs;"
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
# schools from the "no full name" log warnings
COLLEGE_EXTRA += ";" + (
    "San Diego=Toreros;Lamar=Cardinals;Idaho=Vandals;UT Rio Grande Valley=Vaqueros;"
    "Houston Christian=Huskies;Austin Peay=Governors;Tarleton State=Texans;Incarnate Word=Cardinals;"
    "East Texas A&M=Lions;Texas Southern=Tigers;Portland State=Vikings;Cal Poly=Mustangs;"
    "North Dakota State=Bison;Samford=Bulldogs;Mercer=Bears;Jackson State=Tigers;Alcorn State=Braves;"
    "Prairie View A&M=Panthers;Lindenwood=Lions;Mississippi Valley State=Delta Devils;"
    "West Florida=Argonauts;North Carolina Central=Eagles;Morgan State=Bears;Southern Illinois=Salukis;"
    "Chicago State=Cougars;Montana State=Bobcats;Montana=Grizzlies;Gardner-Webb=Runnin' Bulldogs;"
    "South Carolina State=Bulldogs;Delaware State=Hornets;Bucknell=Bison;Maine=Black Bears;"
    "Eastern Illinois=Panthers;South Dakota=Coyotes;Missouri State=Bears;William & Mary=Tribe;"
    "Stonehill=Skyhawks;Robert Morris=Colonials;LIU=Sharks;Sacred Heart=Pioneers;Stony Brook=Seawolves;"
    "North Carolina A&T=Aggies;University at Albany=Great Danes;VMI=Keydets;Norfolk State=Spartans;"
    "Southeastern Louisiana=Lions;Nicholls State=Colonels;Stephen F. Austin=Lumberjacks;"
    "Youngstown State=Penguins;Omaha=Mavericks;SIU Edwardsville=Cougars;FDU=Knights;Hofstra=Pride;"
    "Loyola Marymount=Lions;Quinnipiac=Bobcats;Niagara=Purple Eagles;Cal State Bakersfield=Roadrunners;"
    "Maryland-Eastern Shore=Hawks;Oakland=Golden Grizzlies;UC Santa Barbara=Gauchos")

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
                        "UNT": "North Texas", "Albany": "University at Albany"})


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
MIN_WIN = float(os.getenv("MIN_WIN", "0.40"))  # minimum win probability for any call
HOURS_AHEAD = float(os.getenv("HOURS_AHEAD", "6"))  # max hours until a game starts (live games always count)
# typical game length in hours, used to estimate start = expected end - length
GAME_HOURS = {"NBA": 2.5, "NFL": 3.5, "NHL": 2.75, "NCAAF": 3.5, "NCAAM": 2.25, "EPL": 2.0, "MLS": 2.0,
              "ATP": 3.0, "WTA": 2.5, "MLB": 3.0}
TOP_N = int(os.getenv("TOP_N", "3"))  # max new picks per scan
MAX_OPEN = int(os.getenv("MAX_OPEN", "25"))  # max open picks overall
STATUS_EVERY = float(os.getenv("STATUS_SECONDS", "3600"))  # status update interval
WATCH_EVERY = float(os.getenv("WATCH_SECONDS", "3600"))  # hourly watchlist interval
WATCH_N = int(os.getenv("WATCH_N", "3"))  # games on the watchlist
ARB_MIN = float(os.getenv("ARB_MIN", "0.01"))  # min locked profit per set, in dollars
ARB_REPEAT = float(os.getenv("ARB_REPEAT", "1800"))  # seconds before re-alerting the same game
RUN_SECONDS = int(os.getenv("RUN_SECONDS", str(345 * 60)))
STATE_FILE = os.getenv("STATE_FILE", "sports_state.json")
EDIT_SECONDS = float(os.getenv("EDIT_SECONDS", "120"))
LIVE_SCALE = float(os.getenv("LIVE_SCALE", "1.0"))
FINAL_SCALE = float(os.getenv("FINAL_SCALE", "1.5"))
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
    return get_json(f"{KALSHI}/markets", {"series_ticker": series, "status": "open", "limit": 1000}).get("markets", [])


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


# ---------- injury report (Yahoo Sports page first, ESPN feed as backup; no keys) ----------
INJ_SOURCE = os.getenv("INJURY_SOURCE", "yahoo").lower()  # "yahoo" or "espn" (which one goes first)
YAHOO_SLUG = {"NFL": "nfl", "NBA": "nba", "NHL": "nhl", "MLB": "mlb",
              "NCAAF": "college-football", "NCAAM": "college-basketball"}
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")
YAHOO_HEADERS = {"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml",
                 "Accept-Language": "en-US,en;q=0.9"}
ESPN_HOSTS = ("https://site.api.espn.com/apis/site/v2/sports", "https://site.web.api.espn.com/apis/site/v2/sports")
ESPN_HEADERS = {"User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9", "Origin": "https://www.espn.com",
                "Referer": "https://www.espn.com/"}
INJ_PATH = {"NFL": "football/nfl", "NBA": "basketball/nba", "NHL": "hockey/nhl", "MLB": "baseball/mlb",
            "NCAAF": "football/college-football", "NCAAM": "basketball/mens-college-basketball",
            "EPL": "soccer/eng.1", "MLS": "soccer/usa.1"}
INJ_TTL = float(os.getenv("INJURY_TTL", "600"))  # seconds between refreshes per league
INJ_FAIL_TTL = float(os.getenv("INJURY_FAIL_TTL", "1800"))  # wait after a failure before retrying
INJ_CACHE = {}  # league -> (fetched_at, {team_key: [players]} or None)
INJ_SRC = {}  # league -> "Yahoo Sports" / "ESPN"
INJ_WARNED = set()


def _sev(status):
    """3 = out/IR/suspended, 2 = doubtful, 1 = questionable/day-to-day, 0 = probable."""
    s = (status or "").lower()
    for key, v in (("out", 3), ("injured reserve", 3), ("suspend", 3), ("doubtful", 2),
                   ("questionable", 1), ("day", 1), ("probable", 0)):
        if key in s:
            return v
    return 1


def _st(status):
    s = (status or "").lower()
    if "injured reserve" in s:
        return "IR"
    for a, b in (("out", "OUT"), ("suspend", "SUSP"), ("doubtful", "DOUBT"), ("questionable", "QUES"),
                 ("day", "DTD"), ("probable", "PROB")):
        if a in s:
            return b
    return (s[:5].upper() or "?")


# ----- Yahoo: read the public injuries page -----
def _yahoo_team(text, fulls):
    """Heading text -> normalized full team name (accepts 'Boston Celtics', 'Celtics', 'LA Clippers')."""
    if len(text) > 40:
        return None
    n = _norm(text)
    if n.startswith("la "):
        n = "los angeles " + n[3:]
    for nf in fulls:
        if n == nf or (len(n) >= 4 and nf.endswith(" " + n)):
            return nf
    return None


class _InjParse(HTMLParser):
    """Walks the page: remembers the latest team heading, reads every table row under it."""

    def __init__(self, fulls):
        super().__init__(convert_charrefs=True)
        self.fulls, self.team = fulls, None
        self.in_table, self.row, self.cell = False, None, None
        self.cell_tag, self.headers, self.out = "td", None, {}

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.in_table, self.headers = True, None
        elif tag == "tr" and self.in_table:
            self.row = []
        elif tag in ("td", "th") and self.in_table:
            self.cell, self.cell_tag = [], tag

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append((" ".join("".join(self.cell).split()), self.cell_tag))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self._finish_row()
            self.row = None
        elif tag == "table":
            self.in_table = False

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)
            return
        t = " ".join(data.split())
        if t:
            k_ = _yahoo_team(t, self.fulls)
            if k_:
                self.team = k_

    def _finish_row(self):
        cells = [c for c, _ in self.row]
        if len(cells) < 2:
            return
        if all(t == "th" for _, t in self.row):
            self.headers = [c.lower() for c in cells]
            return
        if self.team is None:
            return
        h = self.headers or []

        def col(names, default=""):
            for i, nm in enumerate(h):
                if any(x in nm for x in names) and i < len(cells):
                    return cells[i]
            return default

        name = col(("player", "name"), cells[0])
        if not name:
            return
        self.out.setdefault(self.team, []).append({
            "name": name[:40],
            "pos": col(("pos",), cells[1] if len(cells) > 2 else "")[:6],
            "status": col(("status",), cells[2] if len(cells) > 2 else "") or "Unknown",
            "detail": col(("injury", "comment", "detail", "note"), cells[3] if len(cells) > 3 else "")[:40]})


def _inj_yahoo(league):
    slug = YAHOO_SLUG.get(league)
    fulls = sorted({_norm(v) for v in NAME_MAP.get(league, {}).values()})
    if not slug or not fulls:
        return None
    r = DATA.get(f"https://sports.yahoo.com/{slug}/injuries/", headers=YAHOO_HEADERS, timeout=(3.05, 12))
    r.raise_for_status()
    ps = _InjParse(fulls)
    ps.feed(r.text)
    out = ps.out
    if out and len(out) >= max(3, len(fulls) // 3):  # healthy parse: teams not listed have no injuries
        for nf in fulls:
            out.setdefault(nf, [])
    return out or None


# ----- ESPN: public JSON feed (backup) -----
def _espn_get(path):
    last = None
    for host in ESPN_HOSTS:
        try:
            r = DATA.get(f"{host}/{path}/injuries", headers=ESPN_HEADERS, timeout=(3.05, 10))
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
    raise last


def _inj_espn(league):
    path = INJ_PATH.get(league)
    if not path:
        return None
    data = {}
    for t in _espn_get(path).get("injuries", []):
        items = []
        for i in t.get("injuries", []):
            a = i.get("athlete") or {}
            det = i.get("details") or {}
            kind = det.get("type") or det.get("detail") or ""
            items.append({
                "name": a.get("displayName") or a.get("shortName") or "?",
                "pos": (a.get("position") or {}).get("abbreviation") or "",
                "status": i.get("status") or (i.get("type") or {}).get("description") or "Unknown",
                "detail": " ".join(x for x in (det.get("side"), kind) if x and x != "Not Specified")})
        data[_norm(t.get("displayName", ""))] = items
    return data or None


def _inj_league(league):
    now = time.time()
    hit = INJ_CACHE.get(league)
    if hit and now - hit[0] < (INJ_TTL if hit[1] is not None else INJ_FAIL_TTL):
        return hit[1]
    order = ((_inj_yahoo, "Yahoo Sports"), (_inj_espn, "ESPN"))
    if INJ_SOURCE == "espn":
        order = order[::-1]
    data = None
    for fn, label in order:
        try:
            data = fn(league)
        except Exception as e:
            if (label, league) not in INJ_WARNED:  # log once, not every tick
                INJ_WARNED.add((label, league))
                print(f"injury source {label} failed for {league}: {repr(e)}", flush=True)
            data = None
        if data:
            INJ_SRC[league] = label
            break
        if (label, league) not in INJ_WARNED:
            INJ_WARNED.add((label, league))
            print(f"injury source {label} returned nothing for {league}", flush=True)
    INJ_CACHE[league] = (now, data)
    return data


def injuries_for(team, league):
    """Injured players for a team, worst first. None = no data for this league/team."""
    data = _inj_league(league)
    if data is None:
        return None
    n = _norm(team)
    items = data.get(n)
    if items is None and n.split():
        nick = n.split()[-1]
        c = [v for k_, v in data.items() if k_.split() and k_.split()[-1] == nick]
        if len(c) == 1:
            items = c[0]
    if items is None:
        return None
    return sorted(items, key=lambda x: -_sev(x["status"]))


def attach_injuries(p):
    lg = p.get("league") or (p.get("game") or "").split(" | ")[0]
    try:
        p["inj"] = injuries_for(p["team"], lg)
        p["inj_opp"] = injuries_for(_title_opp(p), lg)
    except Exception as e:
        print("injury attach error", repr(e), flush=True)
        p.setdefault("inj", None)
        p.setdefault("inj_opp", None)
    p["inj_at"] = time.time()


def inj_text(p):
    """Short injury summary for the Discord embed, labeled with full team names."""
    lg = p.get("league") or (p.get("game") or "").split(" | ")[0]
    opp = _title_opp(p)
    parts = []
    for label, key in ((p["team"], "inj"), (opp, "inj_opp")):
        items = p.get(key)
        if items is None:
            parts.append(f"**{label}:** not available")
            continue
        sig = [i for i in items if _sev(i["status"]) >= 1]
        if not sig:
            parts.append(f"**{label}:** no key injuries")
            continue
        top = ", ".join(f"{i['name']} ({_st(i['status'])})" for i in sig[:4])
        more = f" +{len(sig) - 4} more" if len(sig) > 4 else ""
        parts.append(f"**{label}:** {top}{more}")
    return "\n".join(parts)[:1000]


LAST_LOG = [0.0]


def scan():
    """Returns (picks, marks, arbs, leans). marks[ticker] = (win_prob, yes_ask) for live updates of open picks."""
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
            # the two sides of the game (draw excluded): the opponent is the other side's name
            sides = [(m["ticker"], full_name(m.get("yes_sub_title"), league)) for m in ms
                     if m.get("yes_sub_title") and _norm(m["yes_sub_title"]) not in ("tie", "draw")]
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
                win = mid / tot if tot else mid  # win probability from Kalshi prices
                marks[m["ticker"]] = (win, a)
                label = full_name(m.get("yes_sub_title"), league) if m.get("yes_sub_title") else m["ticker"]
                title = full_game((m.get("title") or "").replace(" Winner?", ""), league)
                game = f"{league} | {title}" if title else f"{league} | {m['ticker']}"
                opp = None
                if len(sides) == 2:
                    opp = next((n_ for t_, n_ in sides if t_ != m["ticker"]), None)
                price = a
                if win >= MIN_WIN and PMIN <= price <= PMAX:
                    leans.append({"side": "YES", "price": price, "p": win, "team": label, "game": game,
                                  "event": ev_key, "src": "Kalshi"})
                r = evaluate(m, win)
                if r:
                    picks.append({**r, "ticker": m["ticker"], "series": series, "event": ev_key, "league": league,
                                  "team": label, "opp": opp, "title": title, "game": game, "commence": end,
                                  "p_yes": win, "books": len(ms), "src": "Kalshi"})
                else:
                    st["no_call"] += 1
    if now - LAST_LOG[0] >= 120:
        LAST_LOG[0] = now
        print(f"scan: {st['markets']} markets | no quote {st['no_quote']} | wide {st['wide']} | "
              f"no call {st['no_call']} | CALLS {len(picks)} | ARBS {len(arbs)}", flush=True)
    return sorted(picks, key=lambda x: -x["growth"]), marks, arbs, leans


# =====================================================================
# CHART: win-probability step chart, scoreboard header, sport emblems, injury report
# =====================================================================
CW, CH = 960, 1070
BG = (15, 14, 16, 255)
WHITE, MUTED = (255, 255, 255, 255), (160, 168, 178, 255)
C_UP, C_DN = (0, 217, 142), (255, 87, 87)
AMBER = (255, 190, 40)
BLUE, PURPLE = (79, 114, 184), (142, 107, 191)
GOLD = (245, 180, 0, 255)
GRID, AXIS, SUB = (46, 46, 48, 255), (124, 124, 124, 255), (168, 168, 168, 255)
RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS
LOGO_DIR = os.getenv("LOGO_DIR", "logos")  # logos/<normalized_team_name>.png, e.g. logos/buffalo_bills.png
KIND = {"NFL": "football", "NCAAF": "football", "NBA": "basketball", "NCAAM": "basketball", "NHL": "hockey",
        "EPL": "soccer", "MLS": "soccer", "ATP": "tennis", "WTA": "tennis", "MLB": "baseball"}


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


@functools.lru_cache(maxsize=64)
def _sans(size, bold=True):
    names = ([os.getenv("CHART_FONT", "")] +
             (["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"] if bold
              else ["DejaVuSans.ttf", "LiberationSans-Regular.ttf", "arial.ttf", "Arial.ttf"]))
    for nm in names:
        if nm:
            try:
                return ImageFont.truetype(nm, size)
            except OSError:
                pass
    return _mono(size, bold)


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


def _kind(p):
    lg = p.get("league") or (p.get("game") or "").split(" | ")[0]
    return lg, KIND.get(lg, "football")


@functools.lru_cache(maxsize=128)
def _logo_img(name):
    fn = os.path.join(LOGO_DIR, _norm(name).replace(" ", "_") + ".png")
    try:
        return Image.open(fn).convert("RGBA")
    except Exception:
        return None


# ---------- abbreviations ----------
_AB = (
    "Atlanta Hawks=ATL;Boston Celtics=BOS;Brooklyn Nets=BKN;Charlotte Hornets=CHA;Chicago Bulls=CHI;"
    "Cleveland Cavaliers=CLE;Dallas Mavericks=DAL;Denver Nuggets=DEN;Detroit Pistons=DET;"
    "Golden State Warriors=GSW;Houston Rockets=HOU;Indiana Pacers=IND;Los Angeles Lakers=LAL;"
    "Los Angeles Clippers=LAC;Memphis Grizzlies=MEM;Miami Heat=MIA;Milwaukee Bucks=MIL;"
    "Minnesota Timberwolves=MIN;New Orleans Pelicans=NOP;New York Knicks=NYK;Oklahoma City Thunder=OKC;"
    "Orlando Magic=ORL;Philadelphia 76ers=PHI;Phoenix Suns=PHX;Portland Trail Blazers=POR;"
    "Sacramento Kings=SAC;San Antonio Spurs=SAS;Toronto Raptors=TOR;Utah Jazz=UTA;Washington Wizards=WAS;"
    "Arizona Cardinals=ARI;Atlanta Falcons=ATL;Baltimore Ravens=BAL;Buffalo Bills=BUF;Carolina Panthers=CAR;"
    "Chicago Bears=CHI;Cincinnati Bengals=CIN;Cleveland Browns=CLE;Dallas Cowboys=DAL;Denver Broncos=DEN;"
    "Detroit Lions=DET;Green Bay Packers=GB;Houston Texans=HOU;Indianapolis Colts=IND;"
    "Jacksonville Jaguars=JAX;Kansas City Chiefs=KC;Las Vegas Raiders=LV;Los Angeles Chargers=LAC;"
    "Los Angeles Rams=LAR;Miami Dolphins=MIA;Minnesota Vikings=MIN;New England Patriots=NE;"
    "New Orleans Saints=NO;New York Giants=NYG;New York Jets=NYJ;Philadelphia Eagles=PHI;"
    "Pittsburgh Steelers=PIT;San Francisco 49ers=SF;Seattle Seahawks=SEA;Tampa Bay Buccaneers=TB;"
    "Tennessee Titans=TEN;Washington Commanders=WAS;"
    "Anaheim Ducks=ANA;Boston Bruins=BOS;Buffalo Sabres=BUF;Calgary Flames=CGY;Carolina Hurricanes=CAR;"
    "Chicago Blackhawks=CHI;Colorado Avalanche=COL;Columbus Blue Jackets=CBJ;Dallas Stars=DAL;"
    "Detroit Red Wings=DET;Edmonton Oilers=EDM;Florida Panthers=FLA;Los Angeles Kings=LAK;Minnesota Wild=MIN;"
    "Montreal Canadiens=MTL;Nashville Predators=NSH;New Jersey Devils=NJD;New York Islanders=NYI;"
    "New York Rangers=NYR;Ottawa Senators=OTT;Philadelphia Flyers=PHI;Pittsburgh Penguins=PIT;"
    "San Jose Sharks=SJS;Seattle Kraken=SEA;St. Louis Blues=STL;Tampa Bay Lightning=TBL;"
    "Toronto Maple Leafs=TOR;Utah Mammoth=UTA;Vancouver Canucks=VAN;Vegas Golden Knights=VGK;"
    "Washington Capitals=WSH;Winnipeg Jets=WPG;"
    "Arsenal=ARS;Aston Villa=AVL;AFC Bournemouth=BOU;Brentford=BRE;Brighton and Hove Albion=BHA;"
    "Burnley=BUR;Chelsea=CHE;Crystal Palace=CRY;Everton=EVE;Fulham=FUL;Leeds United=LEE;Liverpool=LIV;"
    "Manchester City=MCI;Manchester United=MUN;Newcastle United=NEW;Nottingham Forest=NFO;"
    "Sunderland=SUN;Tottenham Hotspur=TOT;West Ham United=WHU;Wolverhampton Wanderers=WOL;"
    "Other outcomes=OTH")
ABBR = {_norm(a): b for a, b in (x.split("=") for x in _AB.split(";"))}
try:
    with open(os.getenv("ABBR_FILE", "team_abbr.json")) as _f:  # optional {"Ohio Bobcats": "OHIO"}
        ABBR.update({_norm(a): b for a, b in json.load(_f).items()})
except Exception:
    pass
SCHOOLS = [x.split("=", 1)[0] for x in (COLLEGE + ";" + COLLEGE_EXTRA).split(";")]


def abbr(name, league):
    n = _norm(name)
    if n in ABBR:
        return ABBR[n]
    w = re.sub(r"[^A-Za-z0-9 ]", "", name).split()
    if not w:
        return "?"
    if league in ("ATP", "WTA"):
        return w[-1][:3].upper()
    if league in ("NCAAF", "NCAAM"):
        best = max((s for s in SCHOOLS if _norm(name).startswith(_norm(s))), key=len, default=None)
        if best:
            sw = re.sub(r"[^A-Za-z0-9 ]", "", best).split()
            if len(sw) == 1:
                return sw[0].upper() if len(sw[0]) <= 4 else sw[0][:4].upper()
            return "".join(x[0] for x in sw[:3]).upper()
    return w[0][:3].upper() if len(w) > 1 else w[0][:4].upper()


def _title_opp(p):
    """The team we are playing against: the other side of the Kalshi game, else parsed from the title."""
    if p.get("opp"):
        return p["opp"]
    parts = re.split(r"\s+(?:at|vs\.?|v)\s+", p.get("title") or "", maxsplit=1, flags=re.I)
    if len(parts) == 2:
        a, b = parts
        t = _norm(p["team"])
        return b if _norm(a) == t else a if _norm(b) == t else b
    return "Opponent"


def _opp_name(p, league):
    return _title_opp(p)


# ---------- sport art (bottom-left panel) ----------
def art_football(d, box, k, accent, win):
    x0, y0, x1, y1 = box
    L = max(1, int(k(1)))
    w = x1 - x0
    h = min(y1 - y0, w / 1.9)
    cy = (y0 + y1) / 2
    fy0, fy1 = cy - h / 2, cy + h / 2
    ez = w * 0.09
    pw = (w - 2 * ez) / 10
    d.rectangle([x0, fy0, x1, fy1], fill=(30, 110, 55, 255))
    for i in range(0, 10, 2):
        d.rectangle([x0 + ez + i * pw, fy0, x0 + ez + (i + 1) * pw, fy1], fill=(37, 126, 63, 255))
    d.rectangle([x0, fy0, x0 + ez, fy1], fill=accent + (210,))
    d.rectangle([x1 - ez, fy0, x1, fy1], fill=(70, 80, 95, 230))
    for i in range(11):
        x = x0 + ez + i * pw
        d.line([(x, fy0), (x, fy1)], fill=(255, 255, 255, 210), width=L)
    for j in range(101):
        x = x0 + ez + j * pw / 10
        d.line([(x, fy0), (x, fy0 + k(3))], fill=(255, 255, 255, 170), width=L)
        d.line([(x, fy1 - k(3)), (x, fy1)], fill=(255, 255, 255, 170), width=L)
    d.rectangle([x0, fy0, x1, fy1], outline=(255, 255, 255, 255), width=max(1, int(k(2))))
    for gx, sgn in ((x0 + ez * 0.35, 1), (x1 - ez * 0.35, -1)):  # goalposts
        d.line([(gx, cy - h * 0.13), (gx, cy + h * 0.13)], fill=(255, 215, 0, 255), width=max(1, int(k(2))))
        d.line([(gx, cy - h * 0.13), (gx + sgn * k(5), cy - h * 0.13)], fill=(255, 215, 0, 255), width=L)
        d.line([(gx, cy + h * 0.13), (gx + sgn * k(5), cy + h * 0.13)], fill=(255, 215, 0, 255), width=L)
    bx = x0 + ez + max(0.0, min(1.0, win)) * 10 * pw  # ball at win probability
    d.line([(bx, fy0), (bx, fy1)], fill=AMBER + (230,), width=max(1, int(k(2))))
    rx, ry = k(9), k(5.5)
    d.ellipse([bx - rx, cy - ry, bx + rx, cy + ry], fill=(139, 69, 19, 255), outline=(255, 255, 255, 255))
    d.line([(bx - rx * 0.4, cy), (bx + rx * 0.4, cy)], fill=(255, 255, 255, 255), width=L)


def art_baseball(d, box, k, accent, win):
    x0, y0, x1, y1 = box
    L = max(1, int(k(1)))
    s = min(x1 - x0, y1 - y0)
    hx, hy = (x0 + x1) / 2, y1 - s * 0.05
    r = s * 0.95
    d.pieslice([hx - r, hy - r, hx + r, hy + r], 225, 315, fill=(30, 110, 55, 255))
    d.arc([hx - r, hy - r, hx + r, hy + r], 225, 315, fill=(255, 255, 255, 255), width=max(1, int(k(2))))
    for ang in (225, 315):
        d.line([(hx, hy), (hx + r * math.cos(math.radians(ang)), hy + r * math.sin(math.radians(ang)))],
               fill=(255, 255, 255, 230), width=L)
    side = s * 0.5
    c = side * 0.7071
    home, fst, snd, trd = (hx, hy), (hx + c, hy - c), (hx, hy - 2 * c), (hx - c, hy - c)
    d.polygon([home, fst, snd, trd], fill=(176, 120, 70, 255))
    mx, my = hx, hy - c
    inner = [(mx + (px - mx) * 0.62, my + (py - my) * 0.62) for px, py in (home, fst, snd, trd)]
    d.polygon(inner, fill=(37, 126, 63, 255))
    d.line([home, fst, snd, trd, home], fill=(255, 255, 255, 230), width=L)
    bs = k(4.5)
    for bx_, by_ in (fst, snd, trd):
        d.rectangle([bx_ - bs, by_ - bs, bx_ + bs, by_ + bs], fill=(255, 255, 255, 255))
    d.polygon([(hx - bs, hy - bs), (hx + bs, hy - bs), (hx + bs, hy), (hx, hy + bs), (hx - bs, hy)],
              fill=(255, 255, 255, 255))
    d.ellipse([mx - k(5), my - k(5), mx + k(5), my + k(5)], fill=(176, 120, 70, 255))
    # bat
    ax, ay = x0 + (x1 - x0) * 0.06, y1 - s * 0.04
    bx2, by2 = x0 + (x1 - x0) * 0.42, y0 + s * 0.30
    dx, dy = bx2 - ax, by2 - ay
    ln = math.hypot(dx, dy)
    nx, ny = -dy / ln, dx / ln
    wh, wb = k(2.5), k(7)
    poly = [(ax + nx * wh, ay + ny * wh), (bx2 + nx * wb, by2 + ny * wb),
            (bx2 - nx * wb, by2 - ny * wb), (ax - nx * wh, ay - ny * wh)]
    d.polygon(poly, fill=(205, 150, 90, 255), outline=(120, 80, 40, 255))
    d.ellipse([bx2 - wb, by2 - wb, bx2 + wb, by2 + wb], fill=(205, 150, 90, 255), outline=(120, 80, 40, 255))
    d.ellipse([ax - wh * 1.7, ay - wh * 1.7, ax + wh * 1.7, ay + wh * 1.7], fill=(120, 80, 40, 255))


def art_tennis(d, box, k, accent, win):
    x0, y0, x1, y1 = box
    L = max(1, int(k(1)))
    s = min(x1 - x0, y1 - y0)
    cx = (x0 + x1) / 2 + s * 0.12
    hcy = y0 + s * 0.27
    th = math.radians(35)
    a, b = s * 0.19, s * 0.25

    def T(px, py):
        return (cx + px * math.cos(th) - py * math.sin(th), hcy + px * math.sin(th) + py * math.cos(th))

    throat_end = b + s * 0.14
    handle_end = throat_end + s * 0.30
    hw = s * 0.045
    d.polygon([T(-hw, throat_end), T(hw, throat_end), T(hw, handle_end), T(-hw, handle_end)], fill=(40, 40, 48, 255))
    for i in range(1, 6):
        yy = throat_end + (handle_end - throat_end) * i / 6
        d.line([T(-hw, yy), T(hw, yy)], fill=(90, 90, 100, 255), width=L)
    d.polygon([T(-a * 0.5, b * 0.9), T(a * 0.5, b * 0.9), T(hw, throat_end), T(-hw, throat_end)],
              fill=accent + (255,))
    for dxx in [(-0.85 + 1.7 * i / 6) * a for i in range(7)]:
        ey = b * math.sqrt(max(0.0, 1 - (dxx / a) ** 2))
        d.line([T(dxx, -ey), T(dxx, ey)], fill=(255, 255, 255, 140), width=L)
    for dyy in [(-0.88 + 1.76 * i / 8) * b for i in range(9)]:
        ex = a * math.sqrt(max(0.0, 1 - (dyy / b) ** 2))
        d.line([T(-ex, dyy), T(ex, dyy)], fill=(255, 255, 255, 140), width=L)
    pts = [T(a * math.cos(2 * math.pi * i / 64), b * math.sin(2 * math.pi * i / 64)) for i in range(64)]
    d.line(pts + [pts[0]], fill=accent + (255,), width=max(2, int(k(5))), joint="curve")
    bx, by, br = x0 + (x1 - x0) * 0.76, y0 + s * 0.80, s * 0.09
    d.ellipse([bx - br, by - br, bx + br, by + br], fill=(205, 235, 60, 255))
    d.arc([bx - br * 2.1, by - br, bx - br * 0.1, by + br], -40, 40, fill=(255, 255, 255, 255),
          width=max(1, int(k(2))))
    d.arc([bx + br * 0.1, by - br, bx + br * 2.1, by + br], 140, 220, fill=(255, 255, 255, 255),
          width=max(1, int(k(2))))


def art_court(d, box, k, accent, win):
    x0, y0, x1, y1 = box
    L = max(1, int(k(1.5)))
    w = x1 - x0
    h = min(y1 - y0, w / 1.75)
    cy = (y0 + y1) / 2
    fy0, fy1 = cy - h / 2, cy + h / 2
    W = (255, 255, 255, 255)
    d.rectangle([x0, fy0, x1, fy1], fill=(196, 140, 84, 255))
    kw, kh = w * 0.19, h * 0.34
    d.rectangle([x0, cy - kh / 2, x0 + kw, cy + kh / 2], fill=accent + (110,), outline=W, width=L)
    d.rectangle([x1 - kw, cy - kh / 2, x1, cy + kh / 2], fill=accent + (110,), outline=W, width=L)
    d.rectangle([x0, fy0, x1, fy1], outline=W, width=L)
    d.line([((x0 + x1) / 2, fy0), ((x0 + x1) / 2, fy1)], fill=W, width=L)
    cr = h * 0.14
    d.ellipse([(x0 + x1) / 2 - cr, cy - cr, (x0 + x1) / 2 + cr, cy + cr], outline=W, width=L)
    d.arc([x0 + kw - cr, cy - cr, x0 + kw + cr, cy + cr], -90, 90, fill=W, width=L)
    d.arc([x1 - kw - cr, cy - cr, x1 - kw + cr, cy + cr], 90, 270, fill=W, width=L)
    hxl, hxr, tr = x0 + w * 0.055, x1 - w * 0.055, h * 0.40
    d.arc([hxl - tr, cy - tr, hxl + tr, cy + tr], -68, 68, fill=W, width=L)
    d.arc([hxr - tr, cy - tr, hxr + tr, cy + tr], 112, 248, fill=W, width=L)
    for hx in (hxl, hxr):
        d.ellipse([hx - h * 0.03, cy - h * 0.03, hx + h * 0.03, cy + h * 0.03], outline=(255, 100, 30, 255), width=L)


def art_rink(d, box, k, accent, win):
    x0, y0, x1, y1 = box
    L = max(1, int(k(1.5)))
    w = x1 - x0
    h = min(y1 - y0, w / 2.0)
    cy = (y0 + y1) / 2
    fy0, fy1 = cy - h / 2, cy + h / 2
    RED_, BLUE_ = (220, 50, 50, 255), (40, 90, 200, 255)
    d.rounded_rectangle([x0, fy0, x1, fy1], radius=h * 0.3, fill=(225, 238, 250, 255), outline=(60, 70, 90, 255),
                        width=L)
    mx = (x0 + x1) / 2
    d.line([(mx, fy0), (mx, fy1)], fill=RED_, width=L * 2)
    d.ellipse([mx - h * 0.16, cy - h * 0.16, mx + h * 0.16, cy + h * 0.16], outline=BLUE_, width=L)
    for fx in (x0 + w * 0.37, x0 + w * 0.63):
        d.line([(fx, fy0), (fx, fy1)], fill=BLUE_, width=L * 2)
    for gx in (x0 + w * 0.08, x0 + w * 0.92):
        d.line([(gx, fy0 + h * 0.05), (gx, fy1 - h * 0.05)], fill=RED_, width=L)
    cr = h * 0.12
    d.pieslice([x0 + w * 0.08 - cr, cy - cr, x0 + w * 0.08 + cr, cy + cr], -90, 90, fill=accent + (130,), outline=RED_)
    d.pieslice([x0 + w * 0.92 - cr, cy - cr, x0 + w * 0.92 + cr, cy + cr], 90, 270, fill=accent + (130,),
               outline=RED_)
    for fx in (x0 + w * 0.2, x0 + w * 0.8):
        for fy in (cy - h * 0.25, cy + h * 0.25):
            d.ellipse([fx - h * 0.1, fy - h * 0.1, fx + h * 0.1, fy + h * 0.1], outline=RED_, width=L)
            d.ellipse([fx - k(2), fy - k(2), fx + k(2), fy + k(2)], fill=RED_)


def art_pitch(d, box, k, accent, win):
    x0, y0, x1, y1 = box
    L = max(1, int(k(1.5)))
    w = x1 - x0
    h = min(y1 - y0, w / 1.55)
    cy = (y0 + y1) / 2
    fy0, fy1 = cy - h / 2, cy + h / 2
    W = (255, 255, 255, 255)
    d.rectangle([x0, fy0, x1, fy1], fill=(30, 110, 55, 255))
    n = 8
    for i in range(0, n, 2):
        d.rectangle([x0 + w * i / n, fy0, x0 + w * (i + 1) / n, fy1], fill=(37, 126, 63, 255))
    d.rectangle([x0, fy0, x1, fy1], outline=W, width=L)
    mx = (x0 + x1) / 2
    d.line([(mx, fy0), (mx, fy1)], fill=W, width=L)
    cr = h * 0.17
    d.ellipse([mx - cr, cy - cr, mx + cr, cy + cr], outline=W, width=L)
    d.ellipse([mx - k(2), cy - k(2), mx + k(2), cy + k(2)], fill=W)
    pb, gb = (w * 0.16, h * 0.54), (w * 0.06, h * 0.26)
    d.rectangle([x0, cy - pb[1] / 2, x0 + pb[0], cy + pb[1] / 2], fill=accent + (70,), outline=W, width=L)
    d.rectangle([x1 - pb[0], cy - pb[1] / 2, x1, cy + pb[1] / 2], fill=accent + (70,), outline=W, width=L)
    d.rectangle([x0, cy - gb[1] / 2, x0 + gb[0], cy + gb[1] / 2], outline=W, width=L)
    d.rectangle([x1 - gb[0], cy - gb[1] / 2, x1, cy + gb[1] / 2], outline=W, width=L)


ART = {"football": art_football, "baseball": art_baseball, "tennis": art_tennis, "basketball": art_court,
       "hockey": art_rink, "soccer": art_pitch}


# ---------- emblems: helmet / racket / basketball / puck, with the team logo built in ----------
def _badge(base, d, cx, cy, r, color, ab, logo):
    """White disc with the team logo, or the team's initials if there's no logo file."""
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255), outline=tuple(color) + (255,),
              width=max(1, int(r * 0.14)))
    if logo:
        lg = logo.copy()
        lg.thumbnail((max(1, int(r * 1.5)), max(1, int(r * 1.5))))
        base.paste(lg, (int(cx - lg.width / 2), int(cy - lg.height / 2)), lg)
    else:
        t = ab[:3]
        d.text((cx, cy), t, font=_sans(max(6, int(r * (0.85 if len(t) < 3 else 0.68))), True),
               fill=tuple(int(c * 0.6) for c in color) + (255,), anchor="mm")


def draw_helmet(base, d, cx, cy, s, color, ab, face, logo=None):
    """face=+1 looks right, -1 looks left. s = overall size in px. Logo sits on the shell side."""

    def P(x, y):
        return (cx + face * x * s, cy + y * s)

    def ell(x0, y0, x1, y1, **kw):
        a, b = P(x0, y0), P(x1, y1)
        d.ellipse([min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])], **kw)

    def poly(pts, **kw):
        d.polygon([P(*q) for q in pts], **kw)

    def ln(pts, w, fill):
        d.line([P(*q) for q in pts], fill=fill, width=max(1, int(w * s)), joint="curve")

    col = tuple(color) + (255,)
    dark = tuple(int(c * 0.55) for c in color) + (255,)
    gray = (200, 200, 208, 255)
    d.ellipse([cx - 0.55 * s, cy + 0.40 * s, cx + 0.55 * s, cy + 0.52 * s], fill=(0, 0, 0, 90))
    poly([(0.05, 0.2), (0.46, 0.14), (0.50, 0.30), (0.40, 0.42), (0.0, 0.44), (-0.3, 0.34), (-0.30, 0.2)],
         fill=col, outline=dark)
    ell(-0.60, -0.50, 0.50, 0.42, fill=col, outline=dark, width=max(1, int(0.02 * s)))
    stripe = []
    for i in range(19):
        x = -0.52 + 0.94 * i / 18
        stripe.append((x, -0.04 - 0.40 * math.sqrt(max(0.0, 1 - ((x + 0.05) / 0.56) ** 2))))
    ln(stripe, 0.055, (255, 255, 255, 235))
    ell(-0.08, 0.10, 0.06, 0.24, fill=dark)  # ear hole
    ell(-0.055, 0.125, 0.035, 0.215, fill=(0, 0, 0, 120))
    ell(0.14, -0.22, 0.52, 0.08, fill=(10, 10, 16, 150), outline=(255, 255, 255, 70))  # visor
    ln([(0.47, -0.02), (0.66, 0.02), (0.68, 0.20), (0.54, 0.32), (0.34, 0.36)], 0.035, gray)  # facemask
    ln([(0.66, 0.08), (0.44, 0.10)], 0.03, gray)
    ln([(0.67, 0.15), (0.44, 0.19)], 0.03, gray)
    ln([(0.34, 0.36), (0.12, 0.33)], 0.03, (40, 40, 48, 255))  # chin strap
    ell(-0.46, -0.42, -0.12, -0.24, fill=(255, 255, 255, 50))  # gloss
    bx, by = P(-0.16, -0.10)
    _badge(base, d, bx, by, 0.2 * s, color, ab, logo)  # team logo on the shell


def draw_racket(base, d, cx, cy, s, color, ab, face, logo=None):
    """Tennis racket tilted 30 degrees; face=+1 head leans right, -1 leans left. Logo in the strings."""
    th = math.radians(30)
    a, b = 0.27, 0.36  # head half-width / half-height (in units of s)
    hx = cx + face * 0.1475 * s  # head center, so the whole racket is centered on (cx, cy)
    hy = cy - 0.2555 * s

    def T(x, y):
        return (hx + face * (x * math.cos(th) - y * math.sin(th)) * s, hy + (x * math.sin(th) + y * math.cos(th)) * s)

    col = tuple(color) + (255,)
    dark = tuple(int(c * 0.55) for c in color) + (255,)
    throat_end, handle_end, hw = b + 0.17, b + 0.17 + 0.42, 0.055
    d.ellipse([cx - 0.5 * s, cy + 0.50 * s, cx + 0.5 * s, cy + 0.60 * s], fill=(0, 0, 0, 70))  # shadow
    # handle + leather grip
    d.polygon([T(-hw, throat_end), T(hw, throat_end), T(hw, handle_end), T(-hw, handle_end)], fill=(38, 38, 46, 255))
    for i in range(1, 8):
        yy = throat_end + (handle_end - throat_end) * i / 8
        d.line([T(-hw, yy), T(hw, yy + 0.02)], fill=(110, 110, 124, 255), width=max(1, int(0.012 * s)))
    ex_, ey_ = T(0, handle_end)
    d.ellipse([ex_ - hw * s * 1.3, ey_ - hw * s * 1.3, ex_ + hw * s * 1.3, ey_ + hw * s * 1.3], fill=col)
    # throat in team color
    d.polygon([T(-a * 0.55, b * 0.9), T(a * 0.55, b * 0.9), T(hw, throat_end), T(-hw, throat_end)], fill=col)
    d.line([T(0, b * 0.9), T(0, throat_end)], fill=(255, 255, 255, 120), width=max(1, int(0.015 * s)))
    # string bed
    sw = max(1, int(0.012 * s))
    for i in range(9):
        dx = (-0.88 + 1.76 * i / 8) * a
        ey = b * math.sqrt(max(0.0, 1 - (dx / a) ** 2))
        d.line([T(dx, -ey), T(dx, ey)], fill=(255, 255, 255, 150), width=sw)
    for i in range(11):
        dy = (-0.9 + 1.8 * i / 10) * b
        ex = a * math.sqrt(max(0.0, 1 - (dy / b) ** 2))
        d.line([T(-ex, dy), T(ex, dy)], fill=(255, 255, 255, 150), width=sw)
    # frame
    pts = [T(a * math.cos(2 * math.pi * i / 72), b * math.sin(2 * math.pi * i / 72)) for i in range(72)]
    d.line(pts + [pts[0]], fill=dark, width=max(2, int(0.075 * s)), joint="curve")
    d.line(pts + [pts[0]], fill=col, width=max(2, int(0.05 * s)), joint="curve")
    gl = [T(a * math.cos(math.radians(t)), b * math.sin(math.radians(t))) for t in range(200, 262, 6)]
    d.line(gl, fill=(255, 255, 255, 110), width=max(1, int(0.015 * s)))  # frame gloss
    hxp, hyp = T(0, 0)
    _badge(base, d, hxp, hyp, 0.17 * s, color, ab, logo)  # logo in strings


def draw_ball(base, d, cx, cy, r, kind, color, ab, logo=None):
    """Basketball / soccer ball / baseball with a logo badge; hockey gets a puck with the logo on top."""
    w = max(1, int(r * 0.06))
    if kind == "hockey":
        ry = r * 0.42
        d.ellipse([cx - r, cy + r * 0.22 - ry, cx + r, cy + r * 0.22 + ry], fill=(18, 18, 22, 255))
        d.rectangle([cx - r, cy - r * 0.18, cx + r, cy + r * 0.22], fill=(30, 30, 36, 255))
        d.rectangle([cx - r, cy - r * 0.02, cx + r, cy + r * 0.10], fill=tuple(color) + (255,))
        d.ellipse([cx - r, cy - r * 0.18 - ry, cx + r, cy - r * 0.18 + ry], fill=(52, 52, 60, 255))
        _badge(base, d, cx, cy - r * 0.18, r * 0.34, color, ab, logo)
        return
    ring = r * 0.12
    d.ellipse([cx - r - ring, cy - r - ring, cx + r + ring, cy + r + ring], fill=tuple(color) + (255,))
    body = {"basketball": (214, 106, 44), "soccer": (245, 245, 245), "baseball": (240, 240, 235)}.get(kind,
                                                                                                    (245, 245, 245))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=body + (255,))
    arcs = lambda c, ww: (d.arc([cx - 2.3 * r, cy - r, cx - 0.3 * r, cy + r], -45, 45, fill=c, width=ww),
                          d.arc([cx + 0.3 * r, cy - r, cx + 2.3 * r, cy + r], 135, 225, fill=c, width=ww))
    if kind == "basketball":
        c = (70, 35, 15, 255)
        d.line([(cx, cy - r), (cx, cy + r)], fill=c, width=w)
        d.line([(cx - r, cy), (cx + r, cy)], fill=c, width=w)
        arcs(c, w)
        d.arc([cx - r * 0.98, cy - r * 0.98, cx + r * 0.98, cy + r * 0.98], 0, 360, fill=c, width=w)
    elif kind == "baseball":
        arcs((200, 40, 40, 255), w * 2)
    else:
        dk = (30, 30, 34, 255)
        pts = [(cx + 0.34 * r * math.cos(math.radians(-90 + 72 * i)),
                cy + 0.34 * r * math.sin(math.radians(-90 + 72 * i))) for i in range(5)]
        d.polygon(pts, fill=dk)
        for i in range(5):
            ang = math.radians(-90 + 72 * i)
            d.line([pts[i], (cx + 0.75 * r * math.cos(ang), cy + 0.75 * r * math.sin(ang))], fill=dk, width=w)
            ex, ey = cx + 0.9 * r * math.cos(ang), cy + 0.9 * r * math.sin(ang)
            d.ellipse([ex - 0.13 * r, ey - 0.13 * r, ex + 0.13 * r, ey + 0.13 * r], fill=dk)
    d.ellipse([cx - 0.7 * r, cy - 0.8 * r, cx - 0.1 * r, cy - 0.35 * r], fill=(255, 255, 255, 45))  # gloss
    _badge(base, d, cx, cy, r * 0.5, color, ab, logo)


def draw_team_emblem(base, cx, cy, s, name, kind, color, ab, face):
    """Football -> helmet, tennis -> racket, everything else -> its ball/puck. Team logo is built in."""
    d = ImageDraw.Draw(base, "RGBA")
    logo = _logo_img(name)
    if kind == "football":
        draw_helmet(base, d, cx, cy, s, color, ab, face, logo)
    elif kind == "tennis":
        draw_racket(base, d, cx, cy, s, color, ab, face, logo)
    else:
        draw_ball(base, d, cx, cy, s * 0.5, kind, color, ab, logo)


# ---------- the chart ----------
def render_frames(p, outcome, S, n):
    SS = S * (2 if S <= 1.25 else 1)  # supersample for smooth edges
    k = lambda v: v * SS
    lw = lambda v: max(1, int(round(k(v))))
    F = lambda sz, b=True: _sans(max(6, int(round(k(sz)))), b)
    W, H = int(CW * SS), int(CH * SS)
    league, kind = _kind(p)
    team, opp = p["team"], _opp_name(p, league)
    a1, a2 = abbr(team, league), abbr(opp, league)
    pc, oc = BLUE, PURPLE
    price = p["price"]
    entry = p.get("entry_price") or price
    left = p["commence"] - time.time()
    started = left < GAME_HOURS.get(league, 3.0) * 3600
    hist = [list(x) for x in (p.get("pts") or [[time.time(), price, p["p"]]])]
    if len(hist) == 1:
        hist.append([hist[0][0] + 60, hist[0][1], hist[0][2]])
    win = hist[-1][2] * 100
    p0 = (p.get("p0") or hist[0][2]) * 100
    delta = win - p0

    base = Image.new("RGBA", (W, H), BG)
    d = ImageDraw.Draw(base, "RGBA")

    def fit(text, maxw, size, bold=True, lo=9):
        """Font + text that fit in maxw (scaled px): shrink the font first, then trim with a dot."""
        sz = size
        while sz > lo and d.textlength(text, font=F(sz, bold)) > maxw:
            sz -= 1
        f = F(sz, bold)
        t = text
        while len(t) > 3 and d.textlength(t, font=f) > maxw:
            t = t[:-1]
        if t != text:
            t = t.rstrip() + "."
        return f, t

    # ===== scoreboard header =====
    draw_team_emblem(base, k(112), k(84), k(100), team, kind, pc, a1, +1)
    draw_team_emblem(base, k(848), k(84), k(100), opp, kind, oc, a2, -1)
    f_, t_ = fit(team, k(176), 21)
    d.text((k(112), k(166)), t_, font=f_, fill=WHITE, anchor="mm")
    f_, t_ = fit(opp, k(176), 21)
    d.text((k(848), k(166)), t_, font=f_, fill=WHITE, anchor="mm")
    d.text((k(255), k(86)), f"{price * 100:.0f}", font=F(62), fill=GOLD, anchor="mm")
    d.text((k(705), k(86)), f"{(1 - price) * 100:.0f}", font=F(62), fill=GOLD, anchor="mm")
    d.text((k(255), k(128)), f"{team.upper()[:16]} (c)", font=F(11, False), fill=AXIS, anchor="mm")
    d.text((k(705), k(128)), f"{opp.upper()[:16]}{' / DRAW' if league in ('EPL', 'MLS') else ''} (c)",
           font=F(11, False), fill=AXIS, anchor="mm")
    if outcome is None:
        tag_c = (0, 217, 142, 255) if started else AMBER + (255,)
        d.ellipse([k(432), k(38), k(442), k(48)], fill=tag_c)
        d.text((k(450), k(43)), "LIVE" if started else "UPCOMING", font=F(13), fill=tag_c, anchor="lm")
        d.text((k(480), k(86)), _hhmm(left), font=F(38), fill=WHITE, anchor="mm")
        d.text((k(480), k(124)), f"{league} | {team} to win"[:44], font=F(16, False), fill=SUB, anchor="mm")
        d.text((k(480), k(150)), f"Entry {entry * 100:.0f}c | {1 / entry:.2f}x payout", font=F(13, False),
               fill=AXIS, anchor="mm")
    else:
        oc_col = (C_UP if outcome == "win" else C_DN) + (255,)
        d.text((k(480), k(44)), "FINAL", font=F(13), fill=AXIS, anchor="mm")
        d.text((k(480), k(88)), outcome.upper(), font=F(42), fill=oc_col, anchor="mm")
        d.text((k(480), k(128)), f"{p.get('pnl', 0):+.2f}u", font=F(22), fill=WHITE, anchor="mm")
        d.text((k(480), k(152)), f"{league} | {team}"[:44], font=F(13, False), fill=SUB, anchor="mm")
    # page indicator
    d.rounded_rectangle([k(455), k(218), k(505), k(231)], radius=k(6.5), fill=WHITE)
    d.ellipse([k(520), k(218), k(533), k(231)], fill=(85, 85, 90, 255))

    # ===== win-probability step chart =====
    CT, CB = 272, 640
    PX0_, PXE, GX1, LBL = 24, 700, 842, 936
    allv = [h[2] * 100 for h in hist]
    allv += [100 - v for v in allv]
    lo = min(30, math.floor((min(allv) - 2) / 10) * 10)
    hi = max(70, math.ceil((max(allv) + 2) / 10) * 10)
    Y = lambda v: k(CB) - (v - lo) / (hi - lo) * (k(CB) - k(CT))
    t0, t1 = hist[0][0], max(hist[-1][0], hist[0][0] + 1)
    X = lambda t: k(PX0_) + (t - t0) / (t1 - t0) * (k(PXE) - k(PX0_))
    for v in range(int(lo), int(hi) + 1, 10):
        mid_ = v == 50
        _dots(d, k(PX0_), Y(v), k(GX1), Y(v), (78, 78, 82, 255) if mid_ else GRID, lw(1), k(4), k(4))
        d.text((k(LBL), Y(v)), f"{v}%", font=F(15), fill=SUB if mid_ else AXIS, anchor="rm")
    ey = Y(p0)
    _dots(d, k(PX0_), ey, k(PXE), ey, AMBER + (210,), lw(1.4), k(2), k(5))
    d.text((k(PX0_ + 4), ey - k(9)), f"ENTRY {p0:.0f}%", font=F(11), fill=AMBER + (255,), anchor="lm")

    def steps(vals):
        pts_ = []
        keep = [i for i in range(len(hist)) if i in (0, len(hist) - 1) or vals[i] != vals[i - 1]]
        prev = None
        for i in keep:
            x, y = X(hist[i][0]), Y(vals[i])
            if prev is not None:
                pts_.append((x, prev))
            pts_.append((x, y))
            prev = y
        return pts_

    pv = [h[2] * 100 for h in hist]
    ov = [100 - v for v in pv]
    lines = [(steps(ov), oc), (steps(pv), pc)]
    for pts_, c in lines:  # glow, then crisp line
        d.line(pts_, fill=c + (38,), width=lw(10), joint="curve")
    for pts_, c in lines:
        d.line(pts_, fill=c + (255,), width=lw(3.2), joint="curve")
    # session high / low markers on the pick line
    if len(pv) > 2 and max(pv) - min(pv) >= 2:
        for idx, lab, up in ((pv.index(max(pv)), "HI", True), (pv.index(min(pv)), "LO", False)):
            hx, hy = X(hist[idx][0]), Y(pv[idx])
            d.ellipse([hx - k(4.5), hy - k(4.5), hx + k(4.5), hy + k(4.5)], fill=BG, outline=pc + (255,),
                      width=lw(2))
            d.text((hx, hy + (-k(14) if up else k(14))), f"{lab} {pv[idx]:.0f}%", font=F(11), fill=SUB,
                   anchor="mm")

    # end dots, name pills and big readouts
    ex = k(PXE)
    ys = {"p": Y(win), "o": Y(100 - win)}
    anchor_y = dict(ys)
    gap = k(100)
    up, dn = sorted(("p", "o"), key=lambda q: ys[q])
    if anchor_y[dn] - anchor_y[up] < gap:
        mid = (anchor_y[up] + anchor_y[dn]) / 2
        anchor_y[up], anchor_y[dn] = mid - gap / 2, mid + gap / 2
    PXL, PW = k(724), k(176)
    for key, nm, val, c, sub in (("p", team, win, pc, f"{delta:+.1f} pts"),
                                 ("o", opp, 100 - win, oc, f"{-delta:+.1f} pts")):
        ay = anchor_y[key]
        pill_cy = ay - k(18)
        d.line([(ex + k(10), ys[key]), (PXL, pill_cy)], fill=c + (110,), width=lw(1.5))
        d.rounded_rectangle([PXL, pill_cy - k(17), PXL + PW, pill_cy + k(17)], radius=k(6), fill=c + (255,))
        f_, t_ = fit(nm, PW - k(14), 16)
        d.text((PXL + PW / 2, pill_cy), t_, font=f_, fill=WHITE, anchor="mm")
        d.text((PXL, ay + k(26)), f"{val:.0f}%", font=F(42), fill=c + (255,), anchor="lm")
        d.text((PXL + k(2), ay + k(58)), sub, font=F(12, False), fill=AXIS, anchor="lm")
        d.ellipse([ex - k(8.5), ys[key] - k(8.5), ex + k(8.5), ys[key] + k(8.5)], fill=c + (255,), outline=BG,
                  width=lw(2))

    # time axis
    d.text((k(PX0_), k(654)), "PICKED " + time.strftime("%H:%M UTC", time.gmtime(t0)), font=F(11, False),
           fill=AXIS, anchor="lm")
    d.text((k(PXE), k(654)), "NOW" if outcome is None else "FINAL", font=F(11), fill=AXIS, anchor="rm")

    # ===== bottom row: sport art + stat chips =====
    d.rounded_rectangle([k(24), k(676), k(344), k(836)], radius=k(14), fill=(255, 255, 255, 8),
                        outline=(255, 255, 255, 28), width=lw(1))
    ART[kind](d, (k(32), k(684), k(336), k(828)), k, pc, win / 100)
    up_c, dn_c = C_UP + (255,), C_DN + (255,)
    if outcome is None:
        chip6 = ("IF WIN", f"+{(1 - entry - fee(entry)) * 100:.0f}c", up_c)
    else:
        chip6 = ("RESULT", f"{p.get('pnl', 0):+.2f}u", up_c if outcome == "win" else dn_c)
    chips = [("ENTRY", f"{entry * 100:.0f}c", WHITE),
             ("NOW", f"{price * 100:.0f}c", up_c if price >= entry else dn_c),
             ("PAYOUT", f"{1 / price:.2f}x", WHITE),
             ("WIN PROB", f"{win:.0f}%", pc + (255,)),
             ("EDGE VS ENTRY", f"{delta:+.1f} pts", up_c if delta >= 0 else dn_c),
             chip6]
    cw_, chh, g = (572 - 24) / 3, 74, 12
    for i, (lab, val, col) in enumerate(chips):
        cx0 = 364 + (i % 3) * (cw_ + g)
        cy0 = 676 + (i // 3) * (chh + g)
        d.rounded_rectangle([k(cx0), k(cy0), k(cx0 + cw_), k(cy0 + chh)], radius=k(12), fill=(255, 255, 255, 10),
                            outline=(255, 255, 255, 26), width=lw(1))
        d.text((k(cx0 + 14), k(cy0 + 18)), lab, font=F(11, False), fill=AXIS, anchor="lm")
        d.text((k(cx0 + 14), k(cy0 + 48)), val, font=F(27), fill=col, anchor="lm")

    # ===== injury report =====
    IY0, IY1 = 856, 1046
    sev_col = {3: (255, 87, 87, 255), 2: (255, 140, 60, 255), 1: (255, 190, 40, 255), 0: (0, 217, 142, 255)}
    d.rounded_rectangle([k(24), k(IY0), k(936), k(IY1)], radius=k(14), fill=(255, 255, 255, 8),
                        outline=(255, 255, 255, 28), width=lw(1))
    d.text((k(40), k(IY0 + 20)), "INJURY REPORT", font=F(14), fill=WHITE, anchor="lm")
    d.text((k(920), k(IY0 + 20)), f"{INJ_SRC.get(league, 'Injury feed')} | statuses can change before game time",
           font=F(10, False), fill=AXIS, anchor="rm")

    def inj_col(x0, name_, col_, items):
        BW = 190  # badge shows the full team name
        d.rounded_rectangle([k(x0), k(IY0 + 38), k(x0 + BW), k(IY0 + 60)], radius=k(6), fill=col_ + (255,))
        f_, t_ = fit(name_, k(BW - 14), 13)
        d.text((k(x0 + BW / 2), k(IY0 + 49)), t_, font=f_, fill=WHITE, anchor="mm")
        if items is None:
            d.text((k(x0), k(IY0 + 92)), "Injury data not available for this matchup", font=F(13, False),
                   fill=AXIS, anchor="lm")
            return
        if not items:
            d.text((k(x0), k(IY0 + 92)), "No injuries reported", font=F(14), fill=C_UP + (255,), anchor="lm")
            return
        cnt = [sum(1 for i in items if _sev(i["status"]) == s_) for s_ in (3, 2, 1)]
        summ = " ".join(f"{c} {lab}" for c, lab in zip(cnt, ("OUT", "DOUBTFUL", "QUESTIONABLE")) if c)
        f2, t2 = fit(summ or "All probable", k(220), 11, False, 8)
        d.text((k(x0 + BW + 12), k(IY0 + 49)), t2, font=f2, fill=SUB, anchor="lm")
        show = items if len(items) <= 4 else items[:3]
        for r_, it in enumerate(show):
            ry = IY0 + 78 + r_ * 27 + 13
            sv = _sev(it["status"])
            d.rounded_rectangle([k(x0), k(ry - 10), k(x0 + 54), k(ry + 10)], radius=k(5),
                                fill=sev_col[sv][:3] + (60,), outline=sev_col[sv], width=lw(1))
            d.text((k(x0 + 27), k(ry)), _st(it["status"]), font=F(11), fill=sev_col[sv], anchor="mm")
            d.text((k(x0 + 64), k(ry)), it["name"][:22], font=F(14), fill=WHITE, anchor="lm")
            meta = " | ".join(x for x in (it["pos"], it["detail"]) if x)[:24]
            d.text((k(x0 + 428), k(ry)), meta, font=F(11, False), fill=SUB, anchor="rm")
        if len(items) > 4:
            d.text((k(x0 + 64), k(IY0 + 78 + 3 * 27 + 13)), f"+{len(items) - 3} more", font=F(12, False),
                   fill=AXIS, anchor="lm")

    inj_col(40, team, pc, p.get("inj"))
    inj_col(492, opp, oc, p.get("inj_opp"))
    d.text((k(CW / 2), k(1058)), "Signals only, not financial advice", font=F(10, False), fill=(90, 90, 95, 255),
           anchor="mm")

    # ===== frames (pulsing end dots) =====
    out = []
    for f in range(n):
        ph = (math.sin(2 * math.pi * f / n) + 1) / 2 if n > 1 else 0.3
        im = base.copy()
        fd = ImageDraw.Draw(im, "RGBA")
        for key, c in (("p", pc), ("o", oc)):
            r = k(14 + 8 * ph)
            fd.ellipse([ex - r, ys[key] - r, ex + r, ys[key] + r], fill=c + (int(75 - 45 * ph),))
            fd.ellipse([ex - k(8.5), ys[key] - k(8.5), ex + k(8.5), ys[key] + k(8.5)], fill=c + (255,), outline=BG,
                       width=lw(2))
        out.append(im.convert("RGB").resize((int(CW * S), int(CH * S)), RESAMPLE))
    return out


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
    return (f"{w}W-{n - w}L ({w / n * 100:.0f}%)", f"{pnl:+.2f}u ({pnl / stake * 100:+.1f}% ROI)",
            f"{lw_}W-{len(last) - lw_}L ({lw_ / len(last) * 100:.0f}%)")


def build_embed(p, state, outcome=None):
    rec, pnl, last = stats(state["history"])
    lg = p.get("league") or (p.get("game") or "").split(" | ")[0]
    lines = [f"**Game:** {p['game']}", f"**Pick:** {p['team']} to win", f"**Opponent:** {_opp_name(p, lg)}",
             f"**Price:** {p['price'] * 100:.0f}c | **Payout:** {1 / p['price']:.2f}x",
             f"**Win prob:** {p['p'] * 100:.0f}%", f"**Closes:** <t:{int(p['commence'])}:R>"]
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
                       {"name": "P&L", "value": pnl, "inline": True},
                       {"name": "🩺 Injury report", "value": inj_text(p), "inline": False}],
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
    embed = {"title": f"📋 Hourly top {len(top)}", "description": "\n\n".join(rows), "color": GREY,
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
    """Update open picks (live price, win probability, injuries) and re-post the chart."""
    for t, old in state["open"].items():
        if now >= old["commence"] or t not in marks:
            continue
        win, price = marks[t]
        if not (0 < price < 1):
            continue
        c = price + fee(price)
        old.update({"price": price, "p": win, "ev": win - c, "roi": (win - c) / price, "edge": win - price})
        old["pts"] = (old.get("pts", []) + [[round(now), round(price, 4), round(win, 4)]])[-600:]
        if now - old.get("inj_at", 0) >= INJ_TTL:
            attach_injuries(old)
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
        first_time = a["event"] not in state["arbs"]  # count profit once per game per 24h
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
        attach_injuries(p)
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
