from flask_cors import CORS
from flask import Blueprint, request, jsonify
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
import os
import uuid
import base64
import requests
from urllib.parse import urlencode
import json

auth_bp = Blueprint("auth", __name__)
CORS(auth_bp)

# ============================================================
# Supabase REST Setup
# ============================================================

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
SUPABASE_KEY = (os.environ.get("SUPABASE_KEY") or "").strip()

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


# ============================================================
# Google OAuth
# ============================================================

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI",
    "https://observe-pro-backend.onrender.com/api/auth/google/callback"
)

FRONTEND_COMPLETE_URL = os.environ.get(
    "FRONTEND_COMPLETE_URL",
    "https://your-frontend-domain.com/google-complete"
)

TERMS_VERSION = "1.0"
PRIVACY_VERSION = "1.0"


# ============================================================
# REGISTER
# ============================================================

@auth_bp.route("/register", methods=["POST"])
def register():
    data = request.get_json() or {}

    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    city = (data.get("city") or "").strip()
    country = (data.get("country") or "").strip()
    terms_accepted_at = data.get("termsAcceptedAt")
    timezone = data.get("timezone") or "UTC"



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
        return jsonify({"error": "You must accept the Terms and Privacy Policy"}), 400

    # Check duplicate
    existing = sb_get("users", {"email": f"eq.{email}"})
    if existing:
        return jsonify({"error": "Email already registered"}), 409

    user_id = str(uuid.uuid4())

    sb_post("users", {
        "id": user_id,
        "email": email,
        "password_hash": generate_password_hash(password),
        "auth_provider": "local",
        "city": city,
        "country": country,
        "timezone": timezone,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "terms_accepted_at": terms_accepted_at,
        "terms_version": TERMS_VERSION,
        "privacy_version": PRIVACY_VERSION,
        "profile_complete": True
    })


    return jsonify({
        "id": user_id,
        "email": email,
        "city": city,
        "country": country
    }), 201


# ============================================================
# LOGIN
# ============================================================

@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.get_json() or {}

    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400

    users = sb_get("users", {"email": f"eq.{email}"})
    if not users:
        return jsonify({"error": "Invalid email or password"}), 401

    user = users[0]

    if user.get("auth_provider") == "google":
        return jsonify({"error": "Use Google login for this account"}), 401

    if not check_password_hash(user.get("password_hash", ""), password):
        return jsonify({"error": "Invalid email or password"}), 401

    needs_reconsent = (
        user.get("terms_version") != TERMS_VERSION or
        user.get("privacy_version") != PRIVACY_VERSION
    )

    return jsonify({
        "id": user["id"],
        "email": user["email"],
        "city": user.get("city", ""),
        "country": user.get("country", ""),
        "needsReconsent": needs_reconsent
    })


# ============================================================
# GOOGLE START
# ============================================================

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


# ============================================================
# GOOGLE CALLBACK
# ============================================================

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

    users = sb_get("users", {"email": f"eq.{email}"})
    user = users[0] if users else None

    if not user:
        user_id = str(uuid.uuid4())
        sb_post("users", {
            "id": user_id,
            "email": email,
            "google_id": google_id,
            "auth_provider": "google",
            "created_at": datetime.utcnow().isoformat() + "Z",
            "profile_complete": False
        })
    else:
        user_id = user["id"]

    payload = {
        "id": user_id,
        "email": email,
    }

    encoded = base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).decode()

    return "", 302, {
        "Location": f"com.observepro.space://auth?p={encoded}"
    }



# ============================================================
# COMPLETE PROFILE
# ============================================================

@auth_bp.route("/complete-profile", methods=["POST"])
def complete_profile():
    data = request.get_json() or {}

    user_id = data.get("userId")
    city = (data.get("city") or "").strip()
    country = (data.get("country") or "").strip()
    lat = data.get("lat")
    lon = data.get("lon")
    timezone = data.get("timezone") or "UTC"



    if not user_id:
        return jsonify({"success": False, "error": "Missing userId"}), 400

    if not city or not country:
        return jsonify({"success": False, "error": "City and country required"}), 400

    if lat is None or lon is None:
        return jsonify({"success": False, "error": "Latitude and longitude required"}), 400

    sb_patch(
        "users",
        {"id": f"eq.{user_id}"},
        {
             "city": city,
             "country": country,
             "lat": float(lat),
             "lon": float(lon),
             "timezone": timezone,
             "profile_complete": True
        }
    )


    return jsonify({"success": True})


# ============================================================
# DELETE ACCOUNT
# ============================================================

@auth_bp.route("/delete-account", methods=["DELETE"])
def delete_account():
    data = request.get_json() or {}
    user_id = data.get("userId")

    if not user_id:
        return jsonify({"success": False, "error": "Missing userId"}), 400

    sb_delete("users", {"id": f"eq.{user_id}"})

    return jsonify({
        "success": True,
        "message": "Account deleted permanently"
    })

