import os, time, json, base64, threading, statistics
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("POLL_SECONDS", "0.5")
os.environ.setdefault("IMG_SECONDS", "1")
os.environ.setdefault("EDIT_SECONDS", "1")

import requests
import websocket
import signals as S

TIMER_LEAD = float(os.getenv("TIMER_LEAD", "0.7"))
KALSHI_POLL = float(os.getenv("KALSHI_POLL", "0.1"))
DASH_PORT = int(os.getenv("DASH_PORT", "8000"))
DASH_HZ = float(os.getenv("DASH_HZ", "30"))
KEY_ID = os.getenv("KALSHI_KEY_ID")
KEY_PATH = os.getenv("KALSHI_KEY_PATH")
HTTP = requests.Session()

FEED = {"m": None, "t": 0.0}      # latest Kalshi market
LATEST = {}                        # (sym, source) -> (price, time)
STATE = {"s": None}
FLOCK = threading.Lock()


# ---------- generic websocket runner with auto-reconnect ----------
def run_ws(name, url, on_open, on_msg, headers=None):
    def loop():
        while True:
            try:
                ws = websocket.WebSocketApp(
                    url, header=headers() if headers else None,
                    on_open=on_open, on_message=on_msg,
                    on_error=lambda w, e: print(name, "error", e))
                ws.run_forever(ping_interval=15, ping_timeout=8)
            except Exception as e:
                print(name, "crashed", e)
            time.sleep(1)
    threading.Thread(target=loop, daemon=True).start()


# ---------- exchange price pushes ----------
def start_price_feeds():
    def cb_open(ws):
        ws.send(json.dumps({"type": "subscribe", "product_ids": ["ETH-USD", "BTC-USD"],
                            "channels": ["ticker"]}))

    def cb_msg(ws, m):
        d = json.loads(m)
        if d.get("type") == "ticker" and d.get("price"):
            LATEST[(d["product_id"][:3], "cb")] = (float(d["price"]), time.time())

    def kr_open(ws):
        ws.send(json.dumps({"method": "subscribe",
                            "params": {"channel": "ticker", "symbol": ["ETH/USD", "BTC/USD"]}}))

    def kr_msg(ws, m):
        d = json.loads(m)
        if d.get("channel") == "ticker":
            for x in d.get("data", []):
                LATEST[(x["symbol"].split("/")[0], "kr")] = (float(x["last"]), time.time())

    def bs_open(ws):
        for p in ("ethusd", "btcusd"):
            ws.send(json.dumps({"event": "bts:subscribe",
                                "data": {"channel": f"live_trades_{p}"}}))

    def bs_msg(ws, m):
        d = json.loads(m)
        if d.get("event") == "trade":
            sym = d["channel"].replace("live_trades_", "")[:3].upper()
            LATEST[(sym, "bs")] = (float(d["data"]["price"]), time.time())

    run_ws("coinbase", "wss://ws-feed.exchange.coinbase.com", cb_open, cb_msg)
    run_ws("kraken", "wss://ws.kraken.com/v2", kr_open, kr_msg)
    run_ws("bitstamp", "wss://ws.bitstamp.net", bs_open, bs_msg)


def cached_prices():
    now, out = time.time(), {}
    for sym in ("ETH", "BTC"):
        v = [p for (s, _), (p, t) in list(LATEST.items()) if s == sym and now - t < 10]
        out[sym] = (statistics.median(v), len(v)) if v else (None, 0)
    return out


# ---------- Kalshi: REST refresh (strike, rollover) plus optional websocket push ----------
def kalshi_rest():
    m = None
    while True:
        t0 = time.time()
        try:
            if m is None or S.ts(m["close_time"]) <= time.time():
                m = S.open_market_rest()
            if m:
                m = {**m, **S.get_market(m["ticker"])}
                with FLOCK:
                    FEED["m"], FEED["t"] = m, time.time()
        except Exception as e:
            print("kalshi rest error", e)
        time.sleep(max(0.0, (1.0 if KEY_ID else KALSHI_POLL) - (time.time() - t0)))


def _ws_headers():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    path = "/trade-api/ws/v2"
    with open(KEY_PATH, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    t = str(int(time.time() * 1000))
    sig = key.sign((t + "GET" + path).encode(),
                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                               salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": KEY_ID, "KALSHI-ACCESS-TIMESTAMP": t,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}


def kalshi_ws():
    sub = {"tk": None}

    def on_open(ws):
        with FLOCK:
            m = FEED["m"]
        if m:
            sub["tk"] = m["ticker"]
            ws.send(json.dumps({"id": 1, "cmd": "subscribe",
                                "params": {"channels": ["ticker"],
                                           "market_tickers": [m["ticker"]]}}))

    def on_msg(ws, raw):
        msg = json.loads(raw)
        if msg.get("type") != "ticker":
            return
        d = msg.get("msg", {})
        with FLOCK:
            if not FEED["m"]:
                return
            if sub["tk"] and FEED["m"]["ticker"] != sub["tk"]:
                ws.close()  # market rolled: reconnect and resubscribe
                return
            cur = dict(FEED["m"])
            for k in ("yes_bid", "yes_ask", "yes_bid_dollars", "yes_ask_dollars"):
                if k in d:
                    cur[k] = d[k]
            if "price" in d:
                cur["last_price"] = d["price"]
            FEED["m"], FEED["t"] = cur, time.time()

    while FEED["m"] is None:
        time.sleep(0.2)
    run_ws("kalshi", "wss://api.elections.kalshi.com/trade-api/ws/v2",
           on_open, on_msg, headers=_ws_headers)


def feed_market():
    with FLOCK:
        m = FEED["m"]
    return m if m and S.ts(m["close_time"]) > time.time() else None


# ---------- local live dashboard (server-sent events) ----------
PAGE = """<!doctype html><meta charset=utf-8><title>ETH 15m live</title>
<style>
*{box-sizing:border-box}
body{margin:0;height:100vh;display:flex;flex-direction:column;color:#fff;font-family:system-ui,sans-serif;
 background:linear-gradient(180deg,#0d1f1f,#2d1a1f 60%,#0a0a0a)}
#top{display:flex;gap:26px;padding:14px 20px 6px;align-items:baseline;flex-wrap:wrap}
#clk{font-size:36px;font-variant-numeric:tabular-nums;font-weight:700}
#bar{display:flex;gap:8px;padding:6px 20px 10px;flex-wrap:wrap}
.b{flex:1 1 90px;min-width:90px;border-radius:12px;padding:7px 8px 8px;text-align:center;
 border:1.5px solid rgba(255,255,255,.28);background:rgba(90,96,108,.55);transition:background .15s}
.b.up{background:#00d98e}.b.dn{background:#ff5757}
.b small{display:block;font-size:11px;opacity:.85;letter-spacing:.5px}
.b b{display:block;font-size:15px;margin-top:3px}
#info{padding:0 20px 6px;font-size:13px;color:#a0a8b2}
canvas{flex:1;width:100%;min-height:0}
</style>
<div id=top><b style="font-size:22px">ETH 15 min</b><span id=clk></span>
<span>Now <b id=px style="font-size:28px"></b></span><span>Target <b id=tg></b></span>
<span id=diff></span><span id=kal></span><span id=call></span></div>
<div id=bar></div><div id=info></div>
<canvas id=c></canvas>
<script>
const c=document.getElementById('c'),g=c.getContext('2d'),$=i=>document.getElementById(i);
const NAMES=[['mom','MOM'],['fib','FIB'],['ema','EMA'],['boll','BOLL'],['atr','ATR'],['macd','MACD']];
const bar=$('bar'),els={};
[...NAMES,['tally','TALLY'],['conf','CONF']].forEach(([k,n])=>{
 const d=document.createElement('div');d.className='b';d.innerHTML='<small>'+n+'</small><b>WAIT</b>';
 bar.appendChild(d);els[k]=d});
function setB(k,v,label,tip){
 const e=els[k];e.className='b'+(v===1?' up':v===-1?' dn':'');
 e.lastChild.textContent=(v===1?'▲ ':v===-1?'▼ ':'')+label;e.title=tip||''}
let D=null,off=0,hist=[];const WIN=120000;
const es=new EventSource('/stream');
es.onmessage=e=>{D=JSON.parse(e.data);off=D.now*1000-Date.now();
 if(D.price)hist.push([D.now*1000,D.price]);
 const cut=D.now*1000-WIN;while(hist.length&&hist[0][0]<cut)hist.shift();
 const V=D.votes||{};
 NAMES.forEach(([k])=>{const x=V[k]||[null,''],v=x[0];
  setB(k,v,v===1?'UP':v===-1?'DOWN':v===0?'NEUTRAL':'WAIT',x[1])});
 const t=D.tally;
 if(t&&(t[0]+t[1]+t[2])>0){const tv=t[0]>t[1]?1:t[1]>t[0]?-1:0;setB('tally',tv,t[0]+'U '+t[1]+'D','Confluence')}
 else setB('tally',null,'WAIT','');
 if(D.side)setB('conf',D.side==='UP'?1:-1,Math.round(D.conf||0)+'%','Live confidence');
 else setB('conf',null,'--','');
 $('info').textContent='Combined '+(D.comb==null?'--':(D.comb>=0?'+':'')+D.comb.toFixed(2))
  +' | RSI '+(D.rsi==null?'--':D.rsi.toFixed(0))
  +' | Fib '+(D.fib||'--')+(D.fib?'%':'')
  +' | Flips '+(D.flips||0)
  +(D.avg60?' | 60s avg $'+D.avg60.toFixed(2):'')};
function draw(){
 requestAnimationFrame(draw);if(!D)return;
 const r=devicePixelRatio||1,W=c.clientWidth*r,H=c.clientHeight*r;
 if(c.width!=W||c.height!=H){c.width=W;c.height=H}
 const now=Date.now()+off,left=Math.max(0,D.close_ts*1000-now);
 $('clk').textContent=Math.floor(left/60000)+':'+String(Math.floor(left/1000)%60).padStart(2,'0')+'.'+String(Math.floor(left%1000)).padStart(3,'0');
 $('px').textContent=D.price?'$'+D.price.toFixed(2):'--';
 $('tg').textContent=D.strike?'$'+D.strike.toFixed(2):'--';
 const df=D.price-D.strike;$('diff').textContent=(df>=0?'▲ $':'▼ $')+Math.abs(df).toFixed(2);
 $('diff').style.color=df>=0?'#00d98e':'#ff5757';
 $('kal').textContent='Kalshi YES '+D.yes.toFixed(0)+'% / NO '+(100-D.yes).toFixed(0)+'%';
 $('call').textContent=D.side?D.side+' call '+Math.round(D.conf||0)+'%':'No call yet';
 g.clearRect(0,0,W,H);if(hist.length<2)return;
 const t1=now,t0=now-WIN,vals=hist.map(h=>h[1]).concat(D.strike?[D.strike]:[]);
 let lo=Math.min(...vals),hi=Math.max(...vals),pad=(hi-lo||1)*.15;lo-=pad;hi+=pad;
 const X=t=>(t-t0)/(t1-t0)*(W-90*r),Y=v=>H-(v-lo)/(hi-lo)*H;
 if(D.strike){g.setLineDash([8*r,6*r]);g.strokeStyle='#cdd2dc';g.beginPath();g.moveTo(0,Y(D.strike));g.lineTo(W,Y(D.strike));g.stroke();g.setLineDash([])}
 g.strokeStyle=df>=0?'#00d98e':'#ff5757';g.lineWidth=2*r;g.beginPath();
 hist.forEach((h,i)=>{i?g.lineTo(X(h[0]),Y(h[1])):g.moveTo(X(h[0]),Y(h[1]))});g.stroke();
}
draw();
</script>"""


def payload(m):
    st = STATE["s"] or {}
    cur = st.get("current") or {}
    out = {"now": time.time(), "price": cached_prices()["ETH"][0],
           "strike": float(m.get("floor_strike") or m.get("cap_strike") or 0),
           "close_ts": S.ts(m["close_time"]), "yes": S.yes_pct(m),
           "side": cur.get("side"),
           "conf": cur.get("live_conf") or cur.get("conf"),
           "votes": cur.get("votes"), "tally": cur.get("tally"),
           "comb": cur.get("comb"), "rsi": cur.get("rsi"), "fib": cur.get("fib"),
           "flips": cur.get("flips", 0), "avg60": cur.get("avg60")}
    return json.dumps(out, default=str)


class Dash(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path != "/stream":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            while True:
                m = feed_market()
                if m:
                    try:
                        data = payload(m)
                        self.wfile.write(f"data: {data}\n\n".encode())
                        self.wfile.flush()
                    except RuntimeError:
                        pass  # state dict changed mid-read; next frame is fine
                time.sleep(1 / DASH_HZ)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


# ---------- patches onto the original script ----------
S.open_market_rest = S.open_market
S.open_market = feed_market
S.all_prices = cached_prices
for s in S.PRICES:
    S.PRICES[s] = deque(maxlen=4000)

_settle, _save = S.settle_pending, S.save_state
_last = {"settle": 0.0, "save": 0.0}


def settle_throttled(state):
    if time.time() - _last["settle"] >= 3:
        _last["settle"] = time.time()
        _settle(state)


def save_throttled(state):
    if time.time() - _last["save"] >= 1:
        _last["save"] = time.time()
        _save(state)


S.settle_pending, S.save_state = settle_throttled, save_throttled

_tl = threading.local()


class _Time:
    def __getattr__(self, n):
        return getattr(time, n)

    def time(self):
        return time.time() + (TIMER_LEAD if getattr(_tl, "r", False) else 0.0)


S.time = _Time()
_render = S.render_frames


def render_lead(*a, **k):
    _tl.r = True
    try:
        return _render(*a, **k)
    finally:
        _tl.r = False


S.render_frames = render_lead


# ---------- main ----------
def main():
    threading.Thread(target=kalshi_rest, daemon=True).start()
    start_price_feeds()
    if KEY_ID and KEY_PATH:
        threading.Thread(target=kalshi_ws, daemon=True).start()
    else:
        print("No Kalshi key set: polling Kalshi every", KALSHI_POLL, "s instead of push")
    srv = ThreadingHTTPServer(("127.0.0.1", DASH_PORT), Dash)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"Live dashboard: http://localhost:{DASH_PORT}")

    while feed_market() is None or not LATEST:
        time.sleep(0.05)
    state = S.load_state()
    STATE["s"] = state
    end = time.time() + S.RUN_SECONDS
    nxt = time.time()
    while time.time() < end:
        try:
            S.tick(state)
        except Exception as e:
            print("tick error", e)
        nxt += S.POLL
        d = nxt - time.time()
        if d > 0:
            time.sleep(d)
        else:
            nxt = time.time()


if __name__ == "__main__":
    main()
