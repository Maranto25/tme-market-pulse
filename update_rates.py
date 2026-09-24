#!/usr/bin/env python3
"""
TME Market Pulse — automatic rates.json updater.

Fetches the six first-mortgage national averages from Mortgage News Daily
(the project's exclusive source for these rates) and writes rates.json.

Compliance rules baked in:
  * A product that cannot be parsed is written as null -> the dashboard
    renders "Data Pending". Never estimated, never carried forward.
  * If NOTHING parses (site redesign / outage), the script exits non-zero
    and writes nothing, so a good file is never overwritten with garbage.
  * Sanity bounds reject implausible values before they can be published.

Run by .github/workflows/update-rates.yml — no human in the loop.
"""

import json, os, re, sys, urllib.request
from datetime import datetime, timezone, timedelta

URL = "https://www.mortgagenewsdaily.com/mortgage-rates"
OUT = os.environ.get("RATES_OUT", "rates.json")

# key -> the label exactly as Mortgage News Daily prints it
PRODUCTS = [
    ("fixed30", "30 Yr. Fixed"),
    ("fixed15", "15 Yr. Fixed"),
    ("fha30",   "30 Yr. FHA"),
    ("va30",    "30 Yr. VA"),
    ("jumbo30", "30 Yr. Jumbo"),
    ("arm76",   "7/6 SOFR ARM"),
]

RATE_MIN, RATE_MAX = 0.50, 25.00      # plausible mortgage rate band
CHANGE_MAX = 3.00                      # a >3pt single-day move is a parse error


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode("utf-8", "replace")


def to_text(html: str) -> str:
    """Strip scripts/styles/tags and normalise whitespace, so the parser reads
    the page the way a person does and survives most markup changes."""
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", "\n", html)
    html = (html.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
    return re.sub(r"[ \t\r\f\v]+", " ", html)


def parse_rate(text: str, label: str):
    """Find '<label> ... X.XX% ... Change: ±Y.YY' within a tight window."""
    pat = (re.escape(label) + r"\s*(?:" + re.escape(label) + r"\s*)?"
           r"(\d{1,2}\.\d{2})\s*%.{0,60}?Change:\s*([+-]?\d{1,2}\.\d{2})")
    for m in re.finditer(pat, text, re.S):
        rate, change = float(m.group(1)), float(m.group(2))
        if RATE_MIN <= rate <= RATE_MAX and abs(change) <= CHANGE_MAX:
            return round(rate, 2), round(change, 2)
    return None, None


def parse_as_of(text: str) -> str:
    """Prefer the date Mortgage News Daily stamps on the page."""
    for pat in (r"as of\s+(\d{1,2})/(\d{1,2})/(\d{2,4})",
                r"(\d{1,2})/(\d{1,2})/(\d{2,4})\s+Mortgage News Daily"):
        m = re.search(pat, text, re.I)
        if m:
            mo, d, y = (int(g) for g in m.groups())
            y = y + 2000 if y < 100 else y
            try:
                return datetime(y, mo, d).strftime("%Y-%m-%d")
            except ValueError:
                pass
    # Fallback: today's date in US Eastern (where the index is published)
    return (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%d")


def main() -> int:
    try:
        text = to_text(fetch(URL))
    except Exception as e:
        print(f"::error::Could not reach Mortgage News Daily: {e}")
        return 1

    rates, found = {}, 0
    for key, label in PRODUCTS:
        rate, change = parse_rate(text, label)
        if rate is None:
            print(f"::warning::{label} not parsed - writing null (Data Pending)")
            rates[key] = {"rate": None, "change": None}
        else:
            print(f"  {label:<14} {rate:>6.2f}%  change {change:+.2f}")
            rates[key] = {"rate": rate, "change": change}
            found += 1

    if found == 0:
        print("::error::Parsed 0 of 6 rates - Mortgage News Daily layout may have "
              "changed. Leaving the existing rates.json untouched.")
        return 1

    payload = {
        "source": "Mortgage News Daily",
        "asOf": parse_as_of(text),
        "updatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "updatedBy": "GitHub Actions (automatic)",
        "rates": rates,
    }
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    print(f"Wrote {OUT} - as of {payload['asOf']}, {found} of 6 products reported.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
