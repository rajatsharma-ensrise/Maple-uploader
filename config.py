"""
Saari settings aur secret keys yahan environment variables se aati hain.

- Render par: Render Dashboard → Service → Environment mein set karo.
- Apne computer par: project folder mein `.env` file rakho (template: `.env.example`).
  `.env` kabhi GitHub par push mat karna — `.gitignore` mein already hai.
"""

import os
import secrets

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass


def _env(name, default=""):
    return str(os.environ.get(name, default)).strip()


# ── Shopify ──────────────────────────────────────────────
SHOPIFY_SHOP         = _env("SHOPIFY_SHOP", "maplestore-in")
SHOPIFY_ACCESS_TOKEN = _env("SHOPIFY_ACCESS_TOKEN")

# ── Google Drive (videos) ────────────────────────────────
GOOGLE_API_KEY = _env("GOOGLE_API_KEY")

# ── Email (OTP + log mail) ───────────────────────────────
# BREVO_API_KEY set ho to email Brevo API (HTTPS) se jaata hai — Render Free plan ke liye zaroori,
# kyunki wahan Gmail SMTP ports block hain. Set na ho to Gmail SMTP use hota hai (local ke liye).
SENDER_EMAIL        = _env("SENDER_EMAIL")
SENDER_NAME         = _env("SENDER_NAME", "Maple Uploader")
SENDER_APP_PASSWORD = _env("SENDER_APP_PASSWORD")
BREVO_API_KEY       = _env("BREVO_API_KEY")

# ── App ──────────────────────────────────────────────────
# Set na ho to har restart par naya random key banega (sab users logout ho jaayenge).
FLASK_SECRET_KEY        = _env("FLASK_SECRET_KEY") or secrets.token_hex(32)
OTP_EXPIRY_SECONDS      = int(_env("OTP_EXPIRY_SECONDS", "300"))
SESSION_TIMEOUT_MINUTES = int(_env("SESSION_TIMEOUT_MINUTES", "30"))


def missing_settings():
    """Jo zaroori settings set nahi hain unki list."""
    missing = []
    if not SHOPIFY_ACCESS_TOKEN:
        missing.append("SHOPIFY_ACCESS_TOKEN")
    if not GOOGLE_API_KEY:
        missing.append("GOOGLE_API_KEY")
    if not SENDER_EMAIL:
        missing.append("SENDER_EMAIL")
    if not BREVO_API_KEY and not SENDER_APP_PASSWORD:
        missing.append("BREVO_API_KEY (ya local ke liye SENDER_APP_PASSWORD)")
    if not _env("FLASK_SECRET_KEY"):
        missing.append("FLASK_SECRET_KEY")
    return missing
