# backend/weather.py

import os
import json
import time
from datetime import datetime, timedelta
import requests

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CACHE_TTL = 3600  # 1 hour


# -----------------------------
# Region Detection
# -----------------------------

def detect_region(lat, lon):
    # USA (rough bounding box)
    if 24 <= lat <= 49 and -125 <= lon <= -66:
        return "US"

    # UK / Ireland
    if 49 <= lat <= 61 and -11 <= lon <= 2:
        return "UK"

    # Continental Europe (very rough)
    if 36 <= lat <= 71 and -10 <= lon <= 40:
        return "EU"

    return "GLOBAL"


# -----------------------------
# Cache Helpers
# -----------------------------

def _cache_path(lat, lon):
    key = f"{round(lat, 2)}_{round(lon, 2)}"
    return os.path.join(DATA_DIR, f"weather_cache_{key}.json")


def _load_cache(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if time.time() - data.get("generated_ts", 0) < CACHE_TTL:
            return data
    except Exception:
        return None
    return None


def _save_cache(path, data):
    data["generated_ts"] = time.time()
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# -----------------------------
# Public API
# -----------------------------

def get_weather_forecast(lat, lon):
    """
    Returns normalized hourly night-weather data:
    {
      "provider": "Open-Meteo",
      "generated_at": "...",
      "lat": 52.5,
      "lon": 13.4,
      "hours": [
        {
          "time": "2026-02-03T22:00:00Z",
          "cloud": 18,
          "precip": 0,
          "is_night": True
        },
        ...
      ]
    }
    """

    cache_path = _cache_path(lat, lon)
    cached = _load_cache(cache_path)
    if cached:
        return cached

    region = detect_region(lat, lon)

    # For now, all regions use Open-Meteo.
    # The router is already here; we’ll swap per-region providers later.
    data = _fetch_open_meteo(lat, lon)

    data["region"] = region

    _save_cache(cache_path, data)
    return data


# -----------------------------
# Provider: Open-Meteo (ECMWF / Global)
# -----------------------------

def _fetch_open_meteo(lat, lon):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "cloudcover,precipitation_probability",
        "daily": "sunrise,sunset",
        "timezone": "UTC"
    }

    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    raw = resp.json()

    hourly_times = raw["hourly"]["time"]
    clouds = raw["hourly"]["cloudcover"]
    precips = raw["hourly"]["precipitation_probability"]

    # Build night windows from daily sunrise/sunset
    night_ranges = []
    for sr, ss in zip(raw["daily"]["sunrise"], raw["daily"]["sunset"]):
        sunset = datetime.fromisoformat(ss)
        sunrise = datetime.fromisoformat(sr) + timedelta(days=1)
        night_ranges.append((sunset, sunrise))

    def is_night(dt):
        for start, end in night_ranges:
            if start <= dt <= end:
                return True
        return False

    hours = []
    for t, c, p in zip(hourly_times, clouds, precips):
        dt = datetime.fromisoformat(t)
        hours.append({
            "time": dt.isoformat() + "Z",
            "cloud": int(c),
            "precip": int(p),
            "is_night": is_night(dt)
        })

    return {
        "provider": "Open-Meteo",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "lat": lat,
        "lon": lon,
        "hours": hours
    }
