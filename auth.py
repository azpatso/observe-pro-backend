from flask_cors import CORS
from flask import Blueprint, request, jsonify
from pathlib import Path
import json
import uuid
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
import os
import base64
import requests
from urllib.parse import urlencode

auth_bp = Blueprint("auth", __name__)
CORS(auth_bp)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# 🔒 Force directory creation
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# 🔒 Force directory creation
DATA_DIR.mkdir(exist_ok=True)

USERS_FILE = DATA_DIR / "users.json"

print("AUTH USERS FILE:", USERS_FILE.resolve())


GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "YOUR_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "YOUR_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = "http://localhost:5000/api/auth/google/callback"
FRONTEND_COMPLETE_URL = "http://localhost:5500/google-complete.html"

# ✅ Consent versioning
TERMS_VERSION = "1.0"
PRIVACY_VERSION = "1.0"

# Ensure file exists
if not USERS_FILE.exists():
    USERS_FILE.write_text("[]", encoding="utf-8")


def load_users():
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_users(users):
    USERS_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")
    print("💾 SAVED USERS:", USERS_FILE.resolve())



@auth_bp.route("/register", methods=["POST"])
def register():
    data = request.get_json() or {}
    

    print("🔔 REGISTER HIT")
    print("📧 EMAIL:", data.get("email"))
    print("📁 USERS FILE:", USERS_FILE.resolve())

    users = load_users()
    print("👥 USERS BEFORE:", users)

    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    city = (data.get("city") or "").strip()
    country = (data.get("country") or "").strip()
    terms_accepted_at = data.get("termsAcceptedAt")


    if not email or "@" not in email:
        return jsonify({"error": "Valid email is required"}), 400

    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    if not any(c.isupper() for c in password):
        return jsonify({"error": "Password must contain at least one uppercase letter"}), 400

    if not city or len(city) < 2:
        return jsonify({"error": "City is required"}), 400

    if not country:
        return jsonify({"error": "Country is required"}), 400
    if not terms_accepted_at:
        return jsonify({
            "error": "You must accept the Terms and Privacy Policy"
        }), 400

    users = load_users()

    # Prevent duplicate emails (case-insensitive)
    if any(u.get("email", "").lower() == email for u in users):
        return jsonify({"error": "Email already registered"}), 409


    user = {
        "id": str(uuid.uuid4()),
        "email": email,
        "password_hash": generate_password_hash(password),
        "auth_provider": "local",
        "city": city,
        "country": country,
        "created_at": datetime.utcnow().isoformat() + "Z",

        # ✅ Consent metadata
        "termsAcceptedAt": terms_accepted_at,
        "termsVersion": TERMS_VERSION,
        "privacyVersion": PRIVACY_VERSION,
    }

    users.append(user)
    save_users(users)

    return jsonify({
        "id": user["id"],
        "email": user["email"],
        "city": user["city"],
        "country": user["country"],
    }), 201


@auth_bp.route("/google/start")
def google_start():
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "prompt": "select_account",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return "", 302, {"Location": url}


@auth_bp.route("/google/callback")
def google_callback():
    code = request.args.get("code")
    if not code:
        return "Missing code", 400

    token_res = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": GOOGLE_REDIRECT_URI,
        },
        timeout=10,
    )

    token_data = token_res.json()
    access_token = token_data.get("access_token")

    if not access_token:
        return "Google token exchange failed", 400

    user_res = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )

    g = user_res.json()
    google_id = g.get("id")
    email = (g.get("email") or "").lower()

    if not google_id or not email:
        return "Invalid Google profile", 400

    users = load_users()
    user = next(
        (u for u in users if u.get("google_id") == google_id or u.get("email") == email),
        None
    )

    if not user:
        user = {
            "id": str(uuid.uuid4()),
            "email": email,
            "google_id": google_id,
            "auth_provider": "google",
            "city": "",
            "country": "",
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        users.append(user)
        save_users(users)

    payload = {
        "id": user["id"],
        "email": user["email"],
        "city": user.get("city", ""),
        "country": user.get("country", ""),
    }

    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return "", 302, {"Location": f"{FRONTEND_COMPLETE_URL}?p={encoded}"}


@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.get_json() or {}

    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400

    users = load_users()
    user = next((u for u in users if u.get("email") == email), None)

    # Block Google-only accounts from password login
    if not user or user.get("auth_provider") == "google":
        return jsonify({"error": "Invalid email or password"}), 401

    if not user.get("password_hash") or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Invalid email or password"}), 401

    # ✅ Check if re-consent is required
    needs_reconsent = (
        user.get("termsVersion") != TERMS_VERSION or
        user.get("privacyVersion") != PRIVACY_VERSION
    )

    return jsonify({
        "id": user["id"],
        "email": user["email"],
        "city": user.get("city", ""),
        "country": user.get("country", ""),
        "needsReconsent": needs_reconsent
    })


@auth_bp.route("/complete-profile", methods=["POST"])
def complete_profile():

    data = request.get_json() or {}

    user_id = data.get("userId")
    city = (data.get("city") or "").strip()
    country = (data.get("country") or "").strip()
    lat = data.get("lat")
    lon = data.get("lon")

    if not user_id:
        return jsonify({"success": False, "error": "Missing userId"}), 400

    if not city or not country:
        return jsonify({"success": False, "error": "City and country are required"}), 400

    if lat is None or lon is None:
        return jsonify({"success": False, "error": "Latitude and longitude are required"}), 400

    users = load_users()
    user = next((u for u in users if u.get("id") == user_id), None)

    if not user:
        return jsonify({
            "success": False,
            "error": "User not found"
        }), 404

    # ✅ Enforce consent for Google users
    if not user.get("termsAcceptedAt"):
        return jsonify({
            "success": False,
            "error": "You must accept the Terms and Privacy Policy to continue."
        }), 403

    user["city"] = city
    user["country"] = country
    user["lat"] = float(lat)
    user["lon"] = float(lon)
    user["profileComplete"] = True

    save_users(users)

    return jsonify({
        "success": True,
        "city": user["city"],
        "country": user["country"],
        "lat": user["lat"],
        "lon": user["lon"],
    })

@auth_bp.route("/delete-account", methods=["POST"])
def delete_account():
    data = request.get_json() or {}
    user_id = data.get("userId")

    if not user_id:
        return jsonify({
            "success": False,
            "error": "Missing userId"
        }), 400

    users = load_users()
    original_count = len(users)

    users = [u for u in users if u.get("id") != user_id]

    if len(users) == original_count:
        return jsonify({
            "success": False,
            "error": "User not found"
        }), 404

    save_users(users)

    return jsonify({
        "success": True,
        "message": "Account deleted permanently"
    })
