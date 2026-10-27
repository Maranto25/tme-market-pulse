#!/usr/bin/env python3
"""
TME Market Pulse - automatic market.json updater (v2).

Runs on GitHub's servers (no CORS, no keys in the page) and writes
market.json for the live dashboard.

Sources, in order (first one that works wins):
  * 10-Year + 2-Year Treasury - U.S. Treasury daily par yield curve CSV (no key)
                                -> FRED API DGS10 / DGS2 (needs FRED_API_KEY)
  * Freddie Mac PMMS 30 / 15  - Freddie Mac PMMS history CSV (no key)
                                -> FRED API MORTGAGE30US / MORTGAGE15US
  * Fed funds target range    - FRED API DFEDTARU / DFEDTARL (needs FRED_API_KEY)
  * WTI crude oil             - Yahoo CL=F -> FRED API DCOILWTICO

v2: v1 used FRED's fredgraph.csv download and Yahoo, both of which block
GitHub's shared servers (timeouts / HTTP 429). Treasury.gov and Freddie Mac
publish their own data as plain CSV files, and the FRED API (with a key kept
in the repo secret FRED_API_KEY) is built for automated use.

Rules baked in:
  * Anything that cannot be read is written as null -> the card shows
    "Unavailable". Never estimated, never carried forward.
  * If NOTHING can be read, the script exits non-zero and writes nothing,
    so a good file is never overwritten with an empty one.
  * Sanity bounds reject implausible values.
  * On a source failure the log prints what came back, so the next fix
    needs no re-run.

Run by .github/workflows/update-rates.yml - no human in the loop.
"""

import csv, io, json, os, sys, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

OUT = os.environ.get("MARKET_OUT", "market.json")
FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
TIMEOUT = 20
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

BANDS = {
    "DGS10": (0.0, 20.0), "DGS2": (0.0, 20.0),
    "MORTGAGE30US": (0.5, 25.0), "MORTGAGE15US": (0.5, 25.0),
    "DFEDTARU": (0.0, 20.0), "DFEDTARL": (0.0, 20.0),
    "DCOILWTICO": (-50.0, 400.0),
}


def log(kind, msg):
    print(f"::{kind}::{msg}" if kind else msg, flush=True)


def get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8-sig", "replace")


def to_iso(s: str):
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def pack(series_id, source, obs):
    """obs = list of (iso_date, float). Keeps the band, newest first."""
    lo, hi = BANDS[series_id]
    obs = sorted({d: v for d, v in obs if d and lo <= v <= hi}.items(), reverse=True)
    if not obs:
        return None
    out = {"source": source,
           "latest": {"date": obs[0][0], "value": round(obs[0][1], 4)},
           "previous": {"date": obs[1][0], "value": round(obs[1][1], 4)} if len(obs) > 1 else None}
    log("", f"  {series_id:<13} {out['latest']['value']:>9} as of {out['latest']['date']}  ({source})")
    return out


def num(s):
    try:
        return float(str(s).strip())
    except ValueError:
        return None


# ---------- U.S. Treasury daily par yield curve (no key) ----------
def treasury_curve():
    """Returns {'DGS10': packed, 'DGS2': packed} from treasury.gov, or {}."""
    rows = []
    year = datetime.now(timezone.utc).year
    for y in (year, year - 1):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
               f"daily-treasury-rates.csv/{y}/all?type=daily_treasury_yield_curve"
               f"&field_tdr_date_value={y}&page&_format=csv")
        try:
            text = get(url)
        except Exception as e:
            log("warning", f"Treasury.gov {y} unreachable: {e}")
            continue
        r = list(csv.DictReader(io.StringIO(text)))
        if not r:
            log("warning", f"Treasury.gov {y}: no rows. First 200 chars: {text[:200]!r}")
            continue
        rows += r
        if len(rows) >= 2:
            break
    if not rows:
        return {}
    keys = {k.strip().lstrip('\ufeff').lower(): k for k in rows[0].keys() if k}
    date_k = keys.get("date")
    ten_k, two_k = keys.get("10 yr"), keys.get("2 yr")
    if not (date_k and ten_k and two_k):
        log("warning", f"Treasury.gov header not recognised: {list(rows[0].keys())}")
        return {}
    ten = [(to_iso(r[date_k]), num(r[ten_k])) for r in rows]
    two = [(to_iso(r[date_k]), num(r[two_k])) for r in rows]
    src = "U.S. Treasury daily par yield curve"
    out = {}
    p = pack("DGS10", src, [(d, v) for d, v in ten if v is not None])
    if p: out["DGS10"] = p
    p = pack("DGS2", src, [(d, v) for d, v in two if v is not None])
    if p: out["DGS2"] = p
    return out


# ---------- Freddie Mac PMMS history (no key) ----------
def freddie_pmms():
    url = "https://www.freddiemac.com/pmms/docs/PMMS_history.csv"
    try:
        text = get(url)
    except Exception as e:
        log("warning", f"Freddie Mac PMMS unreachable: {e}")
        return {}
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 3:
        log("warning", f"Freddie Mac PMMS: too few rows. First 200 chars: {text[:200]!r}")
        return {}
    head = [h.strip().lstrip('\ufeff').lower() for h in rows[0]]
    def col(*names):
        for n in names:
            if n in head:
                return head.index(n)
        return None
    di = col("date", "week")
    c30 = col("pmms30", "30yr", "30-yr", "frm30")
    c15 = col("pmms15", "15yr", "15-yr", "frm15")
    if di is None or c30 is None:
        log("warning", f"Freddie Mac header not recognised: {rows[0]}")
        return {}
    data = [r for r in rows[1:] if len(r) > di]
    out = {}
    src = "Freddie Mac PMMS"
    obs30 = [(to_iso(r[di]), num(r[c30])) for r in data if len(r) > c30]
    p = pack("MORTGAGE30US", src, [(d, v) for d, v in obs30 if v is not None])
    if p: out["MORTGAGE30US"] = p
    if c15 is not None:
        obs15 = [(to_iso(r[di]), num(r[c15])) for r in data if len(r) > c15]
        p = pack("MORTGAGE15US", src, [(d, v) for d, v in obs15 if v is not None])
        if p: out["MORTGAGE15US"] = p
    return out


# ---------- FRED API (needs FRED_API_KEY repo secret) ----------
def fred_api(series_id):
    if not FRED_KEY:
        return None
    q = urllib.parse.urlencode({"series_id": series_id, "api_key": FRED_KEY, "file_type": "json",
                                "sort_order": "desc", "limit": 10})
    try:
        data = json.loads(get("https://api.stlouisfed.org/fred/series/observations?" + q))
    except Exception as e:
        log("warning", f"FRED API {series_id} unavailable: {e}")
        return None
    obs = [(o.get("date"), num(o.get("value"))) for o in data.get("observations", [])]
    return pack(series_id, f"FRED {series_id}", [(d, v) for d, v in obs if v is not None])


# ---------- Yahoo (often rate-limited on shared servers) ----------
def yahoo(symbol):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?interval=1d&range=5d")
    try:
        meta = json.loads(get(url))["chart"]["result"][0]["meta"]
        price, prev, t = meta.get("regularMarketPrice"), meta.get("previousClose") or meta.get("chartPreviousClose"), meta.get("regularMarketTime")
        if price and prev and t:
            when = datetime.fromtimestamp(t, timezone.utc).replace(microsecond=0).isoformat()
            log("", f"  Yahoo {symbol:<7} {price} (prev {prev}) at {when}")
            return {"price": float(price), "previous_close": float(prev), "datetime": when}
    except Exception as e:
        log("notice", f"Yahoo {symbol} unavailable: {e}")
    return None


def main() -> int:
    if not FRED_KEY:
        log("notice", "FRED_API_KEY secret not set - Fed funds and oil fall back or stay null.")

    series = {sid: None for sid in BANDS}
    series.update(treasury_curve())
    series.update(freddie_pmms())
    for sid in BANDS:
        if series[sid] is None:
            series[sid] = fred_api(sid)

    treasury = None
    if series["DGS10"] and series["DGS10"]["previous"]:
        d = series["DGS10"]
        treasury = {"source": "fred", "label": d["source"],   # "fred" = daily close (page wording)
                    "latest": d["latest"], "previous": d["previous"]}

    oil = yahoo("CL=F")
    if oil:
        oil["source"] = "Yahoo Finance CL=F"
    elif series["DCOILWTICO"] and series["DCOILWTICO"]["previous"]:
        d = series["DCOILWTICO"]
        oil = {"price": d["latest"]["value"], "previous_close": d["previous"]["value"],
               "datetime": d["latest"]["date"], "source": "FRED DCOILWTICO (daily spot)"}

    found = sum(1 for v in series.values() if v) + (1 if oil else 0)
    if found == 0:
        log("error", "Nothing could be read. Leaving the existing market.json untouched.")
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
    missing = [k for k, v in series.items() if not v] + ([] if oil else ["oil"])
    log("", f"Wrote {OUT}. Missing: {', '.join(missing) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
