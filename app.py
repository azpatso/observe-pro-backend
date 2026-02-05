from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime, timedelta
import json
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError
from weather import get_weather_forecast
from auth import auth_bp
import threading
import time
from pywebpush import webpush, WebPushException

VAPID_PRIVATE_KEY = "TW7EhNFjJusorX_LyTyVFllJRcBqK3fmkXlOMS6Jx-I"
VAPID_SUBJECT = "mailto:you@example.com"

app = Flask(__name__)
CORS(app)

app.register_blueprint(auth_bp, url_prefix="/api/auth")

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

USERS_FILE = DATA_DIR / "users.json"

def load_users():
    if not USERS_FILE.exists():
        return []
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

def save_users(users):
    USERS_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")

AURORA_CACHE_FILE = DATA_DIR / "aurora_cache.json"
NOAA_KP_FORECAST_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json"
AURORA_CACHE_TTL_SECONDS = 60 * 60  # 1 hour

def send_push(user, title, body, data=None):
    subs = user.get("pushSubscriptions", [])
    if not subs:
        return

    payload = {
        "title": title,
        "body": body,
        "data": data or {}
    }

    # Collect broken subscriptions to remove
    bad_endpoints = set()

    for sub in list(subs):
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps(payload),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
            )
        except WebPushException as e:
            # If endpoint is expired/gone/unauthorized, mark for removal
            try:
                status = getattr(e.response, "status_code", None)
            except Exception:
                status = None

            # Typical "dead subscription" status codes
            if status in (404, 410, 401, 403):
                endpoint = sub.get("endpoint")
                if endpoint:
                    bad_endpoints.add(endpoint)
            continue

    # Remove dead subscriptions so you don’t keep retrying forever
    if bad_endpoints:
        user["pushSubscriptions"] = [
            s for s in subs if s.get("endpoint") not in bad_endpoints
        ]


def _recently_notified(user, hours=12):
    ts = user.get("lastAuroraPushAt")
    if not ts:
        return False

    try:
        last = datetime.fromisoformat(ts.replace("Z", ""))
        return datetime.utcnow() - last < timedelta(hours=hours)
    except Exception:
        return False

def _parse_event_start(event):
    try:
        return datetime.fromisoformat(event["start"].replace("Z", ""))
    except Exception:
        return None


def _already_notified(event):
    return bool(event.get("notifiedAt"))


def _should_notify(event, now):
    start = _parse_event_start(event)
    if not start:
        return False

    etype = event.get("type")

    if etype == "meteor":
        return now >= start - timedelta(hours=12)
    if etype == "eclipse":
        return now >= start - timedelta(hours=24)
    if etype in ("comet", "alignment"):
        return now >= start - timedelta(hours=24)
    if etype == "moon":
        return now.date() == start.date() and now.hour >= 8

    return False

def aurora_notification_job():
    while True:
        try:
            users = load_users()

            for user in users:
                events = user.get("events", [])
                lat = user.get("lat")

                if lat is None:
                    continue


                has_saved_aurora = any(
                    e.get("type") == "aurora" for e in events
                )

                if not has_saved_aurora:
                    continue

                forecast = get_aurora_forecast(lat)

                if forecast.get("likely"):

                    # 🚫 prevent hourly spam
                    if _recently_notified(user, hours=12):
                        continue

                    send_push(
                        user,
                        "🌌 Aurora Alert",
                        forecast.get("message"),
                        {
                            "type": "aurora",
                            "kp": forecast.get("kp_max_next_24h"),
                        }
                    )

                    # mark notification time
                    user["lastAuroraPushAt"] = _utc_now_iso()
                    save_users(users)

        except Exception as e:
            print("Aurora job error:", e)

        # Run every 60 minutes
        time.sleep(60 * 60)

def scheduled_event_notification_job():
    while True:
        try:
            users = load_users()
            now = datetime.utcnow()
            changed = False

            for user in users:
                events = user.get("events", [])
                if not events:
                    continue

                for event in events:
                    etype = event.get("type")

                    # Skip aurora (handled separately)
                    if etype == "aurora":
                        continue

                    if _already_notified(event):
                        continue

                    if not _should_notify(event, now):
                        continue

                    title = f"🌌 {event.get('title', 'Cosmic event')}"
                    body = "Happening soon — check visibility and details in My Sky."

                    send_push(
                        user,
                        title,
                        body,
                        {
                            "type": etype,
                            "eventId": event.get("id")
                        }
                    )

                    # Mark event as notified
                    event["notifiedAt"] = _utc_now_iso()
                    changed = True

            if changed or True:
                save_users(users)

        except Exception as e:
            print("Scheduled notification job error:", e)

        # Check every 30 minutes
        time.sleep(60 * 30)

# ---------- Load Moon Data Once ----------

with open(DATA_DIR / "moon_phases.json", "r", encoding="utf-8") as f:
    MOON_RAW = json.load(f)

with open(DATA_DIR / "meteor_showers.json", "r", encoding="utf-8") as f:
    METEOR_RAW = json.load(f)

with open(DATA_DIR / "comets.json", "r", encoding="utf-8") as f:
    COMETS_RAW = json.load(f)

with open(DATA_DIR / "alignments.json", "r", encoding="utf-8") as f:
    ALIGNMENTS_RAW = json.load(f)

def build_moon_events():
    events = []
    last_phase = None

    for date_str, info in sorted(MOON_RAW.items()):
        phase = info["phase"]

        # Only emit an event when the phase changes
        if phase != last_phase and phase in (
            "New Moon",
            "First Quarter",
            "Full Moon",
            "Last Quarter"
        ):
            events.append({
                "id": f"moon-{date_str}",
                "type": "moon",
                "title": phase,
                "start": f"{date_str}T00:00:00Z",
                "end": f"{date_str}T23:59:59Z",
                "visibility": "global",
                "confidence": "high",
                "source": "internal",
                "tags": [phase.lower().replace(" ", "_")]
            })

        last_phase = phase

    return events

MOON_EVENTS = build_moon_events()

# ---------- Data Providers ----------

def get_eclipse_events():
    """
    Real eclipse events from Jan 2026 to Jan 2028.
    Includes solar and lunar eclipses.
    Normalized to the app's event schema.
    """

    events = [
        {
            "id": "eclipse-2026-02-17-solar-annular",
            "type": "eclipse",
            "title": "Annular Solar Eclipse",
            "start": "2026-02-17T00:00:00Z",
            "end": "2026-02-17T23:59:59Z",
            "visibility": "global",
            "confidence": "high",
            "source": "NASA Eclipse Catalog",
            "tags": ["solar_eclipse", "annular"]
        },
        {
            "id": "eclipse-2026-03-03-lunar-total",
            "type": "eclipse",
            "title": "Total Lunar Eclipse",
            "start": "2026-03-03T00:00:00Z",
            "end": "2026-03-03T23:59:59Z",
            "visibility": "global",
            "confidence": "high",
            "source": "NASA Eclipse Catalog",
            "tags": ["lunar_eclipse", "total"]
        },
        {
            "id": "eclipse-2026-08-12-solar-total",
            "type": "eclipse",
            "title": "Total Solar Eclipse",
            "start": "2026-08-12T00:00:00Z",
            "end": "2026-08-12T23:59:59Z",
            "visibility": "global",
            "confidence": "high",
            "source": "NASA Eclipse Catalog",
            "tags": ["solar_eclipse", "total"]
        },
        {
            "id": "eclipse-2026-08-28-lunar-partial",
            "type": "eclipse",
            "title": "Partial Lunar Eclipse",
            "start": "2026-08-28T00:00:00Z",
            "end": "2026-08-28T23:59:59Z",
            "visibility": "global",
            "confidence": "medium",
            "source": "NASA Eclipse Catalog",
            "tags": ["lunar_eclipse", "partial"]
        },
        {
            "id": "eclipse-2027-02-06-solar-annular",
            "type": "eclipse",
            "title": "Annular Solar Eclipse",
            "start": "2027-02-06T00:00:00Z",
            "end": "2027-02-06T23:59:59Z",
            "visibility": "global",
            "confidence": "high",
            "source": "NASA Eclipse Catalog",
            "tags": ["solar_eclipse", "annular"]
        },
        {
            "id": "eclipse-2027-02-20-lunar-penumbral",
            "type": "eclipse",
            "title": "Penumbral Lunar Eclipse",
            "start": "2027-02-20T00:00:00Z",
            "end": "2027-02-20T23:59:59Z",
            "visibility": "global",
            "confidence": "low",
            "source": "NASA Eclipse Catalog",
            "tags": ["lunar_eclipse", "penumbral"]
        },
        {
            "id": "eclipse-2027-08-02-solar-total",
            "type": "eclipse",
            "title": "Total Solar Eclipse",
            "start": "2027-08-02T00:00:00Z",
            "end": "2027-08-02T23:59:59Z",
            "visibility": "global",
            "confidence": "high",
            "source": "NASA Eclipse Catalog",
            "tags": ["solar_eclipse", "total"]
        }
    ]

    return [e for e in events if _is_future(e)]

def get_meteor_events():
    events = []

    for m in METEOR_RAW:
        peak = m["peak"]

        events.append({
            "id": m["id"],
            "type": "meteor",
            "title": f'{m["name"]} Peak',
            "start": f"{peak}T00:00:00Z",
            "end": f"{peak}T23:59:59Z",
            "visibility": "global",
            "confidence": m["confidence"],
            "source": m["source"],
            "tags": ["meteor_shower", m["name"].lower()]
        })

    return [e for e in events if _is_future(e)]



def get_comet_events():
    now = datetime.utcnow()
    events = []

    for e in COMETS_RAW:
        try:
            end = _parse_iso(e.get("end", e["start"]))
            if end >= now:
                events.append(e)
        except Exception:
            events.append(e)

    return events



def get_alignment_events():
    return [e for e in ALIGNMENTS_RAW if _is_future(e)]



def _utc_now_iso():
    return datetime.utcnow().isoformat() + "Z"

def generate_ics(event):
    def fmt(dt):
        return dt.strftime("%Y%m%dT%H%M%SZ")

    start = _parse_iso(event["start"])
    end = _parse_iso(event.get("end", event["start"]))

    uid = f'{event["id"]}@mysky'

    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//My Sky//EN
BEGIN:VEVENT
UID:{uid}
DTSTAMP:{fmt(datetime.utcnow())}
DTSTART:{fmt(start)}
DTEND:{fmt(end)}
SUMMARY:{event.get("title", "Cosmic Event")}
DESCRIPTION:Saved from My Sky
END:VEVENT
END:VCALENDAR
"""


def _is_future(event):
    try:
        return _parse_iso(event["start"]) >= datetime.utcnow()
    except Exception:
        return True


def _load_aurora_cache():
    if not AURORA_CACHE_FILE.exists():
        return None
    try:
        return json.loads(AURORA_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_aurora_cache(payload):
    AURORA_CACHE_FILE.write_text(json.dumps(payload), encoding="utf-8")

def _ensure_user_events_schema(user):
    # Add events list if missing
    if "events" not in user or not isinstance(user["events"], list):
        user["events"] = []

def _ensure_push_schema(user):
    # Keep your existing pushSubscriptions key, just ensure it's a list
    if "pushSubscriptions" not in user or not isinstance(user["pushSubscriptions"], list):
        user["pushSubscriptions"] = []

def fetch_noaa_kp_forecast_cached():
    """
    Returns NOAA Kp forecast JSON (array of arrays) with a 1-hour cache on disk.
    Source: NOAA SWPC services endpoint.
    """
    cached = _load_aurora_cache()
    if cached:
        try:
            cached_at = datetime.fromisoformat(cached["cached_at"].replace("Z", ""))
            age = (datetime.utcnow() - cached_at).total_seconds()
            if age < AURORA_CACHE_TTL_SECONDS:
                return cached["kp_forecast"]
        except Exception:
            pass

    # Fetch fresh
    try:
        with urlopen(NOAA_KP_FORECAST_URL, timeout=10) as r:
            raw = r.read().decode("utf-8")
            kp_forecast = json.loads(raw)

        _save_aurora_cache({
            "cached_at": _utc_now_iso(),
            "kp_forecast": kp_forecast
        })
        return kp_forecast
    except URLError:
        # If NOAA is unreachable, fall back to cache if we have it
        if cached and "kp_forecast" in cached:
            return cached["kp_forecast"]
        raise


def required_kp_for_lat(lat):
    """
    Very simple, practical mapping:
    higher latitude needs lower Kp; mid-latitudes need higher Kp.
    Uses absolute latitude so it works for southern hemisphere too.
    """
    a = abs(float(lat))

    if a >= 67:
        return 3
    if a >= 63:
        return 4
    if a >= 60:
        return 5
    if a >= 57:
        return 6
    if a >= 54:
        return 7
    if a >= 50:
        return 8
    return 9


def summarize_kp_next_24h(kp_rows):
    """
    NOAA Kp forecast file format:
    [
      ["time_tag","kp","observed","noaa_scale"],
      ["YYYY-MM-DD HH:MM:SS","3.33","predicted",null],
      ...
    ]
    We compute max Kp in the next 24 hours from now.
    """
    now = datetime.utcnow()
    cutoff = now + timedelta(hours=24)

    max_kp = None
    max_entry = None

    # skip header row
    for row in kp_rows[1:]:
        t_str, kp_str, status, scale = row
        try:
            t = datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S")
            if now <= t <= cutoff:
                kp = float(kp_str)
                if (max_kp is None) or (kp > max_kp):
                    max_kp = kp
                    max_entry = {
                        "time_tag": t_str + "Z",
                        "kp": kp,
                        "status": status,
                        "noaa_scale": scale
                    }
        except Exception:
            continue

    return max_kp, max_entry

def get_aurora_forecast(lat):
    """
    Core aurora logic extracted so it can be reused by /api/aurora
    and /api/upcoming.
    """
    kp_rows = fetch_noaa_kp_forecast_cached()
    max_kp, max_entry = summarize_kp_next_24h(kp_rows)

    req_kp = required_kp_for_lat(lat)
    # Find next future time where Kp meets requirement
    next_possible = None
    now = datetime.utcnow()

    for row in kp_rows[1:]:
        t_str, kp_str, *_ = row
        try:
            t = datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S")
            kp = float(kp_str)

            if t > now and kp >= req_kp:
                next_possible = t.strftime("%Y-%m-%d")
                break
        except Exception:
            continue


    if max_kp is None:
        return {
        "lat": lat,
        "required_kp": req_kp,
        "kp_max_next_24h": None,
        "likely": False,
        "next_possible": next_possible,
        "message": "No Kp forecast available right now.",
        "peak": None,
        "source": "NOAA SWPC",
        "cached_at": _load_aurora_cache().get("cached_at") if _load_aurora_cache() else None
    }


    likely = max_kp >= req_kp

    msg = (
        f"High chance tonight/next 24h (Kp max {max_kp:.2f} ≥ {req_kp})"
        if likely
        else f"Low chance (Kp max {max_kp:.2f} < {req_kp})"
    )

    return {
    "lat": lat,
    "required_kp": req_kp,
    "kp_max_next_24h": max_kp,
    "peak": max_entry,
    "likely": likely,
    "next_possible": next_possible,
    "message": msg,
    "source": "NOAA SWPC",
    "cached_at": _load_aurora_cache().get("cached_at") if _load_aurora_cache() else None
}



def aurora_forecast_to_upcoming_event(forecast):
    """
    Turn an aurora forecast into a synthetic upcoming event
    if it is actually likely.
    """
    if not forecast or not forecast.get("likely"):
        return None

    peak = forecast.get("peak") or {}
    time_tag = peak.get("time_tag")  # "YYYY-MM-DD HH:MM:SSZ"

    date_str = None
    if isinstance(time_tag, str) and len(time_tag) >= 10:
        date_str = time_tag[:10]

    kp = peak.get("kp")
    required = forecast.get("required_kp")
    lat = forecast.get("lat")

    return {
        "id": f"aurora-{date_str or 'today'}",
        "type": "aurora",
        "title": "Aurora likely tonight in your area",
        "subtitle": f"Lat {lat}° · Kp {kp} (need ≥ {required})" if kp is not None else f"Lat {lat}°",
        "start": f"{date_str}T00:00:00Z" if date_str else _utc_now_iso(),
        "end": f"{date_str}T23:59:59Z" if date_str else _utc_now_iso(),
        "visibility": "regional",
        "confidence": "medium",
        "source": forecast.get("source", "NOAA SWPC"),
        "tags": ["aurora", "space_weather"],
        "data": forecast
    }

# ---------- Visibility Engine ----------

VISIBILITY_WINDOW_DAYS = 28

BASE_EVENT_CHANCE = {
    "meteor": 65,
    "eclipse": 80,
    "comet": 55,
    "aurora": 70,
}


def _parse_iso(dt):
    return datetime.fromisoformat(dt.replace("Z", ""))


def _weather_score_for_event(event, weather):
    """
    Look at night hours during the event day and compute
    average cloud + precip.
    """
    start = _parse_iso(event["start"])
    end = _parse_iso(event["end"])

    relevant = [
        h for h in weather.get("hours", [])
        if h["is_night"]
        and start <= _parse_iso(h["time"]) <= end + timedelta(days=1)
    ]

    if not relevant:
        return None

    avg_cloud = sum(h["cloud"] for h in relevant) / len(relevant)
    avg_precip = sum(h["precip"] for h in relevant) / len(relevant)

    return avg_cloud, avg_precip


def estimate_visibility(event, lat, lon, weather):
    base = BASE_EVENT_CHANCE.get(event["type"], 50)

    score = base
    reason_parts = []

    weather_stats = _weather_score_for_event(event, weather)

    if weather_stats:
        avg_cloud, avg_precip = weather_stats

        if avg_cloud < 20:
            score += 15
            reason_parts.append("clear skies expected")
        elif avg_cloud < 50:
            score += 5
            reason_parts.append("partly cloudy skies")
        elif avg_cloud < 75:
            score -= 10
            reason_parts.append("mostly cloudy")
        else:
            score -= 25
            reason_parts.append("heavy cloud cover")

        if avg_precip > 40:
            score -= 20
            reason_parts.append("rain likely")
        elif avg_precip > 20:
            score -= 10
            reason_parts.append("chance of rain")

    score = max(0, min(100, int(score)))

    if not reason_parts:
        reason = "visibility uncertain"
    else:
        reason = ", ".join(reason_parts).capitalize()

    return {
        "chance": score,
        "reason": reason
    }

def get_moon_window(days=30):
    today = datetime.utcnow().date()
    end = today + timedelta(days=days)

    # Traditional names for full moons
    month_names = {
        1: "Wolf Moon",
        2: "Snow Moon",
        3: "Worm Moon",
        4: "Pink Moon",
        5: "Flower Moon",
        6: "Strawberry Moon",
        7: "Buck Moon",
        8: "Sturgeon Moon",
        9: "Harvest Moon",
        10: "Hunter’s Moon",
        11: "Beaver Moon",
        12: "Cold Moon",
    }

    # First, find all full moons in the window grouped by month
    fulls_by_month = {}
    for date_str, info in sorted(MOON_RAW.items()):
        try:
            d = datetime.fromisoformat(date_str).date()
        except Exception:
            continue

        if today <= d <= end and info.get("phase") == "Full Moon":
            key = (d.year, d.month)
            fulls_by_month.setdefault(key, []).append(date_str)

    window = []

    for date_str, info in sorted(MOON_RAW.items()):
        try:
            d = datetime.fromisoformat(date_str).date()
        except Exception:
            continue

        if not (today <= d <= end):
            continue

        entry = {
            "date": date_str,
            "phase": info.get("phase"),
            "illumination": info.get("illumination"),
        }

        # Attach special name if this is a named full moon
        if info.get("phase") == "Full Moon":
            key = (d.year, d.month)
            moons = fulls_by_month.get(key, [])

            if len(moons) > 1 and moons.index(date_str) == 1:
                entry["special"] = "Blue Moon"
            else:
                entry["special"] = month_names.get(d.month)

        window.append(entry)

    return window


def get_special_moon_events(days=60):
    """
    Generate special named moon events (Blue Moon, Strawberry Moon, etc.)
    from MOON_RAW within the next `days`.
    """
    today = datetime.utcnow().date()
    end = today + timedelta(days=days)

    # Collect full moons in the window
    full_moons = []
    for date_str, info in sorted(MOON_RAW.items()):
        try:
            d = datetime.fromisoformat(date_str).date()
        except Exception:
            continue

        if today <= d <= end and info.get("phase") == "Full Moon":
            full_moons.append((d, date_str, info))

    events = []

    # Map month → traditional full moon name
    month_names = {
        1: "Wolf Moon",
        2: "Snow Moon",
        3: "Worm Moon",
        4: "Pink Moon",
        5: "Flower Moon",
        6: "Strawberry Moon",
        7: "Buck Moon",
        8: "Sturgeon Moon",
        9: "Harvest Moon",
        10: "Hunter’s Moon",
        11: "Beaver Moon",
        12: "Cold Moon",
    }

    # Detect Blue Moons (second full moon in same month)
    by_month = {}
    for d, date_str, info in full_moons:
        key = (d.year, d.month)
        by_month.setdefault(key, []).append((d, date_str, info))

    for (year, month), moons in by_month.items():
        for idx, (d, date_str, info) in enumerate(moons):
            name = month_names.get(month, "Full Moon")
            tags = ["full_moon"]

            title = name
            subtitle = f"{d.strftime('%B')} full moon"

            if len(moons) > 1 and idx == 1:
                title = "Blue Moon"
                subtitle = "Second full moon of the month"
                tags.append("blue_moon")
            # Keep only Strawberry, Supermoon, Micromoon
            if title not in ("Strawberry Moon", "Supermoon", "Micromoon"):
                continue

            events.append({
                "id": f"moon-special-{date_str}",
                "type": "moon_special",
                "title": title,
                "subtitle": subtitle,
                "start": f"{date_str}T00:00:00Z",
                "end": f"{date_str}T23:59:59Z",
                "visibility": "global",
                "confidence": "high",
                "source": "Lunar tradition",
                "tags": tags
            })

    return events

# ---------- API Routes ----------
@app.route("/api/user/events", methods=["GET"])
def get_user_events():
    user_id = request.args.get("userId")
    if not user_id:
        return jsonify({"success": False, "error": "Missing userId"}), 400

    users = load_users()
    user = next((u for u in users if u.get("id") == user_id), None)
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    _ensure_user_events_schema(user)
    return jsonify({"success": True, "events": user["events"]})


@app.route("/api/user/events", methods=["POST"])
def add_user_event():
    data = request.get_json() or {}
    user_id = data.get("userId")
    event = data.get("event")

    if not user_id or not event:
        return jsonify({"success": False, "error": "Missing userId or event"}), 400

    if not isinstance(event, dict) or not event.get("id"):
        return jsonify({"success": False, "error": "Event must be an object with an id"}), 400

    users = load_users()
    user = next((u for u in users if u.get("id") == user_id), None)
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    _ensure_user_events_schema(user)

    # Avoid duplicates by id
    existing = user["events"]
    if not any(e.get("id") == event.get("id") for e in existing):
        # add server-side metadata (optional but useful)
        event = dict(event)
        event.setdefault("createdAt", _utc_now_iso())
        existing.append(event)
        save_users(users)

    return jsonify({"success": True, "events": user["events"]})


@app.route("/api/user/events", methods=["DELETE"])
def delete_user_event():
    data = request.get_json() or {}
    user_id = data.get("userId")
    event_id = data.get("eventId")

    if not user_id or not event_id:
        return jsonify({"success": False, "error": "Missing userId or eventId"}), 400

    users = load_users()
    user = next((u for u in users if u.get("id") == user_id), None)
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    _ensure_user_events_schema(user)

    before = len(user["events"])
    user["events"] = [e for e in user["events"] if e.get("id") != event_id]
    after = len(user["events"])

    if after != before:
        save_users(users)

    return jsonify({"success": True, "events": user["events"]})

@app.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    data = request.get_json() or {}

    user_id = data.get("userId")
    sub = data.get("subscription")

    if not user_id or not sub:
        return jsonify({"success": False, "error": "Missing userId or subscription"}), 400

    # Basic validation (prevents corrupt writes)
    if not isinstance(sub, dict) or not sub.get("endpoint"):
        return jsonify({"success": False, "error": "Invalid subscription object"}), 400

    users = load_users()
    user = next((u for u in users if u.get("id") == user_id), None)

    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    _ensure_push_schema(user)

    endpoint = sub.get("endpoint")

    # Remove any existing subscription with same endpoint, then add the latest version
    user["pushSubscriptions"] = [
        s for s in user["pushSubscriptions"]
        if s.get("endpoint") != endpoint
    ]
    user["pushSubscriptions"].append(sub)

    save_users(users)

    return jsonify({"success": True})

@app.route("/api/calendar/<event_id>")
def export_calendar(event_id):
    users = load_users()

    # Find the event across all users
    for user in users:
        for event in user.get("events", []):
            if event.get("id") == event_id:

                # 🚫 Aurora cannot be exported
                if event.get("type") == "aurora":
                    return jsonify({
                        "error": "Aurora forecasts can’t be exported to calendar because they rely on short-term (~24h) space weather predictions for accuracy."
                    }), 400

                ics = generate_ics(event)

                return (
                    ics,
                    200,
                    {
                        "Content-Type": "text/calendar",
                        "Content-Disposition": f'attachment; filename="{event_id}.ics"',
                    },
                )

    return jsonify({"error": "Event not found"}), 404


@app.route("/api/moon")
def moon():
    return jsonify(get_moon_window(30))


@app.route("/api/eclipses")
def eclipses():
    return jsonify(get_eclipse_events())

@app.route("/api/meteors")
def meteors():
    return jsonify(get_meteor_events())

@app.route("/api/aurora")
def aurora():
    from flask import request

    lat = request.args.get("lat", type=float)
    if lat is None:
        return jsonify({"error": "lat query param is required, e.g. /api/aurora?lat=55.9"}), 400

    forecast = get_aurora_forecast(lat)
    return jsonify(forecast)

@app.route("/api/comets")
def comets():
    return jsonify(get_comet_events())

@app.route("/api/weather")
def api_weather():
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)

    if lat is None or lon is None:
        return jsonify({"error": "lat and lon are required"}), 400

    try:
        data = get_weather_forecast(lat, lon)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/alignments")
def alignments():
    return jsonify(get_alignment_events())

@app.route("/api/upcoming")
def upcoming_events():
    from flask import request

    now = datetime.utcnow()

    all_events = []
    all_events.extend(get_eclipse_events())
    all_events.extend(get_meteor_events())
    all_events.extend(get_comet_events())
    all_events.extend(get_special_moon_events())
    all_events.extend(get_alignment_events())


    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)

    weather = None
    if lat is not None and lon is not None:
        weather = get_weather_forecast(lat, lon)

        forecast = get_aurora_forecast(lat)
        aurora_event = aurora_forecast_to_upcoming_event(forecast)
        if aurora_event:
            all_events.append(aurora_event)

    enriched = []
    for e in all_events:
        start = _parse_iso(e["start"])
        end = _parse_iso(e.get("end", e["start"]))

        # Drop only if the event fully ended (end + 1 day)
        if end + timedelta(days=1) < now:
            continue

        e = dict(e)

        if weather and lat is not None and lon is not None:
            if (start - now).days <= VISIBILITY_WINDOW_DAYS:
                e["visibility"] = estimate_visibility(e, lat, lon, weather)

        enriched.append(e)


        

    enriched.sort(key=lambda e: e["start"])
    return jsonify(enriched[:50])

if __name__ == "__main__":
    threading.Thread(
        target=aurora_notification_job,
        daemon=True
    ).start()

    threading.Thread(
        target=scheduled_event_notification_job,
        daemon=True
    ).start()

    app.run(host="0.0.0.0", port=5000, debug=True)


