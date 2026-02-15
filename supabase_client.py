import os
import requests

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

def sb_get(path, params=None):
    return requests.get(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=HEADERS,
        params=params
    )

def sb_post(path, data):
    return requests.post(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=HEADERS,
        json=data
    )

def sb_patch(path, data, params=None):
    return requests.patch(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=HEADERS,
        params=params,
        json=data
    )

def sb_delete(path, params=None):
    return requests.delete(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=HEADERS,
        params=params
    )
