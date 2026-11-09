"""Vectorised technical indicators used by the signal engine.

Everything here is pure pandas/numpy so the same functions can be used by the
live scanner and the backtester (one source of truth -> no look-ahead drift).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    # Wilder smoothing
    au = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = au / ad.replace(0.0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(50.0)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    mac = ema(close, fast) - ema(close, slow)
    sig = mac.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return mac, sig, mac - sig


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff()
    dn = -l.diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr_ = tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    pdi = 100 * pd.Series(plus, index=df.index).ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr_
    mdi = 100 * pd.Series(minus, index=df.index).ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr_
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean().fillna(0.0)


def stdev_channel(close: pd.Series, n: int = 20, k: float = 2.0):
    m = sma(close, n)
    sd = close.rolling(n, min_periods=n).std()
    return m - k * sd, m, m + k * sd


def volume_zscore(vol: pd.Series, n: int = 50) -> pd.Series:
    m = vol.rolling(n, min_periods=10).mean()
    sd = vol.rolling(n, min_periods=10).std()
    return ((vol - m) / sd.replace(0, np.nan)).fillna(0.0)


def rolling_slope(s: pd.Series, n: int = 10) -> pd.Series:
    """Normalised linear slope (percent per bar) over n bars."""
    def _f(w: np.ndarray) -> float:
        x = np.arange(len(w), dtype=float)
        b = np.polyfit(x, w, 1)[0]
        mean = np.mean(w)
        return (b / mean * 100.0) if mean else 0.0

    return s.rolling(n, min_periods=n).apply(_f, raw=True).fillna(0.0)


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Attach every indicator the engine needs to an OHLCV frame."""
    df = df.copy()
    c = df["close"]
    df["ema20"] = ema(c, 20)
    df["ema50"] = ema(c, 50)
    df["ema200"] = ema(c, 200)
    df["rsi"] = rsi(c, 14)
    mac, sig, hist = macd(c)
    df["macd"], df["macd_sig"], df["macd_hist"] = mac, sig, hist
    df["atr"] = atr(df, 14)
    df["adx"] = adx(df, 14)
    df["vol_z"] = volume_zscore(df["volume"])
    df["bb_low"], df["bb_mid"], df["bb_up"] = stdev_channel(c, 20, 2.0)
    df["slope"] = rolling_slope(c, 10)
    df["atr_pct"] = df["atr"] / c * 100.0
    df["body"] = (df["close"] - df["open"]).abs()
    df["range"] = (df["high"] - df["low"]).replace(0, np.nan)
    df["body_ratio"] = (df["body"] / df["range"]).fillna(0.0)
    df["dir"] = np.sign(df["close"] - df["open"]).fillna(0.0)
    return df


def to_py(o):
    """Recursively convert numpy / pandas scalars into plain JSON-safe Python."""
    import math

    if isinstance(o, dict):
        return {str(k): to_py(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [to_py(v) for v in o]
    mod = type(o).__module__
    if mod.startswith("numpy") or mod.startswith("pandas"):
        try:
            o = o.item() if hasattr(o, "item") else bool(o)
        except Exception:
            return None
    if isinstance(o, float) and not math.isfinite(o):
        return None
    return o


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate lower timeframe candles into a higher timeframe (left-labelled)."""
    o = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return o.dropna(subset=["open", "high", "low", "close"])
