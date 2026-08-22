"""
Vercel Python serverless function: /api/quotes

Core, frequently-polled data: 5 US holdings + USD/KRW + USD/JPY via Twelvedata
(7 credits - Twelvedata's free tier caps at 8 credits/minute, so this must
stay under that on its own) plus the 2 KRW-native ETFs via Naver's public
mobile API (no key, no credit cost). The dollar index (DXY) is intentionally
NOT here - seeing it needs 5 more Twelvedata credits, which would blow the
per-minute cap - it lives in the separate, less-frequently-polled /api/dxy.
"""

import json
import os
import time
import urllib.request
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler

TWELVEDATA_KEY = os.environ.get("TWELVEDATA_API_KEY", "dd706d353688442488eed00adca58530")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

US_SYMBOLS = ["GLD", "CRCL", "MSTR", "MSTU", "TSLA"]
FX_SYMBOLS = ["USD/KRW", "USD/JPY"]
KR_ETFS = {"TIGER 미국S&P500동일가중": "488500", "TIGER 미국배당다우존스": "458730"}

_last_good = {}  # best-effort warm-instance cache, see note in build_payload()


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read())


def twelvedata_batch(symbols):
    """One batched call = one HTTP request, but still 1 credit per symbol."""
    joined = ",".join(urllib.parse.quote(s, safe="") for s in symbols)
    url = "https://api.twelvedata.com/quote?symbol=" + joined + "&apikey=" + TWELVEDATA_KEY
    out = {}
    try:
        data = _get_json(url)
        # single-symbol requests come back as one flat object instead of {symbol: {...}}
        if len(symbols) == 1:
            data = {symbols[0]: data}
        for sym in symbols:
            row = data.get(sym)
            if not row or row.get("status") == "error" or "close" not in row:
                out[sym] = {"price": None, "prevClose": None, "changePct": None, "time": None,
                            "error": (row or {}).get("message", "missing")}
                continue
            price = float(row["close"])
            prev = float(row["previous_close"]) if row.get("previous_close") else None
            chg = float(row["percent_change"]) if row.get("percent_change") not in (None, "") else None
            # last_quote_at is the actual moment this price tick was captured;
            # Twelvedata's "timestamp" field is a session/day marker, not live.
            out[sym] = {"price": price, "prevClose": prev, "changePct": chg, "time": row.get("last_quote_at") or row.get("timestamp")}
            _last_good[sym] = out[sym]
    except Exception as e:
        for sym in symbols:
            out[sym] = _last_good.get(sym) or {"price": None, "prevClose": None, "changePct": None, "time": None, "error": str(e)}
    return out


def naver_quote(code):
    url = "https://m.stock.naver.com/api/stock/" + code + "/basic"
    key = "naver:" + code
    try:
        data = _get_json(url)
        price = float(data["closePrice"].replace(",", ""))
        chg_pct = float(data["fluctuationsRatio"])
        result = {"price": price, "changePct": chg_pct, "time": data.get("localTradedAt")}
        _last_good[key] = result
        return result
    except Exception as e:
        return _last_good.get(key) or {"price": None, "changePct": None, "time": None, "error": str(e)}


def build_payload():
    us = twelvedata_batch(US_SYMBOLS)
    fx = twelvedata_batch(FX_SYMBOLS)
    kr = {name: naver_quote(code) for name, code in KR_ETFS.items()}

    payload = {
        "quotes": {sym: us[sym]["price"] for sym in US_SYMBOLS},
        "krwQuotes": {name: kr[name]["price"] for name in KR_ETFS},
        "usdKrw": fx["USD/KRW"]["price"],
        "usdKrwChgPct": fx["USD/KRW"]["changePct"],
        "usdJpy": fx["USD/JPY"]["price"],
        "usdJpyChgPct": fx["USD/JPY"]["changePct"],
        "fetchedAt": int(time.time()),
        "usQuoteTime": us["TSLA"]["time"],
        "krQuoteTime": kr["TIGER 미국S&P500동일가중"]["time"],
    }

    usd_krw, usd_jpy = payload["usdKrw"], payload["usdJpy"]
    if usd_krw and usd_jpy:
        payload["jpyKrw100"] = usd_krw / usd_jpy * 100
        prev_krw, prev_jpy = fx["USD/KRW"]["prevClose"], fx["USD/JPY"]["prevClose"]
        if prev_krw and prev_jpy:
            prev_100 = prev_krw / prev_jpy * 100
            payload["jpyKrwChgPct"] = (payload["jpyKrw100"] - prev_100) / prev_100 * 100
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
        # Matches the frontend's 15-minute poll interval - Vercel's edge cache
        # serves this to every viewer/tab in that window from one upstream
        # call, which is what actually keeps daily credit usage sustainable
        # regardless of how many people have the page open.
        self.send_header("Cache-Control", "s-maxage=840, stale-while-revalidate=120")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
