#!/usr/bin/env python3
"""
GEX calculator -> TradingView pine-seeds CSVs.

For each ticker it pulls the delayed CBOE options chain (free, includes gamma + OI),
computes dollar gamma exposure per strike, extracts key levels, and writes five
seed files per ticker into ./data (one row = one day, columns carry the values):

  {T}_L1  open=spot  high=gamma_flip  low=hvl  close=call_wall  volume=put_wall
  {T}_L2  top-5 call-GEX strikes   (open..volume)
  {T}_L3  top-5 call-GEX values    ($ per 1% move, billions)
  {T}_L4  top-5 put-GEX strikes
  {T}_L5  top-5 put-GEX values     (billions, positive magnitude)

GEX convention (standard SpotGamma-style): dealers long calls / short puts.
  call GEX = +gamma * OI * 100 * spot^2 * 0.01
  put  GEX = -gamma * OI * 100 * spot^2 * 0.01
"""
import csv
import datetime as dt
import os
import re
import sys
import requests

CBOE = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
INDEXES = {"SPX", "NDX", "RUT", "VIX", "XSP"}
DEFAULT = ["SPX", "NDX", "SPY", "QQQ", "IWM", "TLT", "NVDA", "TSLA", "AAPL", "AMZN", "META", "MSFT", "GOOGL", "AMD"]
OCC = re.compile(r"^(?P<root>[A-Z0-9]+?)(?P<d>\d{6})(?P<cp>[CP])(?P<k>\d{8})$")

# Which expirations to include
#   "0dte"  -> nearest expiry only
#   "week"  -> expiries within 7 days
#   "all"   -> everything within MAX_DAYS
MODE = os.environ.get("GEX_MODE", "week")
MAX_DAYS = int(os.environ.get("GEX_MAX_DAYS", "45"))
OUT = os.environ.get("GEX_OUT", "data")


def fetch(ticker):
    sym = ("_" + ticker) if ticker in INDEXES else ticker
    r = requests.get(CBOE.format(sym=sym), timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()["data"]


def compute(ticker):
    data = fetch(ticker)
    spot = float(data["current_price"])
    today = dt.date.today()
    rows = []
    for o in data["options"]:
        m = OCC.match(o["option"])
        if not m:
            continue
        exp = dt.datetime.strptime(m["d"], "%y%m%d").date()
        dte = (exp - today).days
        if dte < 0 or dte > MAX_DAYS:
            continue
        rows.append((exp, dte, m["cp"], int(m["k"]) / 1000.0, float(o.get("gamma") or 0), float(o.get("open_interest") or 0)))
    if not rows:
        raise RuntimeError("no options")

    exps = sorted({r[0] for r in rows})
    if MODE == "0dte":
        keep = {exps[0]}
    elif MODE == "week":
        keep = {e for e in exps if (e - today).days <= 7}
    else:
        keep = set(exps)
    rows = [r for r in rows if r[0] in keep]

    calls, puts = {}, {}
    scale = spot * spot * 0.01 * 100
    for _, _, cp, k, g, oi in rows:
        v = g * oi * scale
        (calls if cp == "C" else puts)[k] = (calls if cp == "C" else puts).get(k, 0) + v

    strikes = sorted(set(calls) | set(puts))
    net = {k: calls.get(k, 0) - puts.get(k, 0) for k in strikes}

    call_wall = max(calls, key=calls.get)
    put_wall = max(puts, key=puts.get)
    hvl = max(strikes, key=lambda k: abs(net[k]))  # highest-volume level proxy: largest |net GEX|

    # gamma flip: strike where cumulative net GEX crosses zero (closest to spot)
    flip, cum, prev_k, prev_c = spot, 0.0, None, None
    crossings = []
    for k in strikes:
        cum += net[k]
        if prev_c is not None and (prev_c < 0) != (cum < 0):
            t = abs(prev_c) / (abs(prev_c) + abs(cum) or 1)
            crossings.append(prev_k + t * (k - prev_k))
        prev_k, prev_c = k, cum
    if crossings:
        flip = min(crossings, key=lambda x: abs(x - spot))

    def top5(d, above=None):
        items = sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:5]
        items += [(0.0, 0.0)] * (5 - len(items))
        return [i[0] for i in items], [i[1] / 1e9 for i in items]

    cs, cv = top5(calls)
    ps, pv = top5(puts)
    return {
        "L1": [spot, flip, hvl, call_wall, put_wall],
        "L2": cs, "L3": cv, "L4": ps, "L5": pv,
    }


def write(ticker, res):
    os.makedirs(OUT, exist_ok=True)
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT")
    for key, vals in res.items():
        path = os.path.join(OUT, f"{ticker}_{key}.csv")
        existing = []
        if os.path.exists(path):
            with open(path) as f:
                existing = [l.strip() for l in f if l.strip() and not l.startswith(day)]
        existing = existing[-250:]
        existing.append(day + "," + ",".join(f"{v:.4f}" for v in vals))
        with open(path, "w") as f:
            f.write("\n".join(existing) + "\n")


def main():
    tickers = [t.upper().replace(".", "_") for t in (sys.argv[1:] or DEFAULT)]
    for t in tickers:
        try:
            write(t, compute(t))
            print("ok  ", t)
        except Exception as e:
            print("FAIL", t, e)


if __name__ == "__main__":
    main()
