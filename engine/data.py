"""Data layer: CCXT market data with exchange fallback and pagination.

Live sandbox note: binance.com geoblocks this host (HTTP 451) and bybit returns
403, so the default routing is bitget -> mexc. Adding another ccxt id is enough
to extend the fallback chain.
"""
from __future__ import annotations

import time
from typing import Iterable

import pandas as pd

import ccxt

EXCHANGE_CHAIN = ("bitget", "mexc")

# liquid majors used when the ticker endpoint is unavailable
FALLBACK_UNIVERSE = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT",
    "BNB/USDT:USDT", "DOGE/USDT:USDT", "ADA/USDT:USDT", "AVAX/USDT:USDT",
    "LINK/USDT:USDT", "TON/USDT:USDT", "NEAR/USDT:USDT", "SUI/USDT:USDT",
]

TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "12h": 43_200_000, "1d": 86_400_000,
}

_exchanges: dict[str, ccxt.Exchange] = {}
_cache: dict[tuple, tuple[float, pd.DataFrame]] = {}
TTL = 45  # seconds


def exchange(name: str) -> ccxt.Exchange:
    if name not in _exchanges:
        _exchanges[name] = getattr(ccxt, name)({
            "options": {"defaultType": "swap"},
            "enableRateLimit": True,
            "timeout": 25_000,
        })
    return _exchanges[name]


def _to_df(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts").sort_values("ts")
    df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("time")[["open", "high", "low", "close", "volume"]].astype(float)


def fetch_ohlcv(symbol: str, tf: str, limit: int = 500,
                drop_partial: bool = True) -> pd.DataFrame:
    """Fetch ``limit`` closed candles, paginating backwards if needed."""
    key = (symbol, tf, limit, drop_partial)
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < TTL:
        return hit[1]

    last_err: Exception | None = None
    span = TF_MS.get(tf, 60_000)
    for name in EXCHANGE_CHAIN:
        try:
            ex = exchange(name)
            rows: list = []
            oldest = None
            since = None
            guard = 0
            while len({r[0] for r in rows}) < limit and guard < 80:
                guard += 1
                chunk = ex.fetch_ohlcv(symbol, tf, since=since, limit=1000)
                if not chunk:
                    break
                rows = chunk + rows
                oldest_new = min(r[0] for r in chunk)
                if oldest is not None and oldest_new >= oldest:
                    break                      # exchange has no older history
                oldest = oldest_new
                since = oldest - 1000 * span
            if not rows:
                raise RuntimeError("empty response")
            df = _to_df(rows)
            if drop_partial:
                if len(df) > 1 and (time.time() * 1000 - df.index[-1].value / 1e6) < span:
                    df = df.iloc[:-1]
            df = df.tail(limit)
            _cache[key] = (now, df)
            return df
        except Exception as e:  # try the next venue
            last_err = e
            continue
    raise RuntimeError(f"no data for {symbol} {tf}: {last_err}")


def top_symbols(limit: int = 12, min_quote_volume: float = 20_000_000) -> list[str]:
    """Most liquid USDT perpetuals, filtered to tradeable majors/large caps."""
    for name in EXCHANGE_CHAIN:
        try:
            ex = exchange(name)
            ex.load_markets()
            tickers = ex.fetch_tickers()
            rows = []
            for sym, t in tickers.items():
                if not sym.endswith(":USDT"):
                    continue
                mk = ex.markets.get(sym) or {}
                info = mk.get("info") or {}
                # skip tokenised equities / RWAs and delisted contracts
                if str(info.get("isRwa", "NO")).upper() == "YES":
                    continue
                if str(info.get("symbolStatus", "normal")) != "normal":
                    continue
                qv = t.get("quoteVolume") or 0.0
                if qv >= min_quote_volume and t.get("last"):
                    rows.append((sym, qv))
            allrows = []
            for sym, t in tickers.items():
                if not sym.endswith(":USDT"):
                    continue
                mk = ex.markets.get(sym) or {}
                info = mk.get("info") or {}
                if str(info.get("isRwa", "NO")).upper() == "YES":
                    continue
                if str(info.get("symbolStatus", "normal")) != "normal":
                    continue
                qv = t.get("quoteVolume") or 0.0
                if t.get("last") and qv > 0:
                    allrows.append((sym, qv))
            allrows.sort(key=lambda x: -x[1])
            if allrows:
                # keep only liquid names, but never return fewer than `limit`
                liquid = [s for s, qv in allrows if qv >= min_quote_volume]
                return (liquid or [s for s, _ in allrows])[:limit] if len(liquid) >= limit \
                    else [s for s, _ in allrows[:limit]]
        except Exception:
            continue
    return FALLBACK_UNIVERSE[:limit]


def last_price(symbol: str) -> float:
    for name in EXCHANGE_CHAIN:
        try:
            t = exchange(name).fetch_ticker(symbol)
            if t.get("last"):
                return float(t["last"])
        except Exception:
            continue
    return float("nan")
