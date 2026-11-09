"""Builds multi-timeframe structural context for live scanning and backtests."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import data, indicators as I, structure as S

LIVE_TFS = {"1d": 420, "4h": 520, "1h": 700}
RESAMPLE_RULE = {"1h": "1h", "4h": "4h", "1d": "1D"}


def load_live(symbol: str) -> tuple[dict[str, S.StructureState], dict[str, int]]:
    tfs: dict[str, S.StructureState] = {}
    idx: dict[str, int] = {}
    for tf, lim in LIVE_TFS.items():
        df = I.enrich(data.fetch_ohlcv(symbol, tf, lim))
        tfs[tf] = S.build(df, k=2)
        idx[tf] = len(df) - 1
    return tfs, idx


def load_from_base(base: pd.DataFrame, base_tf: str = "15m") -> tuple[dict[str, S.StructureState], dict]:
    """Derive higher timeframes from one base series (used by the backtester).

    Returns ``(states, idx)`` where ``idx`` maps base bar -> last *closed* bar of
    each higher timeframe, so no future information leaks into the evaluation.
    """
    base = I.enrich(base)
    states: dict[str, S.StructureState] = {base_tf: S.build(base, k=2)}
    idx = {base_tf: np.arange(len(base))}
    for tf, rule in RESAMPLE_RULE.items():
        if tf == base_tf:
            continue
        htf = I.enrich(I.resample(base, rule))
        states[tf] = S.build(htf, k=2)
        times = htf.index.values
        bt = base.index.values
        pos = np.searchsorted(times, bt, side="right") - 1   # bar containing t
        idx[tf] = np.clip(pos - 1, 0, len(htf) - 1)          # last fully closed
    return states, idx


def snapshot_context(states: dict[str, S.StructureState], idx: dict[str, int],
                     limit: int = 170, tf: str = "1h") -> dict:
    """Chart payload for the dashboard: candles + live zones/levels/EMAs."""
    st = states.get(tf) or states["1h"]
    df = st.df.tail(limit)
    i = idx.get(tf, len(st.df) - 1)
    offset = len(st.df) - len(df)
    zones = []
    for z in st.zones:
        if z.idx > i or (z.filled is not None and z.filled <= i) or i - z.idx > 300:
            continue
        zones.append({"kind": z.kind, "dir": int(z.dir),
                      "top": float(z.top), "bot": float(z.bot),
                      "idx": max(0, int(z.idx) - offset), "age": int(i - z.idx)})
    zones = zones[-16:]
    candles = [[int(ts.value // 10**6), float(o), float(h), float(l), float(c), float(v)]
               for ts, o, h, l, c, v in zip(df.index, df["open"], df["high"],
                                            df["low"], df["close"], df["volume"])]
    return {
        "tf": tf,
        "candles": candles,
        "zones": zones,
        "ema20": [float(x) if np.isfinite(x) else None for x in df["ema20"].tolist()],
        "ema50": [float(x) if np.isfinite(x) else None for x in df["ema50"].tolist()],
        "sweeps": [{"idx": j - offset, "dir": int(st.sweep_dir[j]),
                    "level": float(st.sweep_level[j])}
                   for j in range(max(0, i - 150), i + 1)
                   if st.sweep_dir[j] != 0 and np.isfinite(st.sweep_level[j])],
    }
