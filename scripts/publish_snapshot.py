#!/usr/bin/env python3
"""Generate a fresh REAL snapshot and write it into docs/data (+ app/data mirror)."""
from __future__ import annotations
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import snapshot_server as S  # noqa: E402


def app_meta(d: dict, source: str, status: str) -> dict:
    g = d.get("generated_at", "")
    try:
        t = dt.datetime.strptime(g, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        nxt = (t + dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        day, hhmm = t.strftime("%Y-%m-%d"), t.strftime("%H%M")
    except Exception:
        nxt, day, hhmm = "", "1970-01-01", "0000"
    sigs = d.get("signals", [])
    return {
        "snapshot_id": g.replace(":", "-"),
        "source": source,
        "freshness": "near-live",
        "status": status,
        "generated_at": g,
        "next_expected_at": nxt,
        "signal_count": len(sigs),
        "error_count": len(d.get("errors", [])),
        "signal_ids": [f"{x.get('symbol')}|{x.get('ts')}|{x.get('direction')}|{x.get('entry')}" for x in sigs],
        "_day": day,
        "_hhmm": hhmm,
    }


def write_json(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def main():
    d = S.build_payload(20, False, 0.30)          # shipped engine, real ccxt
    m = app_meta(d, "github-actions", "live")
    d["app_meta"] = {k: v for k, v in m.items() if not k.startswith("_")}

    targets = [ROOT / "docs" / "data"]
    mirror = ROOT / "app" / "data"
    if mirror.parent.exists():
        targets.append(mirror)

    for base in targets:
        write_json(base / "latest.json", d)
        rel = f"{m['_day']}/{m['_hhmm']}.json"
        write_json(base / "history" / rel, d)
        idx_path = base / "history" / "index.json"
        try:
            idx = json.loads(idx_path.read_text(encoding="utf-8"))
        except Exception:
            idx = {"entries": []}
        entry = {
            "file": rel,
            "generated_at": d["generated_at"],
            "signal_count": m["signal_count"],
            "error_count": m["error_count"],
            "source": "github-actions",
        }
        entries = [e for e in idx.get("entries", []) if e.get("file") != rel]
        entries.insert(0, entry)
        idx["entries"] = entries[:50]
        write_json(idx_path, idx)

    print(f"[publish] {d['generated_at']} | signals={m['signal_count']} "
          f"errors={m['error_count']} | dirs={[str(t) for t in targets]}")


if __name__ == "__main__":
    main()
