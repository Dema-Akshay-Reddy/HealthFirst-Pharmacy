"""Environment-driven configuration — single source of truth for paths & runtime knobs.

Every value defaults to the local hackathon layout; each can be overridden
without touching code (12-factor style), which is what the Docker image and
the test suite rely on.
"""
import logging
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Tiny stdlib .env loader (BASE_DIR/.env): KEY=VALUE lines.

    Real environment variables always win, so deployed overrides are safe.
    Mainly used for LLM credentials (OLLAMA_API_KEY / OPENAI_API_KEY / LLM_MODEL).
    """
    path = BASE_DIR / ".env"
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except OSError:
        pass


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() not in {"0", "false", "no", "off"}


# --- paths -------------------------------------------------------------------
# Mutable state (DB, uploads, backups) lives in DATA_DIR...
DATA_DIR = Path(os.environ.get("PHARMACY_DATA_DIR") or BASE_DIR / "data")
DB_PATH = Path(os.environ.get("PHARMACY_DB_PATH") or DATA_DIR / "pharmacy.db")
UPLOAD_DIR = Path(os.environ.get("PHARMACY_UPLOAD_DIR") or DATA_DIR / "uploads")
BACKUP_DIR = DATA_DIR / "backups"
# ...while read-only seed inputs are independent, so a volume-mounted DATA_DIR
# (Docker) can never shadow the bundled inventory dataset.
DATASET_DIR = Path(os.environ.get("PHARMACY_DATASET_DIR") or BASE_DIR / "data" / "zenith")

# --- server --------------------------------------------------------------------
HOST = (os.environ.get("HOST") or "127.0.0.1").strip() or "127.0.0.1"
try:
    PORT = int(os.environ.get("PORT") or 0) or 8000  # PORT=0/empty -> default 8000
except ValueError:
    PORT = 8000

# --- security / behaviour --------------------------------------------------------
ENV = (os.environ.get("PHARMACY_ENV") or "development").strip().lower()
DOCS_ENABLED = ENV != "production" or _bool("PHARMACY_DOCS", False)
SEED_ON_START = _bool("PHARMACY_SEED_ON_START", True)
# Laya weather features are opt-in. On the bundled 6-SKU dataset, validation
# showed weather improves timing/quantity accuracy but is worse on the primary
# due-within-7d flag, so the shipped model excludes them until more demand
# history exists. Toggling retrains automatically (feature-count guard).
LAYA_WEATHER_FEATURES = _bool("PHARMACY_LAYA_WEATHER", False)
CORS_ORIGINS = [o.strip() for o in (os.environ.get("PHARMACY_CORS_ORIGINS") or "").split(",") if o.strip()]


def current_api_key() -> str:
    """API key, read per-request so tests can toggle auth without re-importing.

    Empty/ unset -> API auth disabled (demo mode). Set PHARMACY_API_KEY to lock
    every /api endpoint behind `X-API-Key` (or `Authorization: Bearer`, or
    `?api_key=` for browser download links).
    """
    return (os.environ.get("PHARMACY_API_KEY") or "").strip()


# --- logging -----------------------------------------------------------------------
LOG_LEVEL = (os.environ.get("PHARMACY_LOG_LEVEL")
             or ("WARNING" if ENV == "production" else "INFO")).upper()


def setup_logging() -> None:
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Keep uvicorn's access log aligned with our level and format context.
    logging.getLogger("uvicorn.access").setLevel(LOG_LEVEL)
