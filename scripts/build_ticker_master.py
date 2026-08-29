import csv
import json
import os
from collections import Counter
from pathlib import Path

TMP = Path(os.environ["TEMP"]) / "opencode"
OUT = Path("data") / "ticker_master.csv"

raw = json.loads((TMP / "company_tickers.json").read_text(encoding="utf-8"))
edgar = {}
for v in raw.values():
    t = v["ticker"].upper().replace(".", "-")
    edgar[t] = {"ticker": t, "cik": v["cik_str"], "company_name": v["title"].strip()}


def read_nasdaq(name):
    rows = []
    with (TMP / name).open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f, delimiter="|"):
            if r.get("Symbol") or r.get("ACT Symbol"):
                rows.append(r)
    return rows


def clean(sym):
    return sym.upper().replace(".", "-").replace("$", "-").replace("+", "-")


exch = {}
for r in read_nasdaq("nasdaqlisted.txt"):
    exch[clean(r["Symbol"])] = (
        {"Q": "NASDAQ Global Select", "G": "NASDAQ Global", "S": "NASDAQ Capital", "Z": "BATS"}.get(
            r.get("Market Category", ""), r.get("Market Category", "")
        ),
        r.get("ETF") == "Y",
        r.get("Test Issue") == "Y",
        r.get("Financial Status", ""),
    )
for r in read_nasdaq("otherlisted.txt"):
    sym = clean(r["ACT Symbol"])
    if sym not in exch:
        exch_name = {"A": "NYSE American", "P": "NYSE Arca", "N": "NYSE", "Z": "BATS"}.get(
            r.get("Exchange", ""), r.get("Exchange", "")
        )
        exch[sym] = (exch_name, r.get("ETF") == "Y", r.get("Test Issue") == "Y", "")

out = []
for t, e in sorted(edgar.items()):
    x = exch.get(t, ("", False, False, ""))
    out.append(
        {
            "ticker": t,
            "cik": e["cik"],
            "company_name": e["company_name"],
            "exchange": x[0],
            "is_etf": int(x[1]),
            "is_test_issue": int(x[2]),
            "status_flag": x[3],
        }
    )
out = [r for r in out if not r["is_test_issue"]]

with OUT.open("w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(out[0]))
    w.writeheader()
    w.writerows(out)

print("file:", OUT.resolve())
print("total:", len(out))
print("by exchange:", Counter(r["exchange"] or "UNLISTED" for r in out).most_common())
print("etfs:", sum(r["is_etf"] for r in out))
