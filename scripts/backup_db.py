"""Daily SQLite backup with rotation.

Copies the live database (path taken from DATABASE_URL in .env) into
~/backups/store-YYYYMMDD-HHMM.db using SQLite's online backup API (safe
while the app is running), and keeps the newest KEEP_COUNT copies.

Install as a daily cron job on the VPS:
    (crontab -l 2>/dev/null; echo "0 3 * * * /home/ubuntu/bondom_account/.venv/bin/python /home/ubuntu/bondom_account/scripts/backup_db.py >>/tmp/bondom-backup.log 2>&1") | crontab -
"""

import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared.config import settings  # noqa: E402

BACKUP_DIR = Path.home() / "backups"
KEEP_COUNT = 14


def db_path_from_url(url: str) -> Path:
    m = re.match(r"sqlite\+aiosqlite:///(.*)", url)
    if not m:
        raise SystemExit(f"Not a SQLite database URL, nothing to back up: {url}")
    raw = m.group(1)
    # sqlite:///relative/path  vs  sqlite:////absolute/path
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / raw
    return path


def main() -> None:
    src = db_path_from_url(settings.database_url)
    if not src.exists():
        raise SystemExit(f"Database not found: {src}")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    target = BACKUP_DIR / f"store-{stamp}.db"

    with sqlite3.connect(src) as source, sqlite3.connect(target) as dest:
        source.backup(dest)
    print(f"backup written: {target}")

    backups = sorted(BACKUP_DIR.glob("store-*.db"))
    for old in backups[:-KEEP_COUNT]:
        old.unlink()
        print(f"pruned old backup: {old}")


if __name__ == "__main__":
    main()
