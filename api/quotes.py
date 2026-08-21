"""
Vercel Python serverless function: /api/quotes

Fetches live prices server-side from Yahoo Finance's public (unauthenticated)
chart endpoint - no API key needed, no CORS issue since this runs on the
server, not in the visitor's browser. Returns one clean JSON payload the
dashboard's frontend can consume directly.
"""

import json
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler

SYMBOLS = {
    "GLD": "GLD",
    "CRCL": "CRCL",
    "MSTR": "MSTR",
    "MSTU": "MSTU",
    "TSLA": "TSLA",
    "TIGER_SP500": "488500.KS",
    "TIGER_DIV": "458730.KS",
    "USDKRW": "KRW=X",
    "USDJPY": "JPY=X",
    "DXY": "DX-Y.NYB",
}

# Alternate hostnames Yahoo's unauthenticated chart API answers on - rotating
# across them, one request at a time (never in parallel), avoids tripping
# its per-connection burst rate limit.
HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# Best-effort last-known-good cache. Serverless instances get reused ("warm")
# across nearby requests, so this often survives a few invocations even though
# it is not guaranteed to persist - it costs nothing when it doesn't. Its job
# is to keep serving a real (if slightly stale) price during a transient
# Yahoo rate-limit/outage instead of a bare null.
_last_good = {}


def fetch_symbol(symbol, attempt=0):
    host = HOSTS[attempt % len(HOSTS)]
    url = "https://" + host + "/v8/finance/chart/" + symbol
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
        meta = data["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("previousClose") or meta.get("chartPreviousClose")
        ts = meta.get("regularMarketTime")
        chg_pct = None
        if price is not None and prev:
            chg_pct = (price - prev) / prev * 100
        result = {"price": price, "prevClose": prev, "changePct": chg_pct, "time": ts, "stale": False}
        if price is not None:
            _last_good[symbol] = result
        return result
    except urllib.error.HTTPError as e:
        if e.code == 429 and attempt < 2:
            time.sleep(0.35)
            return fetch_symbol(symbol, attempt + 1)
        if symbol in _last_good:
            stale = dict(_last_good[symbol])
            stale["stale"] = True
            return stale
        return {"price": None, "prevClose": None, "changePct": None, "time": None, "stale": True, "error": str(e)}
    except Exception as e:
        if symbol in _last_good:
            stale = dict(_last_good[symbol])
            stale["stale"] = True
            return stale
        return {"price": None, "prevClose": None, "changePct": None, "time": None, "stale": True, "error": str(e)}


def build_payload():
    # Sequential, not parallel: Yahoo's unofficial endpoint rate-limits bursts
    # of concurrent connections from one IP much more aggressively than a
    # steady stream of single requests.
    results = {}
    for key, sym in SYMBOLS.items():
        results[key] = fetch_symbol(sym)

    payload = {
        "quotes": {
            "GLD": results["GLD"]["price"],
            "CRCL": results["CRCL"]["price"],
            "MSTR": results["MSTR"]["price"],
            "MSTU": results["MSTU"]["price"],
            "TSLA": results["TSLA"]["price"],
        },
        "krwQuotes": {
            "TIGER 미국S&P500동일가중": results["TIGER_SP500"]["price"],
            "TIGER 미국배당다우존스": results["TIGER_DIV"]["price"],
        },
        "usdKrw": results["USDKRW"]["price"],
        "usdKrwChgPct": results["USDKRW"]["changePct"],
        "dxy": results["DXY"]["price"],
        "dxyChgPct": results["DXY"]["changePct"],
        "usdJpy": results["USDJPY"]["price"],
        "usdJpyChgPct": results["USDJPY"]["changePct"],
        "fetchedAt": int(time.time()),
        "usQuoteTime": results["TSLA"]["time"],
        "krQuoteTime": results["TIGER_SP500"]["time"],
    }

    usd_krw = results["USDKRW"]["price"]
    usd_jpy = results["USDJPY"]["price"]
    if usd_krw and usd_jpy:
        jpy_krw_100 = usd_krw / usd_jpy * 100
        payload["jpyKrw100"] = jpy_krw_100
        prev_krw = results["USDKRW"]["prevClose"]
        prev_jpy = results["USDJPY"]["prevClose"]
        if prev_krw and prev_jpy:
            prev_jpy_krw_100 = prev_krw / prev_jpy * 100
            payload["jpyKrwChgPct"] = (jpy_krw_100 - prev_jpy_krw_100) / prev_jpy_krw_100 * 100
        else:
            payload["jpyKrwChgPct"] = None
    else:
        payload["jpyKrw100"] = None
        payload["jpyKrwChgPct"] = None

    return payload


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            payload = build_payload()
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
        except Exception as e:
            body = json.dumps({"error": str(e)}).encode("utf-8")
            self.send_response(500)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "s-maxage=20, stale-while-revalidate=40")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
