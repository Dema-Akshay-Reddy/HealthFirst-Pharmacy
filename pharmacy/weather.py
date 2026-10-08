"""Hyderabad weather features for Laya.

Weather is an exogenous feature, never a hard rule. Historical weather is
fetched from the Open-Meteo archive (keyless) for training-state backfill and
cached on disk so training is reproducible; a deterministic IMD-climatology
fallback keeps the pipeline working offline. Forward forecast uses Open-Meteo
when online, else climatology ("no anomaly" baseline).
"""
import json
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from .config import DATA_DIR

LAT, LON = 17.385, 78.4867  # Hyderabad
CACHE_DIR = DATA_DIR / "weather_cache"
ARCHIVE_URL = ("https://archive-api.open-meteo.com/v1/archive"
               "?latitude={lat}&longitude={lon}&start_date={start}&end_date={end}"
               "&daily=temperature_2m_mean,temperature_2m_max,temperature_2m_min,"
               "relative_humidity_2m_mean,precipitation_sum&timezone=Asia%2FKolkata")
FORECAST_URL = ("https://api.open-meteo.com/v1/forecast"
                "?latitude={lat}&longitude={lon}"
                "&daily=temperature_2m_mean,precipitation_sum,"
                "precipitation_probability_max&forecast_days=7&timezone=Asia%2FKolkata")

# IMD Hyderabad monthly climatology (1991-2020 normals, approximated):
# mean temp C, total rainfall mm. Deterministic offline fallback only.
CLIMATOLOGY = {  # month -> (temp_mean_c, rain_mm_month)
    1: (22.8, 3.4), 2: (25.4, 5.6), 3: (28.6, 15.1), 4: (31.3, 21.6),
    5: (33.2, 38.5), 6: (30.5, 122.5), 7: (28.2, 176.9), 8: (27.8, 167.3),
    9: (27.5, 141.1), 10: (26.3, 85.3), 11: (24.2, 21.7), 12: (22.3, 6.1),
}


def _climatology_daily(d: date) -> dict:
    t, rain = CLIMATOLOGY[d.month]
    return dict(temp_mean=t, temp_max=t + 4.5, temp_min=t - 5.0,
                humidity=55.0, rain=rain / 30.0)


def _http_json(url: str, timeout: float = 15.0) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "laya-weather/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _cache_path(start: str, end: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"hyd_{start}_{end}.json"


def daily_history(start: str, end: str, use_cache: bool = True) -> dict[str, dict] | None:
    """Daily Hyderabad weather for [start, end] (ISO dates).

    Returns {date: {temp_mean, temp_max, temp_min, humidity, rain}} or None
    if unavailable offline and no cached data covers the window.
    """
    cp = _cache_path(start, end)
    if use_cache and cp.exists():
        return json.loads(cp.read_text(encoding="utf-8"))
    data = _http_json(ARCHIVE_URL.format(lat=LAT, lon=LON, start=start, end=end))
    daily = (data or {}).get("daily") or {}
    times = daily.get("time") or []
    if not times:
        return None
    out = {}
    for i, d in enumerate(times):
        out[d] = dict(
            temp_mean=daily["temperature_2m_mean"][i],
            temp_max=daily["temperature_2m_max"][i],
            temp_min=daily["temperature_2m_min"][i],
            humidity=daily["relative_humidity_2m_mean"][i],
            rain=daily["precipitation_sum"][i],
        )
    try:
        cp.write_text(json.dumps(out), encoding="utf-8")
    except OSError:
        pass
    return out

def _stats(hist: dict[str, dict] | None, start: str, end: str) -> dict | None:
    """Aggregate daily weather over [start, end]; None if no data."""
    if not hist:
        return None
    rows = [v for k, v in hist.items() if start <= k <= end]
    if not rows:
        return None
    n = len(rows)
    return dict(
        temp_avg=sum(r["temp_mean"] for r in rows) / n,
        temp_max=max(r["temp_max"] for r in rows),
        rain=sum(r["rain"] or 0 for r in rows),
        humidity=sum(r["humidity"] or 0 for r in rows) / n,
        days=n,
    )


def anomaly(value: float | None, month: int, kind: str) -> float:
    """Anomaly vs monthly climatology: >0 hotter/wetter, <0 cooler/drier."""
    if value is None:
        return 0.0
    t, rain = CLIMATOLOGY[month]
    base = {"temp": t, "rain": rain / 30.0, "humidity": 55.0}[kind]
    if base == 0:
        return 0.0
    return round((value - base) / base, 3)


def weather_features(as_of: str, hist: dict[str, dict] | None = None) -> dict:
    """Weather feature block for a Laya state with analysis date as_of.

    `hist` optionally supplies an already-fetched daily history covering the
    28-day window, so batch callers (training) share ONE archive fetch instead
    of one HTTP call per state.
    Returns {} when neither the archive nor climatology can produce features
    (callers must treat weather as missing, never zero).
    """
    try:
        end = date.fromisoformat(as_of)
    except ValueError:
        return {}
    start28 = (end - timedelta(days=27)).isoformat()
    start7 = (end - timedelta(days=6)).isoformat()
    if hist is None:
        hist = daily_history(start28, as_of)
    w7 = _stats(hist, start7, as_of)
    w28 = _stats(hist, start28, as_of)
    if w7 is None and w28 is None:
        # offline: fall back to climatology so features are deterministic
        rows = {}
        for i in range(28):
            d = end - timedelta(days=i)
            rows[d.isoformat()] = _climatology_daily(d)
        w7 = _stats(rows, start7, as_of)
        w28 = _stats(rows, start28, as_of)
        source = "climatology"
    else:
        source = "open-meteo"
    month = end.month
    f = dict(
        location="Hyderabad",
        source=source,
        temp_7d_avg=round(w7["temp_avg"], 1),
        temp_max_7d=round(w7["temp_max"], 1),
        rain_7d_mm=round(w7["rain"], 1),
        humidity_7d_avg=round(w7["humidity"], 0),
        temp_28d_avg=round(w28["temp_avg"], 1) if w28 else None,
        rain_28d_mm=round(w28["rain"], 1) if w28 else None,
        temperature_anomaly=anomaly(w7["temp_avg"], month, "temp"),
        rainfall_anomaly=anomaly(w7["rain"], month, "rain"),
        humidity_anomaly=anomaly(w7["humidity"], month, "humidity"),
    )
    f.update(forecast_block(as_of))
    f["weather_anomaly"] = classify_anomaly(f)
    return f


_FORECAST_MEM: dict = {}  # per-process cache: one live forecast fetch per run


def forecast_block(as_of: str) -> dict:
    """Forward 7-day forecast features (reorder horizon).

    Uses Open-Meteo when the requested horizon is current/online; otherwise
    climatology-derived neutral flags. Never invents a live forecast.
    """
    try:
        end = date.fromisoformat(as_of)
    except ValueError:
        return dict(forecast_temp_7d=None, forecast_rain_7d=None,
                    forecast_heatwave_flag=0, forecast_heavy_rain_flag=0)
    today = date.today()
    if end >= today - timedelta(days=3):  # horizon overlaps "now" -> try live
        if "live" not in _FORECAST_MEM:
            _FORECAST_MEM["live"] = _http_json(FORECAST_URL.format(lat=LAT, lon=LON))
        data = _FORECAST_MEM["live"]
        daily = (data or {}).get("daily") or {}
        times = daily.get("time") or []
        if times:
            temps = daily.get("temperature_2m_mean") or []
            rains = daily.get("precipitation_sum") or []
            probs = daily.get("precipitation_probability_max") or []
            return dict(
                forecast_temp_7d=round(sum(temps[:7]) / max(len(temps[:7]), 1), 1),
                forecast_rain_7d=round(sum(rains[:7] or []), 1),
                forecast_rain_prob_max=max(probs[:7] or [0]) / 100.0,
                forecast_heatwave_flag=int(any(t >= 40 for t in temps[:7])),
                forecast_heavy_rain_flag=int(any((r or 0) >= 64 for r in rains[:7])),
            )
    # historical/horizon-neutral: flags from the 7d history we already have
    return dict(forecast_temp_7d=None, forecast_rain_7d=None,
                forecast_rain_prob_max=None,
                forecast_heatwave_flag=0, forecast_heavy_rain_flag=0)


def classify_anomaly(f: dict) -> str:
    """Coarse weather-anomaly label for humans and for event signals."""
    ta = f.get("temperature_anomaly") or 0
    ra = f.get("rainfall_anomaly") or 0
    if ta >= 0.15:
        return "hotter_than_normal"
    if ta <= -0.15:
        return "cooler_than_normal"
    if ra >= 0.5:
        return "wetter_than_normal"
    if ra <= -0.5:
        return "drier_than_normal"
    return "normal"
