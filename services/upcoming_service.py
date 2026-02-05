from flask import Blueprint, request, jsonify
from pathlib import Path
import json
import re
import hashlib
import os
import requests
from datetime import datetime

auth_bp = Blueprint("auth", __name__)

BASE_DIR = Path(__file__).parent
USERS_FILE = BASE_DIR / "users.json"

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ------------------ Utilities ------------------

def _load_users():
    if not USERS_FILE.exists():
        return []
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

def _save_users(users):
    USERS_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")

def _hash_password(password, salt=None):
    if salt is None:
        salt = os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100_000
    ).hex()
    return f"{salt}${h}"

def _verify_password(password, stored):
    try:
        salt, h = stored.split("$", 1)
        return _hash_password(password, salt) == stored
    except Exception:
        return False

def _validate_email(email):
    return bool(EMAIL_RE.match(email))

def _error(msg, code=400):
    return jsonify({"error": msg}), code

# ------------------ Geo (Nominatim) ------------------

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

HEADERS = {
    "User-Agent": "MySkyApp/1.0"
}

def search_places(q, limit=8):
    params = {
        "q": q,
        "format": "json",
        "addressdetails": 1,
        "limit": limit,
    }
    r = requests.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=10)
    r.raise_for_status()

    out = []
    for item in r.json():
        addr = item.get("address", {})
        out.append({
            "label": item.get("display_name"),
            "country": addr.get("country"),
            "city": addr.get("city") or addr.get("town") or addr.get("village"),
            "state": addr.get("state"),
            "lat": float(item["lat"]),
            "lon": float(item["lon"]),
        })
    return out

@auth_bp.route("/api/geo/search")
def geo_search():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    try:
        return jsonify(search_places(q))
    except Exception as e:
        return _error(str(e), 500)

# ------------------ Register ------------------

@auth_bp.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(force=True, silent=True) or {}

    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    country = (data.get("country") or "").strip()
    city = (data.get("city") or "").strip()
    lat = data.get("lat")
    lon = data.get("lon")

    if not _validate_email(email):
        return _error("Invalid email format")

    if len(password) < 8:
        return _error("Password must be at least 8 characters")

    if not country or not city:
        return _error("Country and city are required")

    users = _load_users()

    if any(u["email"] == email for u in users):
        return _error("Email already registered")

    user = {
        "id": f"user_{len(users)+1}_{int(datetime.utcnow().timestamp())}",
        "email": email,
        "password": _hash_password(password),
        "country": country,
        "city": city,
        "lat": lat,
        "lon": lon,
        "created_at": datetime.utcnow().isoformat() + "Z"
    }

    users.append(user)
    _save_users(users)

    return jsonify({
        "id": user["id"],
        "email": user["email"],
        "country": user["country"],
        "city": user["city"],
        "lat": user["lat"],
        "lon": user["lon"]
    })

# ------------------ Login ------------------

@auth_bp.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(force=True, silent=True) or {}

    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return _error("Email and password are required")

    users = _load_users()
    user = next((u for u in users if u["email"] == email), None)

    if not user or not _verify_password(password, user["password"]):
        return _error("Invalid email or password", 401)

    return jsonify({
        "id": user["id"],
        "email": user["email"],
        "country": user["country"],
        "city": user["city"],
        "lat": user.get("lat"),
        "lon": user.get("lon")
    })
