"""Build exactly what the Worker will build: per-SKU state snapshots.

Dumps one state per catalogue drug via laya.state_from_db() to a JSON file,
so the service can be tested end-to-end before the Worker side exists.
Run:  python scripts/dump_laya_states.py  (writes /tmp/laya_states.json)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pharmacy import db, laya  # noqa: E402


def main() -> None:
    db.init_db()
    drugs = db.query("SELECT * FROM drugs ORDER BY category, name")
    rows = []
    for d in drugs:
        state = laya.state_from_db(dict(d))
        if state is None:
            continue  # same rule as the Worker side: no demand signal, no row
        rows.append({"drug_id": int(d["id"]), "drug": str(d["name"]), "state": state})
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "laya_states.json")
    out.write_text(json.dumps({"rows": rows}, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {len(rows)} states -> {out}")


if __name__ == "__main__":
    main()
