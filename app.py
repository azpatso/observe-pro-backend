# redeploy trigger
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
import firebase_admin
from firebase_admin import credentials, messaging
import os
import requests
from zoneinfo import ZoneInfo

app = Flask(__name__)
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

# ---------- Firebase Initialization (Production Safe) ----------
import os
import json

try:
    import firebase_admin
    from firebase_admin import credentials, messaging
except ImportError:
    firebase_admin = None
    credentials = None
    messaging = None
    print("⚠️ firebase-admin not installed. Push notifications disabled.")

firebase_key_json = os.environ.get("FIREBASE_CREDENTIALS")

if firebase_admin and firebase_key_json and not firebase_admin._apps:
    try:
        cred = credentials.Certificate(json.loads(firebase_key_json))
        firebase_admin.initialize_app(cred)
        print("✅ Firebase initialized")
    except Exception as e:
        print("❌ Firebase init failed:", e)
else:
    if not firebase_key_json:
        print("⚠️ FIREBASE_SERVICE_ACCOUNT not set")

CORS(
    app,
    resources={r"/api/*": {"origins": "*"}},
    methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    supports_credentials=False,
)

@app.before_request
def handle_options_preflight():
    if request.method == "OPTIONS":
        return "", 200



app.register_blueprint(auth_bp, url_prefix="/api/auth")



AURORA_CACHE_FILE = DATA_DIR / "aurora_cache.json"
NOAA_KP_FORECAST_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json"
AURORA_CACHE_TTL_SECONDS = 60 * 60  # 1 hour


def send_push(user_id, title, body, data=None):
    if not messaging:
        print("Push skipped - Firebase not configured")
        return

    rows = sb_get("push_tokens", {"user_id": f"eq.{user_id}"})
    tokens = [row["token"] for row in rows]

    if not tokens:
        return

    for token in tokens:
        try:
            message = messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                data={k: str(v) for k, v in (data or {}).items()},
                token=token,
            )

            messaging.send(message)

        except messaging.UnregisteredError:
            print("Removing unregistered token:", token)
            sb_delete("push_tokens", {"token": f"eq.{token}"})

        except messaging.InvalidArgumentError:
            print("Removing invalid token:", token)
            sb_delete("push_tokens", {"token": f"eq.{token}"})

        except Exception as e:
            print("FCM send error (kept token):", e)



# ---------- Supabase REST Setup ----------
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
SUPABASE_KEY = (os.environ.get("SUPABASE_KEY") or "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL or SUPABASE_KEY missing")
                       
HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

def sb_get(table, params=None):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=HEADERS,
        params=params,
        timeout=10
    )
    r.raise_for_status()
    return r.json()

def sb_post(table, data):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=HEADERS,
        json=data,
        timeout=10
    )
    r.raise_for_status()
    return r.json()

def sb_patch(table, filters, data):
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=HEADERS,
        params=filters,
        json=data,
        timeout=10
    )
    r.raise_for_status()
    return r.json()

def sb_delete(table, filters):
    r = requests.delete(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=HEADERS,
        params=filters,
        timeout=10
    )
    r.raise_for_status()
    return r.json()





def _recently_notified(user, hours=12):
    ts = user.get("last_aurora_push_at")
    if not ts:
        return False

    try:
        last = datetime.fromisoformat(str(ts).replace("Z", ""))
        return datetime.utcnow() - last < timedelta(hours=hours)
    except Exception:
        return False


def _parse_event_start(event):
    try:
        return datetime.fromisoformat(str(event["start"]).replace("Z", ""))
    except Exception:
        return None



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
            now_iso = _utc_now_iso()

            # Fetch users who saved aurora events (join with users table)
            rows = sb_get(
                "user_events",
                {
                    "type": "eq.aurora",
                    "select": "user_id,users(id,lat,last_aurora_push_at)"
                }
            )

            processed = set()

            for row in rows:
                user = row.get("users")
                if not user:
                    continue

                user_id = user.get("id")
                if not user_id:
                    continue

                # Prevent duplicate processing if multiple aurora saves
                if user_id in processed:
                    continue
                processed.add(user_id)

                lat = user.get("lat")
                if lat is None:
                    continue

                # 12-hour cooldown check
                last_push = user.get("last_aurora_push_at")
                if last_push:
                    try:
                        last = datetime.fromisoformat(str(last_push).replace("Z", ""))
                        if datetime.utcnow() - last < timedelta(hours=12):
                            continue
                    except Exception:
                        pass

                forecast = get_aurora_forecast(lat)

                if forecast.get("likely"):
                    send_push(
                        user_id,
                        "🌌 Aurora Alert",
                        forecast.get("message"),
                        {
                            "type": "aurora",
                            "kp": forecast.get("kp_max_next_24h"),
                        }
                    )

                    # Update last push timestamp
                    sb_patch(
                        "users",
                        {"id": f"eq.{user_id}"},
                        {"last_aurora_push_at": now_iso}
                    )

        except Exception as e:
            print("Aurora job error:", e)

        time.sleep(60 * 60)  # run every hour

def scheduled_event_notification_job():
    while True:
        try:
            events = sb_get(
                "user_events",
                {
                    "select": "*,users(id,timezone)",
                    "start": f"gte.{_utc_now_iso()}"
                }
            )

            for event in events:
                user = event.get("users")
                if not user:
                    continue

                user_id = user.get("id")
                timezone_str = user.get("timezone") or "UTC"

                try:
                    user_tz = ZoneInfo(timezone_str)
                except Exception:
                    user_tz = ZoneInfo("UTC")

                now_local = datetime.now(user_tz)

                start_utc = _parse_event_start(event)
                if not start_utc:
                    continue

                if start_utc.tzinfo is None:
                    start_utc = start_utc.replace(tzinfo=ZoneInfo("UTC"))
                else:
                    start_utc = start_utc.astimezone(ZoneInfo("UTC"))

                start_local = start_utc.astimezone(user_tz)

                event_type = event.get("type")

                # =========================
                # 🌌 AURORA
                # =========================
                if event_type == "aurora":

                    if event.get("notified_4h_at") is None and now_local >= start_local - timedelta(hours=4):
                        send_push(user_id, "🌌 Aurora Incoming",
                                  "Aurora expected in ~4 hours.",
                                  {"type": "aurora"})
                        sb_patch("user_events",
                                 {"id": f"eq.{event['id']}"},
                                 {"notified_4h_at": _utc_now_iso()})

                    if event.get("notified_30m_at") is None and now_local >= start_local - timedelta(minutes=30):
                        send_push(user_id, "🌌 Aurora Soon",
                                  "Aurora activity starting shortly.",
                                  {"type": "aurora"})
                        sb_patch("user_events",
                                 {"id": f"eq.{event['id']}"},
                                 {"notified_30m_at": _utc_now_iso()})

                # =========================
                # 🌠 METEOR
                # =========================
                elif event_type == "meteor":

                    if event.get("notified_12h_at") is None and now_local >= start_local - timedelta(hours=12):
                        send_push(user_id, f"🌠 {event.get('title')}",
                                  "Meteor shower peak in ~12 hours.",
                                  {"type": "meteor"})
                        sb_patch("user_events",
                                 {"id": f"eq.{event['id']}"},
                                 {"notified_12h_at": _utc_now_iso()})

                    if event.get("notified_1h_at") is None and now_local >= start_local - timedelta(hours=1):
                        send_push(user_id, f"🌠 {event.get('title')}",
                                  "Meteor shower peak in ~1 hour.",
                                  {"type": "meteor"})
                        sb_patch("user_events",
                                 {"id": f"eq.{event['id']}"},
                                 {"notified_1h_at": _utc_now_iso()})

                # =========================
                # 🌑 ECLIPSE / ☄ COMET / ✨ ALIGNMENT
                # =========================
                elif event_type in ("eclipse", "comet", "alignment"):

                    if event.get("notified_24h_at") is None and now_local >= start_local - timedelta(hours=24):
                        send_push(user_id, f"🌌 {event.get('title')}",
                                  "Event begins in ~24 hours.",
                                  {"type": event_type})
                        sb_patch("user_events",
                                 {"id": f"eq.{event['id']}"},
                                 {"notified_24h_at": _utc_now_iso()})

                    if event.get("notified_1h_at") is None and now_local >= start_local - timedelta(hours=1):
                        send_push(user_id, f"🌌 {event.get('title')}",
                                  "Event begins in ~1 hour.",
                                  {"type": event_type})
                        sb_patch("user_events",
                                 {"id": f"eq.{event['id']}"},
                                 {"notified_1h_at": _utc_now_iso()})

                # =========================
                # 🌙 MOON (optional 8am logic kept separate)
                # =========================
                elif event_type == "moon":

                    if event.get("notified_1h_at") is None and now_local >= start_local - timedelta(hours=1):
                        send_push(user_id, f"🌙 {event.get('title')}",
                                  "Moon phase happening in ~1 hour.",
                                  {"type": "moon"})
                        sb_patch("user_events",
                                 {"id": f"eq.{event['id']}"},
                                 {"notified_1h_at": _utc_now_iso()})

        except Exception as e:
            print("Scheduled job error:", e)

        time.sleep(60 * 15)


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

    events = sb_get("user_events", {"user_id": f"eq.{user_id}"})

    return jsonify({
        "success": True,
        "events": events
    })



@app.route("/api/test-push")
def test_push():
    # 1) get most recent saved event (that has a user_id)
    events = sb_get(
        "user_events",
        {
            "order": "created_at.desc",
            "limit": 1
        }
    )

    if not events:
        return jsonify({"error": "No saved events found"}), 400

    ev = events[0]
    user_id = ev["user_id"]

    # 2) build a nice title/body
    title = f"🌌 {ev.get('title', 'Cosmic Event')}"
    body = "Saved event reminder — tap to view details."

    # 3) include data for deep-link routing later
    send_push(
        user_id,
        title,
        body,
        {
            "type": ev.get("type", ""),
            "eventId": ev.get("event_id", "")
        }
    )

    return jsonify({"success": True, "sentToUser": user_id, "eventId": ev.get("event_id")})






@app.route("/api/user/events", methods=["POST"])
def add_user_event():
    data = request.get_json() or {}
    user_id = data.get("userId")
    event = data.get("event")

    if not user_id or not event:
        return jsonify({"success": False, "error": "Missing data"}), 400

    sb_post("user_events", {
        "user_id": user_id,
        "event_id": event.get("id"),
        "type": event.get("type"),
        "title": event.get("title"),
        "start": event.get("start"),
    })

    return jsonify({"success": True})




@app.route("/api/user/events", methods=["DELETE"])
def delete_user_event():
    data = request.get_json() or {}
    user_id = data.get("userId")
    event_id = data.get("eventId")

    if not user_id or not event_id:
        return jsonify({"success": False, "error": "Missing userId or eventId"}), 400

    sb_delete(
        "user_events",
        {
            "user_id": f"eq.{user_id}",
            "event_id": f"eq.{event_id}"
        }
    )

    return jsonify({"success": True})



@app.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    data = request.get_json() or {}
    user_id = data.get("userId")
    token = data.get("token")

    if not user_id or not token:
        return jsonify({"success": False, "error": "Missing userId or token"}), 400

    try:
        sb_post("push_tokens", {
            "user_id": user_id,
            "token": token
        })
        return jsonify({"success": True, "message": "Token inserted"}), 200

    except requests.HTTPError as e:
        # 409 = UNIQUE constraint violation (token already exists)
        if e.response is not None and e.response.status_code == 409:
            return jsonify({"success": True, "message": "Token already exists"}), 200

        # Any other error → real failure
        print("Push subscribe error:", e)
        return jsonify({"success": False, "error": "Failed to save token"}), 500






@app.route("/api/calendar/<event_id>")
def export_calendar(event_id):
    from flask import request

    user_id = request.args.get("userId")

    event = None

    # 1️⃣ Try user_events first (saved events)
    if user_id:
        rows = sb_get(
            "user_events",
            {
                "user_id": f"eq.{user_id}",
                "event_id": f"eq.{event_id}",
                "limit": 1
            }
        )
        if rows:
            row = rows[0]
            event = {
                "id": row["event_id"],
                "title": row["title"],
                "start": row["start"],
                "end": row["start"]
            }

    # 2️⃣ If not found → search static events
    if not event:
        all_events = []
        all_events.extend(get_eclipse_events())
        all_events.extend(get_meteor_events())
        all_events.extend(get_comet_events())
        all_events.extend(get_alignment_events())
        all_events.extend(get_special_moon_events())

        for e in all_events:
            if e["id"] == event_id:
                event = e
                break

    if not event:
        return jsonify({"error": "Event not found"}), 404

    ics = generate_ics(event)

    return (
        ics,
        200,
        {
            "Content-Type": "text/calendar",
            "Content-Disposition": f'attachment; filename="{event_id}.ics"',
        },
    )




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

def start_background_jobs():
    threading.Thread(
        target=aurora_notification_job,
        daemon=True
    ).start()

    threading.Thread(
        target=scheduled_event_notification_job,
        daemon=True
    ).start()

start_background_jobs()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))




