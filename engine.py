"""Deterministic analysis core. All structure/zone/score values come from code, never from an LLM."""
import os, math, time, random, re, json, datetime as dt
import requests

CFG = dict(min_score=75, min_lead=20, min_rr=2.0, max_spread=0.5, stale_min=10, news_required=True,
           blackout_min=30, session=(7, 20), k=3, max_risk_pct=2.0)  # starting hypotheses, not proven rules
TFS = {"M5": 5, "M15": 15, "H1": 60, "H4": 240}
MAXP = dict(htf=20, structure=20, zone=15, sweep=15, entry=10, rr=10, conditions=10)


# ---------- data ----------
def sample_m5(n=4000, seed=7):
    """SYNTHETIC random-walk candles. For demo only."""
    r = random.Random(seed); p = 2650.0; t0 = (int(time.time()) // 300) * 300 - n * 300; out = []
    for i in range(n):
        o = p; p += r.gauss(0, .9) + (.12 if (i // 350) % 2 == 0 else -.12)
        out.append(dict(t=t0 + i * 300, o=round(o, 2), h=round(max(o, p) + abs(r.gauss(0, .4)), 2),
                        l=round(min(o, p) - abs(r.gauss(0, .4)), 2), c=round(p, 2)))
    return out


def live_m5():
    k = os.getenv("TWELVEDATA_KEY")
    if not k: return None
    r = requests.get("https://api.twelvedata.com/time_series", timeout=15,
                     params=dict(symbol="XAU/USD", interval="5min", outputsize=5000, timezone="UTC", apikey=k)).json()
    v = r.get("values")
    if not v: raise RuntimeError(str(r)[:120])
    f = lambda s: int(dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp())
    out = [dict(t=f(x["datetime"]), o=float(x["open"]), h=float(x["high"]), l=float(x["low"]), c=float(x["close"])) for x in reversed(v)]
    return [c for c in out if c["t"] + 300 <= time.time()]  # closed candles only


def get_news():
    k = os.getenv("FINNHUB_KEY")
    if not k: return dict(ok=False, items=[], source=None, at=None, err="FINNHUB_KEY not set")
    try:
        d = dt.date.today()
        r = requests.get("https://finnhub.io/api/v1/calendar/economic", timeout=10,
                         params={"token": k, "from": str(d), "to": str(d + dt.timedelta(days=1))}); r.raise_for_status()
        it = [dict(time=e.get("time"), event=e.get("event"), country=e.get("country"), impact=str(e.get("impact")))
              for e in r.json().get("economicCalendar", [])]
        return dict(ok=True, items=it, source="finnhub.io economic calendar", at=int(time.time()))
    except Exception as e:
        return dict(ok=False, items=[], source="finnhub.io", at=None, err=str(e)[:80])


def blackout(news, now, mins):
    for e in news["items"]:
        try:
            t = dt.datetime.strptime(e["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
        except Exception: continue
        if e["country"] == "US" and e["impact"].lower() in ("high", "3") and abs(t - now) <= mins * 60: return True
    return False


# ---------- structure ----------
def resample(m5, mins):
    step = mins * 60; out = []
    for c in m5:
        b = c["t"] // step * step
        if out and out[-1]["t"] == b:
            x = out[-1]; x["h"] = max(x["h"], c["h"]); x["l"] = min(x["l"], c["l"]); x["c"] = c["c"]
        else: out.append(dict(t=b, o=c["o"], h=c["h"], l=c["l"], c=c["c"]))
    if out and out[-1]["t"] + step > m5[-1]["t"] + 300: out.pop()  # drop still-forming bucket
    return out


def atr(c, n=14):
    s = c[-n - 1:]
    return sum(max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"])) for a, b in zip(s, s[1:])) / max(len(s) - 1, 1)


def swings(c, k):
    o = []
    for i in range(k, len(c) - k):  # a swing needs k closed candles after it: no repaint
        w = c[i - k:i + k + 1]
        if c[i]["h"] == max(x["h"] for x in w): o.append((i, "H", c[i]["h"]))
        if c[i]["l"] == min(x["l"] for x in w): o.append((i, "L", c[i]["l"]))
    return o


def structure(c, k):
    sw = swings(c, k); by = {}
    for s in sw: by.setdefault(s[0] + k, []).append(s)
    H = L = None; trend = 0; ev = []
    for j, b in enumerate(c):
        for s in by.get(j, []):
            if s[1] == "H": H = s
            else: L = s
        if H and b["c"] > H[2]:  # close-based break
            ev.append(dict(j=j, t=b["t"], dir=1, type="BOS" if trend >= 0 else "CHoCH", level=H[2], c=b["c"])); trend = 1; H = None
        elif L and b["c"] < L[2]:
            ev.append(dict(j=j, t=b["t"], dir=-1, type="BOS" if trend <= 0 else "CHoCH", level=L[2], c=b["c"])); trend = -1; L = None
    return dict(trend=trend, events=ev, sw=sw)


def zones(c, ev, a):
    out = []
    for e in ev:
        j, d = e["j"], e["dir"]
        for i in range(j - 1, max(j - 16, -1), -1):  # last opposite candle before the break
            if (c[i]["c"] < c[i]["o"]) if d == 1 else (c[i]["c"] > c[i]["o"]):
                z = dict(dir=d, lo=c[i]["l"], hi=c[i]["h"], t=c[i]["t"], disp=abs(c[j]["c"] - c[j]["o"]) / a if a else 0)
                z["mit"] = any((x["c"] < z["lo"]) if d == 1 else (x["c"] > z["hi"]) for x in c[j + 1:])
                out.append(z); break
    return out


def fvgs(c, n=40):
    out = []
    for i in range(max(2, len(c) - n), len(c)):
        if c[i]["l"] > c[i - 2]["h"]:
            lo, hi, d = c[i - 2]["h"], c[i]["l"], 1; f = any(x["l"] <= lo for x in c[i + 1:])
        elif c[i]["h"] < c[i - 2]["l"]:
            lo, hi, d = c[i]["h"], c[i - 2]["l"], -1; f = any(x["h"] >= hi for x in c[i + 1:])
        else: continue
        out.append(dict(dir=d, lo=lo, hi=hi, i=i, filled=f))
    return out


def sweeps(c, sw, k, n=40):
    out = []; sw = sw[-30:]
    for j in range(max(0, len(c) - n), len(c)):
        b = c[j]
        for s in sw:
            if s[0] + k < j:
                if s[1] == "H" and b["h"] > s[2] > b["c"]: out.append(dict(j=j, t=b["t"], dir=-1, level=s[2], ext=b["h"], f="h"))
                if s[1] == "L" and b["l"] < s[2] < b["c"]: out.append(dict(j=j, t=b["t"], dir=1, level=s[2], ext=b["l"], f="l"))
    return out


def build(m5, k):
    x = {}
    for n, m in TFS.items():
        c = m5 if n == "M5" else resample(m5, m)
        a = atr(c); st = structure(c, k)
        x[n] = dict(c=c, a=a, st=st, z=zones(c, st["events"][-6:], a), fvg=fvgs(c), sw=sweeps(c, st["sw"], k),
                    idx={b["t"]: b for b in c[-400:]})
    return x


# ---------- scoring (identical rules for both sides) ----------
def score_side(x, env, d, cfg):
    name = "buy" if d == 1 else "sell"
    cats = {k: dict(pts=0, max=m, why=[]) for k, m in MAXP.items()}; ev = []
    def add(cat, p, why, ref=None):
        cats[cat]["pts"] += p; cats[cat]["why"].append(f"{'+%g' % p if p else '0'}: {why}")
        if ref: ev.append(dict(cat=cat, why=why, ref=ref))
    m5, m15, h1 = x["M5"], x["M15"], x["H1"]; c15, c5 = m15["c"], m5["c"]; a15 = m15["a"]
    for tf in ("H4", "H1"):  # HTF: H4 and H1 only
        ok = x[tf]["st"]["trend"] == d
        add("htf", 10 if ok else 0, f"{tf} trend {'aligned' if ok else 'not aligned'}")
    ev15 = m15["st"]["events"]; last = ev15[-1] if ev15 else None  # structure: M15 only
    if last and last["dir"] == d and len(c15) - 1 - last["j"] <= 20:
        add("structure", 12, f"M15 {last['type']} at {last['level']}", ("M15", last["t"], "c", last["c"]))
    else: add("structure", 0, "no recent M15 break in this direction")
    add("structure", 8 if m15["st"]["trend"] == d else 0, "M15 trend " + ("aligned" if m15["st"]["trend"] == d else "not aligned"))
    touch = lambda z: any(b["l"] <= z["hi"] and b["h"] >= z["lo"] for b in c15[-6:])
    zc = [z for z in m15["z"] if z["dir"] == d and not z["mit"] and touch(z)]; z = zc[-1] if zc else None
    if z:
        add("zone", 8, f"unmitigated M15 zone {z['lo']}-{z['hi']} touched in last 6 candles", ("M15", z["t"], "l", [b for b in c15 if b["t"] == z["t"]][0]["l"]))
        if z["disp"] >= 1: add("zone", 4, f"displacement {z['disp']:.1f} ATR")
        h = h1["c"][-50:]; eq = (max(b["h"] for b in h) + min(b["l"] for b in h)) / 2
        if (z["hi"] <= eq) if d == 1 else (z["lo"] >= eq): add("zone", 3, "zone in " + ("discount" if d == 1 else "premium") + " of H1 range")
    else: add("zone", 0, "no unmitigated zone in play")
    sw = [s for s in m15["sw"] if s["dir"] == d and len(c15) - 1 - s["j"] <= 12]; s = sw[-1] if sw else None
    if s:
        add("sweep", 10, f"M15 liquidity sweep of {s['level']} closed back inside", ("M15", s["t"], s["f"], s["ext"]))
        if z and z["lo"] - .5 * a15 <= s["ext"] <= z["hi"] + .5 * a15: add("sweep", 5, "sweep occurred at the zone")
    else: add("sweep", 0, "no recent sweep")
    e5 = [e for e in m5["st"]["events"] if e["dir"] == d and len(c5) - 1 - e["j"] <= 5]; trig = e5[-1] if e5 else None
    if trig: add("entry", 6, f"M5 {trig['type']} closed beyond {trig['level']}", ("M5", trig["t"], "c", trig["c"]))
    else: add("entry", 0, "no closed M5 confirmation")
    fv = [f for f in m5["fvg"] + m15["fvg"] if f["dir"] == d and not f["filled"]]
    add("entry", 4 if fv else 0, "unfilled FVG present" if fv else "no unfilled FVG")
    plan = None
    if z and trig:
        entry = trig["level"]; sl = z["lo"] - .25 * a15 if d == 1 else z["hi"] + .25 * a15; risk = (entry - sl) * d
        if 0 < risk <= 3 * a15:
            pr = sorted({q[2] for q in h1["st"]["sw"] if (q[2] - entry) * d >= risk}, reverse=(d == -1))
            tp1 = pr[0] if pr else None; tp2 = pr[1] if len(pr) > 1 else None; tgt = tp2 or tp1
            rr = round((tgt - entry) * d / risk, 2) if tgt else None
            plan = dict(entry=round(entry, 2), sl=round(sl, 2), tp1=tp1, tp2=tp2, rr=rr,
                        invalidation=f"M15 close {'below' if d == 1 else 'above'} {round(sl, 2)}")
    rr = plan["rr"] if plan else None
    add("rr", 10 if rr and rr >= 3 else 7 if rr and rr >= 2 else 3 if rr and rr >= 1.5 else 0, f"planned R:R {rr}")
    sp = env["spread"]; h = dt.datetime.fromtimestamp(env["now"], dt.timezone.utc).hour
    add("conditions", 4 if sp is not None and sp <= cfg["max_spread"] else 0, f"spread {sp}")
    add("conditions", 3 if cfg["session"][0] <= h < cfg["session"][1] else 0, f"session hour {h} UTC")
    add("conditions", 3 if env["news_ok"] and not env["blackout"] else 0, "news check passed" if env["news_ok"] and not env["blackout"] else "news unavailable or blackout")
    total = sum(v["pts"] for v in cats.values()); valid = bool(plan and trig and z)
    return dict(side=name, total=total, valid=valid, plan=plan, cats=cats, evidence=ev,
                summary=f"Valid {name} setup, rule score {total}/100" if valid else f"No valid {name} setup",
                trigger=bool(trig))


# ---------- judge ----------
def judge(x, env, buy, sell, cfg, risk_pct):
    blocks, miss = [], []; m5 = x["M5"]["c"]
    if not m5 or env["now"] - (m5[-1]["t"] + 300) > cfg["stale_min"] * 60: blocks.append("Market data missing or stale")
    for s in (buy, sell):
        for e in s["evidence"]:
            tf, t, f, v = e["ref"]; b = x[tf]["idx"].get(t)
            if not b or abs(b[f] - v) > 1e-9: blocks.append("Evidence not verifiable: " + e["why"])
    sp = env["spread"]
    if sp is None: blocks.append("Spread unavailable")
    elif sp > cfg["max_spread"]: blocks.append("Spread above limit")
    if env["blackout"]: blocks.append("High-impact news blackout active")
    if cfg["news_required"] and not env["news_ok"]: blocks.append("Required news check unavailable")
    if risk_pct > cfg["max_risk_pct"]: blocks.append("Risk exceeds limit")
    w, o = (buy, sell) if buy["total"] >= sell["total"] else (sell, buy); p = w["plan"]; d = 1 if w is buy else -1
    if not p: blocks.append("No valid stop loss / plan can be placed")
    else:
        risk = abs(p["entry"] - p["sl"])
        if (m5[-1]["c"] - p["entry"]) * d > .5 * risk: blocks.append("Price already passed planned entry")
        if not p["rr"] or p["rr"] < cfg["min_rr"]: miss.append(f"Reward:risk {p['rr']} below {cfg['min_rr']}")
    if not w["trigger"]: miss.append("Entry trigger not confirmed on a closed M5 candle")
    if w["total"] < cfg["min_score"]: miss.append(f"Leader scores {w['total']} (<{cfg['min_score']})")
    if w["total"] - o["total"] < cfg["min_lead"]: miss.append(f"Lead is {w['total'] - o['total']} (<{cfg['min_lead']})")
    dec = "WAIT" if blocks or miss else w["side"].upper()
    return dict(decision=dec, leader=w["side"], overrides=sorted(set(blocks)), missing=miss,
                plan=p if dec != "WAIT" or p else None)


def cycle(m5, env, cfg=CFG, risk_pct=1.0):
    x = build(m5, cfg["k"]); b = score_side(x, env, 1, cfg); s = score_side(x, env, -1, cfg)
    return x, b, s, judge(x, env, b, s, cfg, risk_pct)


# ---------- risk ----------
def risk_calc(balance, risk_pct, entry, sl, spec):
    amt = balance * risk_pct / 100; dist = abs(entry - sl)
    out = dict(risk_amount=round(amt, 2), stop_distance=round(dist, 2), lots=None)
    need = ("contract_size", "tick_size", "tick_value", "volume_step", "volume_min")
    if not spec or any(not spec.get(k) for k in need):
        out["note"] = "Broker specs missing: lot size not computed"; return out
    per_lot = dist / spec["tick_size"] * spec["tick_value"]  # tick_value: per lot, in account currency
    lots = math.floor(amt / per_lot / spec["volume_step"] + 1e-9) * spec["volume_step"]
    if lots < spec["volume_min"]: out["note"] = "Risk too small for minimum volume"; return out
    out.update(lots=round(lots, 4), actual_risk=round(lots * per_lot, 2)); return out
