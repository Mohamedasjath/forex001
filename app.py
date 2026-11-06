import os, re, json, time, asyncio
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from engine import *

app = FastAPI(); lock = asyncio.Lock(); HIST = "history.jsonl"; HERE = os.path.dirname(__file__)


class Spec(BaseModel):
    contract_size: float | None = None; tick_size: float | None = None; tick_value: float | None = None
    volume_step: float | None = None; volume_min: float | None = None

class Req(BaseModel):
    balance: float = 10000; ccy: str = "USD"; risk_pct: float = 1.0; spec: Spec = Spec()

class Plan(BaseModel):
    entry: float; sl: float; tp1: float | None = None; tp2: float | None = None; rr: float | None = None; invalidation: str

class Side(BaseModel):  # every agent output is validated before display
    side: str; total: float; valid: bool; summary: str; trigger: bool; cats: dict; evidence: list; plan: Plan | None = None

class Explain(BaseModel):
    buy: str; sell: str; judge: str


def explain(payload):
    """Optional LLM narration of already-calculated evidence. Rejected if it introduces numbers not in the data."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key: return None
    try:
        body = json.dumps(payload)
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=40,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json=dict(model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"), max_tokens=700,
                      system='Explain ONLY the supplied calculated evidence in plain English. Never add prices, levels, news or facts not in the JSON. Reply with JSON only: {"buy":str,"sell":str,"judge":str}.',
                      messages=[dict(role="user", content=body)])).json()
        txt = re.sub(r"```json|```", "", r["content"][0]["text"]).strip()
        e = Explain.model_validate_json(txt)
        allowed = set(re.findall(r"\d+\.?\d*", body))
        if not set(re.findall(r"\d+\.?\d*", e.buy + e.sell + e.judge)) <= allowed: return None
        return e.model_dump()
    except Exception:
        return None


def run(r: Req):
    try: m5 = live_m5()
    except Exception as e: raise HTTPException(502, f"Live data failed: {e}")
    if m5:
        src, demo = "Twelve Data XAU/USD (live)", False
        spread = float(os.getenv("SPREAD")) if os.getenv("SPREAD") else None  # broker spread must be supplied
    else:
        m5, src, demo, spread = sample_m5(), "SAMPLE DATA - synthetic, not tradable", True, 0.30
    now = time.time(); news = get_news()
    env = dict(now=now, spread=spread, news_ok=news["ok"], blackout=news["ok"] and blackout(news, now, CFG["blackout_min"]))
    x, buy, sell, j = cycle(m5, env, CFG, r.risk_pct)
    buy, sell = Side.model_validate(buy).model_dump(), Side.model_validate(sell).model_dump()
    p = j["plan"]; risk = risk_calc(r.balance, r.risk_pct, p["entry"], p["sl"], r.spec.model_dump()) if p else None
    c15 = x["M15"]["c"]
    out = dict(instrument="XAUUSD", price=m5[-1]["c"], source=src, demo=demo, updated=m5[-1]["t"] + 300, spread=spread,
               buy=buy, sell=sell, judge=j, risk=risk, ccy=r.ccy, news=news,
               chart=dict(c=c15[-200:], zones=[z for z in x["M15"]["z"] if not z["mit"]],
                          events=[e for e in x["M15"]["st"]["events"] if e["j"] >= len(c15) - 200]), cfg=CFG)
    out["explain"] = explain(dict(buy=buy, sell=sell, judge=j))
    with open(os.path.join(HERE, HIST), "a") as f:
        f.write(json.dumps(dict(ts=int(now), src=src, decision=j["decision"], buy=buy["total"], sell=sell["total"], price=out["price"])) + "\n")
    return out


@app.post("/api/analyze")
async def analyze(r: Req):
    if lock.locked(): raise HTTPException(409, "Analysis already running")  # duplicate-request guard
    async with lock: return await asyncio.to_thread(run, r)

@app.get("/api/history")
def history():
    p = os.path.join(HERE, HIST)
    return [json.loads(l) for l in open(p)][-50:][::-1] if os.path.exists(p) else []

@app.get("/")
def index(): return FileResponse(os.path.join(HERE, "index.html"))
