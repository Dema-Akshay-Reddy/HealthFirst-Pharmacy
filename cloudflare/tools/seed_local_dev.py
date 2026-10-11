"""Seed the LOCAL miniflare D1 sqlite with a test admin + session secret.

Runs only against the local .wrangler/state sqlite — never the remote DBs.
Used to exercise the split-read session routing offline (zero remote quota).
"""
import glob
import sqlite3
import sys
from pathlib import Path

import numpy  # noqa: F401  (ensure env intact)

BASE = Path(__file__).resolve().parent.parent / ".wrangler/state/v3/d1/miniflare-D1DatabaseObject"


def pbkdf2_hash(password: str) -> str:
    import hashlib
    salt = bytes.fromhex("00" * 16)  # deterministic for a test seed
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000, dklen=32)
    return "pbkdf2$100000$" + salt.hex() + "$" + dk.hex()


def main() -> None:
    db_files = [p for p in glob.glob(str(BASE / "*.sqlite")) if "metadata" not in p]
    if not db_files:
        print("no local D1 files found — run wrangler dev once first")
        return
    # two d1 files: primary / secondary by wrangler's internal uuid map;
    # seed BOTH with the same test data for local parity.
    for path in db_files:
        con = sqlite3.connect(path)
        cur = con.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO users(username, password_hash, role, name, created_at) "
            "VALUES('admin', ?, 'admin', 'Administrator', datetime('now'))", (pbkdf2_hash("123456"),))
        cur.execute(
            "INSERT OR IGNORE INTO settings(key, value, updated_at) "
            "VALUES('session_secret', 'localseedsecret0123456789abcdef0123456789abcdef', datetime('now'))")
        for d in [("Dolo 650", "dolo 650", 1), ("Allegra 120", "allegra 120", 2),
                  ("Azithral 500", "azithral 500", 3), ("Glycomet 500", "glycomet 500", 4),
                  ("Telma 40", "telma 40", 5), ("Pan 40", "pan 40", 6)]:
            cur.execute(
                "INSERT OR IGNORE INTO drugs(id, name, norm_name, unit, mrp, cost, lead_time_days, created_at) "
                "VALUES(?,?,?,'unit',0,0,7,datetime('now'))", (d[2], d[0], d[1]))
        con.commit()
        con.close()
        print("seeded:", Path(path).name)
    print("local seeds done — test admin is admin/123456")


if __name__ == "__main__":
    sys.exit(main())
