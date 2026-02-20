from flask_cors import CORS
from flask import Blueprint, request, jsonify, redirect
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
import os
import uuid
import base64
import requests
from urllib.parse import urlencode
import json

auth_bp = Blueprint("auth", __name__)
CORS(auth_bp)

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")

def send_verification_email(to_email, token):
    if not RESEND_API_KEY:
        print("RESEND_API_KEY not set")
        return

    verify_url = f"https://observe-pro-backend.onrender.com/api/auth/verify-email?token={token}"

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <body style="margin:0;padding:0;background:#0d1117;font-family:Arial,sans-serif;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td align="center" style="padding:40px 0;">
            <table width="600" cellpadding="0" cellspacing="0" style="background:#161b22;border-radius:10px;padding:40px;color:#ffffff;">
          
              <tr>
                <td align="center" style="font-size:28px;font-weight:bold;">
                  🌌 Observe Pro
                </td>
              </tr>

              <tr>
                <td style="padding:30px 0 10px 0;font-size:20px;">
                  Verify your email address
                </td>
              </tr>

              <tr>
                <td style="color:#c9d1d9;font-size:14px;line-height:1.6;">
                  Thanks for creating an account.
                  Please confirm your email address by clicking the button below.
                  This link will expire in <strong>24 hours</strong>.
                </td>
              </tr>

              <tr>
                <td align="center" style="padding:30px 0;">
                  <a href="{verify_url}"
                     style="background:#238636;color:white;padding:14px 28px;
                            text-decoration:none;border-radius:8px;font-weight:bold;
                            display:inline-block;">
                     Verify Email
                 </a>
               </td>
             </tr>

             <tr>
               <td style="color:#8b949e;font-size:12px;">
                 If you didn’t create this account, you can safely ignore this email.
               </td>
             </tr>

           </table>
         </td>
       </tr>
     </table>
   </body>
   </html>
   """

    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": "Observe Pro <info@protosdigital.pro>",
                "to": to_email,
                "subject": "Verify your Observe Pro account",
                "html": html_content,
            },
            timeout=10,
        )

        print("Resend response:", response.status_code, response.text)

    except Exception as e:
        print("Email send failed:", e)
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

    verification_token = str(uuid.uuid4())

    expires_at = datetime.utcnow() + timedelta(hours=24)

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
        "profile_complete": True,
        "email_verified": False,
        "email_verification_token": verification_token,
        "email_verification_expires_at": expires_at.isoformat() + "Z"
    })

    send_verification_email(email, verification_token)
    return jsonify({
        "message": "Account created. Please check your email to verify your account."
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
    
    if not user.get("email_verified"):
        return jsonify({"error": "Please verify your email before logging in."}), 403

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
            "profile_complete": False,
            "email_verified": True,
            "email_verification_token": None
        })

        profile_complete = False
    else:
        user_id = user["id"]
        profile_complete = user.get("profile_complete", False)

    payload = {
        "id": user_id,
        "email": email,
        "profileComplete": profile_complete
    }

    encoded = base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).decode()

    return redirect(f"com.observepro.space://auth?p={encoded}")

    



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

@auth_bp.route("/verify-email")
def verify_email():
    token = (request.args.get("token") or "").strip()

    if not token:
        return "Invalid verification link", 400

    users = sb_get(
        "users",
        {
            "email_verification_token": f"eq.{token}"
        }
    )

    if not users:
        return "Invalid or expired verification link", 400

    user = users[0]

    # ⏳ Check expiration
    from datetime import timezone

    expires_at = user.get("email_verification_expires_at")

    if expires_at:
        try:
            # Handle both Z and +00:00 formats safely
            exp = datetime.fromisoformat(
                expires_at.replace("Z", "+00:00")
            )

            if datetime.now(timezone.utc) > exp:
                return "Verification link has expired. Please request a new one.", 400

        except Exception as e:
            print("Expiration parse error:", e)
            return "Invalid verification timestamp.", 400

    sb_patch(
        "users",
        {"id": f"eq.{user['id']}"},
        {
            "email_verified": True,
            "email_verification_token": None,
            "email_verification_expires_at": None
        }
    )

    return """
    <h2>Email verified successfully ✅</h2>
    <p>You can now return to the Observe Pro app and log in.</p>
    """

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

