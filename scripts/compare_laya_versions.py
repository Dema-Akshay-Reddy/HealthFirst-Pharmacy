"""Compare Laya predictions across scikit-learn versions.

The production pickle (laya.pkl) was TRAINED under sklearn 1.9.1, but the
deploy bundle pins 1.6.1. scikit-learn documents cross-version unpickling as
unsupported; this script PROVES whether the difference is real for this model
by running the exact same input states under both versions and diffing the
probability distributions.

Usage (two interpreters, same states file):
    python scripts/compare_laya_versions.py laya_states.json out_a.json   # env A
    python scripts/compare_laya_versions.py laya_states.json out_b.json   # env B
    python scripts/compare_laya_versions.py --diff out_a.json out_b.json
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("PHARMACY_DATA_DIR", str(ROOT / "data"))
os.environ.setdefault("PHARMACY_LAYA_WEATHER", "false")


def run(states_path: str, out_path: str) -> None:
    sys.path.insert(0, str(ROOT))
    from pharmacy import config
    config.LAYA_WEATHER_FEATURES = False
    from pharmacy import laya

    payload = json.loads(Path(states_path).read_text(encoding="utf-8"))
    rows = []
    for r in payload["rows"]:
        out = laya.predict(dict(r["state"]))
        rows.append({
            "drug": r["drug"],
            "decisions": out["decisions"],
            "uncertain": out.get("uncertain"),
            "invalid": out.get("invalid"),
        })
    import sklearn
    import numpy
    Path(out_path).write_text(json.dumps({
        "sklearn": sklearn.__version__, "numpy": numpy.__version__, "rows": rows,
    }, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    print(f"wrote {out_path} (sklearn {sklearn.__version__}, numpy {numpy.__version__}, {len(rows)} rows)")


def diff(a_path: str, b_path: str) -> int:
    a = json.loads(Path(a_path).read_text(encoding="utf-8"))
    b = json.loads(Path(b_path).read_text(encoding="utf-8"))
    print(f"A: sklearn {a['sklearn']} / numpy {a['numpy']}")
    print(f"B: sklearn {b['sklearn']} / numpy {b['numpy']}")
    if len(a["rows"]) != len(b["rows"]):
        print("ROW COUNT DIFFERS")
        return 2
    diffs = 0
    for ra, rb in zip(a["rows"], b["rows"], strict=True):
        assert ra["drug"] == rb["drug"], (ra["drug"], rb["drug"])
        if ra["decisions"] != rb["decisions"]:
            diffs += 1
            print(f"  DIFFERS: {ra['drug']}")
            for q in ra["decisions"]:
                da, db = ra["decisions"][q], rb["decisions"].get(q, {})
                if da != db:
                    print(f"    {q}: A={da}")
                    print(f"    {q}: B={db}")
    if diffs == 0:
        print("IDENTICAL: every decision value AND probability matches across versions")
        return 0
    print(f"{diffs} SKU(s) differ across scikit-learn versions")
    return 1


if __name__ == "__main__":
    if sys.argv[1] == "--diff":
        raise SystemExit(diff(sys.argv[2], sys.argv[3]))
    run(sys.argv[1], sys.argv[2])
