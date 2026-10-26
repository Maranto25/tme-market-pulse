#!/usr/bin/env python3
"""
TME Market Pulse - automatic market.json updater (v1).

Replaces the dead public relay services. Runs on GitHub's servers (no CORS,
no keys in the page) and writes market.json for the live dashboard:

  * 10-Year Treasury  - Yahoo ^TNX (intraday) -> FRED DGS10 (daily close) fallback
  * 2-Year Treasury   - FRED DGS2
  * Freddie Mac PMMS  - FRED MORTGAGE30US / MORTGAGE15US (weekly, Thursdays)
  * Fed funds target  - FRED DFEDTARU / DFEDTARL
  * WTI crude oil     - Yahoo CL=F -> FRED DCOILWTICO fallback

FRED is read from its public CSV download (fredgraph.csv) - no API key.

Rules baked in:
  * Anything that cannot be read is written as null -> the card shows
    "Unavailable". Never estimated, never carried forward.
  * If NOTHING can be read, the script exits non-zero and writes nothing,
    so a good file is never overwritten with an empty one.
  * Sanity bounds reject implausible values.

Run by .github/workflows/update-rates.yml - no human in the loop.
"""

import csv, io, json, os, sys, urllib.request
from datetime import datetime, timezone, timedelta

OUT = os.environ.get("MARKET_OUT", "market.json")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# series id -> (min, max) plausible band
FRED_SERIES = {
    "DGS10":        (0.0, 20.0),
    "DGS2":         (0.0, 20.0),
    "MORTGAGE30US": (0.5, 25.0),
    "MORTGAGE15US": (0.5, 25.0),
    "DFEDTARU":     (0.0, 20.0),
    "DFEDTARL":     (0.0, 20.0),
    "DCOILWTICO":   (-50.0, 400.0),
}


def get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fred(series_id: str):
    """Latest two valid observations from FRED's public CSV (no key)."""
    lo, hi = FRED_SERIES[series_id]
    start = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d")
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}"
    try:
        rows = list(csv.reader(io.StringIO(get(url).decode("utf-8", "replace"))))
    except Exception as e:
        print(f"::warning::FRED {series_id} unreachable: {e}")
        return None
    obs = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        date, val = row[0].strip(), row[1].strip()
        try:
            v = float(val)
        except ValueError:
            continue                       # "." or "" = no observation that day
        if lo <= v <= hi:
            obs.append({"date": date, "value": round(v, 4)})
    if not obs:
        print(f"::warning::FRED {series_id} returned no usable values")
        return None
    obs.sort(key=lambda o: o["date"], reverse=True)
    out = {"source": f"FRED {series_id}", "latest": obs[0],
           "previous": obs[1] if len(obs) > 1 else None}
    print(f"  FRED {series_id:<13} {obs[0]['value']:>9} as of {obs[0]['date']}")
    return out


def yahoo(symbol: str):
    """Yahoo chart meta (price, previous close, time). Often rate-limited on
    shared servers - that is fine, FRED is the fallback."""
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/"
               f"{urllib.request.quote(symbol)}?interval=1d&range=5d")
        try:
            meta = json.loads(get(url, 20))["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice")
            prev = meta.get("previousClose") or meta.get("chartPreviousClose")
            t = meta.get("regularMarketTime")
            if price and prev and t:
                when = datetime.fromtimestamp(t, timezone.utc).replace(microsecond=0).isoformat()
                print(f"  Yahoo {symbol:<7} {price} (prev {prev}) at {when}")
                return {"price": float(price), "previous_close": float(prev), "datetime": when}
        except Exception as e:
            print(f"::notice::Yahoo {symbol} via {host} unavailable: {e}")
    return None


def main() -> int:
    series = {sid: fred(sid) for sid in FRED_SERIES}

    # 10-Year: Yahoo intraday first, FRED daily close second.
    treasury = None
    y = yahoo("^TNX")
    if y:
        cur, prev = y["price"], y["previous_close"]
        if cur > 20:                       # older ^TNX feeds quote x10
            cur, prev = cur / 10, prev / 10
        if 0 < cur < 20 and 0 < prev < 20:
            treasury = {"source": "yahoo",
                        "latest": {"date": y["datetime"], "value": round(cur, 3)},
                        "previous": {"date": None, "value": round(prev, 3)}}
    if not treasury and series["DGS10"] and series["DGS10"]["previous"]:
        d = series["DGS10"]
        treasury = {"source": "fred", "latest": d["latest"], "previous": d["previous"]}

    # Oil: Yahoo front-month first, FRED daily spot second.
    oil = yahoo("CL=F")
    if oil:
        oil["source"] = "Yahoo Finance CL=F"
    elif series["DCOILWTICO"] and series["DCOILWTICO"]["previous"]:
        d = series["DCOILWTICO"]
        oil = {"price": d["latest"]["value"], "previous_close": d["previous"]["value"],
               "datetime": d["latest"]["date"], "source": "FRED DCOILWTICO (daily spot)"}

    found = sum(1 for v in series.values() if v) + (1 if treasury else 0) + (1 if oil else 0)
    if found == 0:
        print("::error::Nothing could be read. Leaving the existing market.json untouched.")
        return 1

    payload = {
        "updatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "updatedBy": "GitHub Actions (automatic)",
        "treasury10y": treasury,
        "oil": oil,
        "series": series,
    }
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    print(f"Wrote {OUT} - {found} of {len(FRED_SERIES) + 2} items read.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
