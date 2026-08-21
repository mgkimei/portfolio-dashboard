"""
Vercel Python serverless function: /api/dxy

The US Dollar Index isn't directly available on Twelvedata's free tier, so
this computes it from the 6 major pairs in its official formula. That's 6
Twelvedata credits by itself, which combined with /api/quotes's 7 would blow
the 8-credit/minute free-tier cap if they ever land in the same minute - so
this lives in its own endpoint, polled far less often (minutes, not seconds)
by the frontend.
"""

import json
import os
import time
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler

TWELVEDATA_KEY = os.environ.get("TWELVEDATA_API_KEY", "dd706d353688442488eed00adca58530")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# ICE US Dollar Index formula components and their exponents.
PAIRS = ["EUR/USD", "USD/JPY", "GBP/USD", "USD/CAD", "USD/SEK", "USD/CHF"]

_last_good = {"dxy": None, "dxyChgPct": None}


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read())


def compute_dxy(eurusd, usdjpy, gbpusd, usdcad, usdsek, usdchf):
    return (
        50.14348112
        * (eurusd ** -0.576)
        * (usdjpy ** 0.136)
        * (gbpusd ** -0.119)
        * (usdcad ** 0.091)
        * (usdsek ** 0.042)
        * (usdchf ** 0.036)
    )


def build_payload():
    joined = ",".join(urllib.parse.quote(p, safe="") for p in PAIRS)
    url = "https://api.twelvedata.com/quote?symbol=" + joined + "&apikey=" + TWELVEDATA_KEY

    try:
        data = _get_json(url)
        closes, prevs = {}, {}
        for p in PAIRS:
            row = data.get(p)
            if not row or "close" not in row:
                raise ValueError("missing " + p)
            closes[p] = float(row["close"])
            prevs[p] = float(row["previous_close"]) if row.get("previous_close") else None

        dxy = compute_dxy(closes["EUR/USD"], closes["USD/JPY"], closes["GBP/USD"],
                           closes["USD/CAD"], closes["USD/SEK"], closes["USD/CHF"])

        dxy_chg_pct = None
        if all(prevs[p] is not None for p in PAIRS):
            dxy_prev = compute_dxy(prevs["EUR/USD"], prevs["USD/JPY"], prevs["GBP/USD"],
                                    prevs["USD/CAD"], prevs["USD/SEK"], prevs["USD/CHF"])
            dxy_chg_pct = (dxy - dxy_prev) / dxy_prev * 100

        _last_good["dxy"] = dxy
        _last_good["dxyChgPct"] = dxy_chg_pct
        return {"dxy": dxy, "dxyChgPct": dxy_chg_pct, "fetchedAt": int(time.time()), "stale": False}
    except Exception as e:
        return {"dxy": _last_good["dxy"], "dxyChgPct": _last_good["dxyChgPct"],
                "fetchedAt": int(time.time()), "stale": True, "error": str(e)}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = build_payload()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "s-maxage=280, stale-while-revalidate=120")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
