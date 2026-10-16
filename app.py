"""
http://localhost:5000
"""

import os
import sys
import queue
import threading
import tempfile
import random
import re
import time
import base64
import smtplib
import traceback
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from functools import wraps

from datetime import datetime, timedelta

import requests
from flask import Flask, request, render_template_string, Response, jsonify, session, redirect, url_for

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import shopify_sync as sync
import video_sync

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
app.config["SESSION_COOKIE_NAME"] = "maple_session"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=getattr(config, "SESSION_TIMEOUT_MINUTES", 30))
# Render par site HTTPS par chalti hai — cookie sirf HTTPS par bhejo
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))

_missing = config.missing_settings()
if _missing:
    print("⚠️  Ye settings set nahi hain (env / .env): " + ", ".join(_missing))


@app.before_request
def _refresh_session():
    session.permanent = True

log_queue     = queue.Queue()
upload_running = False

# Har email ka apna OTP: { "user@x.com": {"code": "123456", "expires": 1712345678.0} }
otp_store = {}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def send_email(to_email, subject, body, attachment_name=None, attachment_bytes=None):
    """
    Email bhejo. BREVO_API_KEY set hai to Brevo API (HTTPS) se — Render Free plan par
    SMTP ports block hain, isliye wahan yahi chalega. Warna Gmail SMTP se (local).
    """
    if config.BREVO_API_KEY:
        payload = {
            "sender": {"email": config.SENDER_EMAIL, "name": config.SENDER_NAME},
            "to": [{"email": to_email}],
            "subject": subject,
            "textContent": body,
        }
        if attachment_bytes is not None:
            payload["attachment"] = [{
                "name": attachment_name,
                "content": base64.b64encode(attachment_bytes).decode("ascii"),
            }]
        r = requests.post(
            BREVO_URL,
            headers={
                "api-key": config.BREVO_API_KEY,
                "accept": "application/json",
                "content-type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if r.status_code not in (200, 201, 202):
            raise Exception(f"Brevo error {r.status_code}: {r.text[:300]}")
        return

    if attachment_bytes is None:
        msg = MIMEText(body, "plain", "utf-8")
    else:
        msg = MIMEMultipart()
        msg.attach(MIMEText(body, "plain", "utf-8"))
        attachment = MIMEApplication(attachment_bytes, Name=attachment_name)
        attachment["Content-Disposition"] = f'attachment; filename="{attachment_name}"'
        msg.attach(attachment)

    msg["Subject"] = subject
    msg["From"] = config.SENDER_EMAIL
    msg["To"] = to_email

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(config.SENDER_EMAIL, config.SENDER_APP_PASSWORD)
        server.sendmail(config.SENDER_EMAIL, [to_email], msg.as_string())


def send_otp_email(otp_code, to_email):
    send_email(
        to_email,
        "Maple Uploader - Login OTP",
        f"Your Maple Uploader login OTP: {otp_code}\n\nThis OTP is valid for {config.OTP_EXPIRY_SECONDS // 60} minutes.",
    )


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return wrapper


def send_log_email(to_email, job_title, log_lines, success, started_at, finished_at):
    """Run ka poora log .txt attachment ke roop mein login email par bhejo."""
    status_word = "SUCCESS" if success else "FAILED"
    duration = finished_at - started_at
    duration_str = str(duration).split(".")[0]
    stamp = started_at.strftime("%Y-%m-%d_%H-%M")
    safe_job = job_title.lower().replace(" ", "_")

    log_text = "\n".join(log_lines) + "\n"

    subject = f"Maple Uploader - {job_title} log - {status_word} - {started_at.strftime('%d %b %Y %H:%M')}"
    body = (
        f"Maple Uploader — {job_title}\n\n"
        f"Status   : {status_word}\n"
        f"Started  : {started_at.strftime('%d %b %Y, %H:%M:%S')}\n"
        f"Finished : {finished_at.strftime('%d %b %Y, %H:%M:%S')}\n"
        f"Duration : {duration_str}\n"
        f"Log lines: {len(log_lines)}\n\n"
        f"Full log is attached."
    )
    send_email(to_email, subject, body,
               attachment_name=f"maple_log_{safe_job}_{stamp}.txt",
               attachment_bytes=log_text.encode("utf-8"))


def keep_awake_while_running():
    """
    Render Free plan 15 minute tak koi request na aaye to app ko sula deta hai.
    Job chalte waqt app har 4 minute apne hi public URL ko ping karti hai,
    taaki lamba upload beech mein na ruke. Job khatam → ping band.
    """
    base = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base:
        return
    while upload_running:
        try:
            requests.get(f"{base}/healthz", timeout=15)
        except Exception:
            pass
        for _ in range(240):
            if not upload_running:
                return
            time.sleep(1)


def run_job(job_title, login_email, work):
    """
    Common runner: har print/log line ko live stream (log_queue) mein bhi bhejta hai
    aur run_log mein bhi jama karta hai. Run khatam hone par (success ya error)
    poora log login email par mail ho jaata hai.
    """
    global upload_running
    upload_running = True

    # Pichhle run ki bachi hui lines (agar browser beech mein disconnect hua tha) hata do
    while True:
        try:
            log_queue.get_nowait()
        except queue.Empty:
            break

    threading.Thread(target=keep_awake_while_running, daemon=True).start()

    started_at = datetime.now()
    run_log = [
        f"Maple Uploader — {job_title}",
        f"User    : {login_email or '-'}",
        f"Started : {started_at.strftime('%d %b %Y, %H:%M:%S')}",
        "",
    ]

    def emit(msg):
        msg = str(msg)
        run_log.append(msg)
        log_queue.put(msg)

    import builtins
    original_print = builtins.print

    def patched_print(*args, **kwargs):
        msg = " ".join(str(a) for a in args)
        original_print(msg)
        emit(msg)

    builtins.print = patched_print

    success = False
    try:
        work(emit)
        success = True
    except Exception as e:
        emit(f"❌ ERROR: {e}")
        emit(traceback.format_exc())
    finally:
        builtins.print = original_print

        finished_at = datetime.now()
        run_log.append("")
        run_log.append(f"Finished: {finished_at.strftime('%d %b %Y, %H:%M:%S')}")

        if login_email:
            try:
                send_log_email(login_email, job_title, run_log, success, started_at, finished_at)
                log_queue.put(f"📧 Full log emailed to {login_email}")
            except Exception as e:
                print(f"Log email failed: {e}")
                log_queue.put(f"📧 Log email failed: {e}")
        else:
            log_queue.put("📧 Log email skipped — login email not found, please logout and login again")

        log_queue.put("__DONE__")
        upload_running = False


def _banner(emit, title):
    emit("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    emit(title)
    emit("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


def run_upload(excel_path, csv_path, drive_folder_id, login_email=None):
    def work(emit):
        _banner(emit, "📂 STEP 1 — Excel → CSV Convert")
        sync.convert_excel_to_csv(excel_path, csv_path)

        _banner(emit, "🚀 STEP 2 — Shopify Upload")
        sync.CSV_FILE = csv_path
        sync.import_all()

        if drive_folder_id and str(drive_folder_id).strip():
            _banner(emit, "🎥 STEP 3 — Video Upload (Google Drive → Shopify)")
            video_sync.start_automation(drive_folder_id)
        else:
            emit("ℹ️ Drive Folder ID not provided — video upload step skipped.")

        _banner(emit, "✅ ALL DONE — Products Shopify uploaded")

    run_job("Products Upload", login_email, work)


def run_video_only(drive_folder_id, login_email=None):
    def work(emit):
        _banner(emit, "🎥 STEP 3 — Video Upload (Google Drive → Shopify)")
        video_sync.start_automation(drive_folder_id)
        _banner(emit, "✅ ALL DONE — Videos uploaded to Shopify")

    run_job("Video Upload", login_email, work)


def run_price_update(excel_path, login_email=None):
    def work(emit):
        _banner(emit, "💰 PRICE UPDATE — SKU based")
        sync.update_prices_from_excel(excel_path)
        _banner(emit, "✅ ALL DONE — Prices updated")

    run_job("Price Update", login_email, work)


def run_status_update(excel_path, login_email=None):
    def work(emit):
        _banner(emit, "🔄 STATUS UPDATE — SKU based")
        sync.update_status_from_excel(excel_path)
        _banner(emit, "✅ ALL DONE — Statuses updated")

    run_job("Status Update", login_email, work)


LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Maple Uploader — Login</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  :root{
    --bg:#f1f5f9;--surface:#ffffff;--border:#e2e8f0;
    --accent:#4f46e5;--accent-light:#eef2ff;
    --text:#0f172a;--muted:#64748b;
    --success:#059669;--error:#dc2626;--radius:14px;
    --shadow:0 1px 3px rgba(0,0,0,.1),0 1px 2px rgba(0,0,0,.06);
    --shadow-md:0 4px 6px -1px rgba(0,0,0,.1),0 2px 4px -1px rgba(0,0,0,.06);
  }
  body{
    background:var(--bg);color:var(--text);
    font-family:'Inter',sans-serif;min-height:100vh;
    display:flex;flex-direction:column;align-items:center;
    justify-content:center;padding:20px;
  }
  .brand{display:flex;align-items:center;gap:12px;margin-bottom:28px;}
  .brand img{height:38px;width:auto;}
  .brand h1{font-size:20px;font-weight:700;color:var(--text);}
  .card{
    background:var(--surface);border:1px solid var(--border);
    border-radius:var(--radius);padding:36px 32px;
    width:100%;max-width:400px;text-align:center;
    box-shadow:var(--shadow-md);
  }
  .card-title{font-size:17px;font-weight:600;margin-bottom:6px;}
  p.sub{color:var(--muted);font-size:13px;margin-bottom:24px;line-height:1.6;}
  .field{
    width:100%;padding:12px 14px;
    background:var(--bg);border:1.5px solid var(--border);
    border-radius:10px;color:var(--text);font-family:'Inter',sans-serif;
    font-size:14px;transition:border-color .2s;margin-bottom:12px;
  }
  .field.otp-field{
    font-size:22px;letter-spacing:10px;text-align:center;font-weight:600;
  }
  .field:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-light);}
  .btn{
    width:100%;padding:13px;border:none;border-radius:10px;
    font-size:14px;font-weight:600;cursor:pointer;color:#fff;
    background:var(--accent);margin-top:4px;
    transition:opacity .2s,box-shadow .2s;
    box-shadow:0 2px 4px rgba(79,70,229,.3);
  }
  .btn:hover:not(:disabled){opacity:.9;box-shadow:0 4px 8px rgba(79,70,229,.35);}
  .btn:disabled{opacity:.45;cursor:not-allowed;}
  .btn-ghost{
    background:transparent;color:var(--muted);border:1.5px solid var(--border);
    box-shadow:none;margin-top:8px;
  }
  .btn-ghost:hover:not(:disabled){background:var(--bg);color:var(--text);box-shadow:none;}
  .divider{height:1px;background:var(--border);margin:20px 0;}
  .msg{font-size:13px;margin-top:14px;min-height:18px;font-weight:500;}
  .msg.ok{color:var(--success);}
  .msg.err{color:var(--error);}
</style>
</head>
<body>
  <div class="brand">
    <img src="https://maplestore.in/cdn/shop/files/Maple_logo_286X123_5faa72df-e223-4470-b20f-8134a5d9a533.png?v=1763625931&width=300" alt="Maple"/>
    <h1>Maple Uploader</h1>
  </div>

  <div class="card">
    <p class="card-title">Sign in to continue</p>
    <p class="sub" id="subText">Enter your registered email address to receive a one-time login code.</p>

    <div id="sendStep">
      <input type="email" class="field" id="emailInput" placeholder="you@example.com" autocomplete="email"/>
      <button class="btn" id="sendBtn" onclick="sendOtp()">Send OTP →</button>
    </div>

    <div id="verifyStep" style="display:none;">
      <input type="text" class="field otp-field" id="otpInput" maxlength="6" placeholder="——————" inputmode="numeric"/>
      <button class="btn" id="verifyBtn" onclick="verifyOtp()">Verify &amp; Login</button>
      <button class="btn btn-ghost" id="resendBtn" onclick="sendOtp()">Resend OTP</button>
    </div>

    <div class="msg" id="msg"></div>
  </div>

<script>
const sendBtn    = document.getElementById('sendBtn');
const sendStep   = document.getElementById('sendStep');
const verifyStep = document.getElementById('verifyStep');
const emailInput = document.getElementById('emailInput');
const otpInput   = document.getElementById('otpInput');
const verifyBtn  = document.getElementById('verifyBtn');
const resendBtn  = document.getElementById('resendBtn');
const msg        = document.getElementById('msg');
const subText    = document.getElementById('subText');

function setMsg(text, cls) { msg.textContent = text; msg.className = 'msg ' + (cls||''); }

async function sendOtp() {
  const email = emailInput.value.trim();
  if (!email) { setMsg('Please enter your email address.', 'err'); return; }
  sendBtn.disabled = true; resendBtn.disabled = true;
  setMsg('Sending…', '');
  try {
    const res  = await fetch('/send_otp', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email}) });
    const data = await res.json();
    if (data.ok) {
      sendStep.style.display  = 'none';
      verifyStep.style.display = 'block';
      subText.innerHTML = 'OTP sent to <strong>' + email + '</strong>. Check your inbox.';
      setMsg('6-digit code on its way!', 'ok');
      otpInput.focus();
    } else {
      setMsg(data.error || 'Failed to send OTP.', 'err');
      sendBtn.disabled = false;
    }
  } catch(err) { setMsg('Network error: ' + err.message, 'err'); sendBtn.disabled = false; }
  resendBtn.disabled = false;
}

async function verifyOtp() {
  const code = otpInput.value.trim();
  if (!code) { setMsg('Please enter the OTP.', 'err'); return; }
  verifyBtn.disabled = true; setMsg('Verifying…', '');
  try {
    const res  = await fetch('/verify_otp', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({otp:code}) });
    const data = await res.json();
    if (data.ok) {
      sessionStorage.setItem('maple_logged_in', '1');
      setMsg('Login successful! Redirecting…', 'ok');
      window.location.href = '/';
    } else {
      setMsg(data.error || 'Invalid OTP.', 'err'); verifyBtn.disabled = false;
    }
  } catch(err) { setMsg('Network error: ' + err.message, 'err'); verifyBtn.disabled = false; }
}

otpInput   && otpInput.addEventListener('keydown',   e => { if (e.key==='Enter') verifyOtp(); });
emailInput && emailInput.addEventListener('keydown', e => { if (e.key==='Enter') sendOtp(); });
</script>
</body>
</html>"""


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Maple Shopify Uploader</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  :root{
    --bg:#f1f5f9;
    --surface:#ffffff;
    --border:#e2e8f0;
    --accent:#4f46e5;
    --accent-light:#eef2ff;
    --accent-dark:#3730a3;
    --teal:#0d9488;
    --teal-light:#f0fdfa;
    --amber:#b45309;
    --amber-light:#fffbeb;
    --violet:#7c3aed;
    --violet-light:#f5f3ff;
    --text:#0f172a;
    --text-2:#334155;
    --muted:#64748b;
    --success:#059669;
    --success-light:#ecfdf5;
    --error:#dc2626;
    --error-light:#fef2f2;
    --radius:12px;
    --shadow:0 1px 3px rgba(0,0,0,.08),0 1px 2px rgba(0,0,0,.05);
    --shadow-md:0 4px 6px -1px rgba(0,0,0,.08),0 2px 4px -1px rgba(0,0,0,.05);
    --shadow-lg:0 10px 15px -3px rgba(0,0,0,.08),0 4px 6px -2px rgba(0,0,0,.04);
  }

  body{
    background:var(--bg);color:var(--text);
    font-family:'Inter',sans-serif;min-height:100vh;
    display:flex;flex-direction:column;align-items:center;
    padding:32px 20px 60px;
  }

  header{
    display:flex;align-items:center;justify-content:space-between;
    width:100%;max-width:700px;margin-bottom:28px;
  }
  .brand{display:flex;align-items:center;gap:12px;}
  .brand img{height:34px;width:auto;}
  .brand-name{font-size:17px;font-weight:700;color:var(--text);}
  .brand-sub{font-size:12px;color:var(--muted);margin-top:1px;}
  .logout-btn{
    font-size:12px;font-weight:500;color:var(--muted);
    text-decoration:none;padding:6px 12px;border-radius:8px;
    border:1px solid var(--border);background:var(--surface);
    transition:all .2s;
  }
  .logout-btn:hover{color:var(--error);border-color:#fca5a5;background:#fff5f5;}

  .card{
    background:var(--surface);border:1px solid var(--border);
    border-radius:var(--radius);width:100%;max-width:700px;
    box-shadow:var(--shadow-md);margin-bottom:18px;overflow:hidden;
  }

  .tab-nav{
    display:flex;border-bottom:1px solid var(--border);
    background:var(--bg);padding:0 4px;
  }
  .tab-btn{
    flex:1;padding:14px 8px;border:none;background:none;
    cursor:pointer;font-size:13px;font-weight:500;color:var(--muted);
    border-bottom:2px solid transparent;margin-bottom:-1px;
    transition:all .2s;display:flex;align-items:center;
    justify-content:center;gap:6px;white-space:nowrap;
  }
  .tab-btn:hover:not(.active){color:var(--text-2);background:rgba(0,0,0,.03);}
  .tab-btn.active{color:var(--accent);border-bottom-color:var(--accent);background:var(--surface);}
  .tab-btn .tab-icon{font-size:15px;}

  .tab-panel{padding:28px;}
  .tab-panel.hidden{display:none;}

  .section-label{
    font-size:11px;font-weight:600;text-transform:uppercase;
    letter-spacing:.9px;color:var(--muted);margin-bottom:16px;
    display:flex;align-items:center;gap:8px;
  }
  .section-label::after{content:"";flex:1;height:1px;background:var(--border);}

  .steps{display:flex;gap:8px;margin-bottom:24px;}
  .step{
    flex:1;border-radius:10px;padding:12px;
    border:1.5px solid var(--border);background:var(--bg);
    display:flex;align-items:center;gap:8px;transition:all .3s;
  }
  .step-icon{font-size:16px;flex-shrink:0;}
  .step-label{font-size:12px;font-weight:500;color:var(--muted);transition:color .3s;}
  .step.active{border-color:var(--accent);background:var(--accent-light);}
  .step.active .step-label{color:var(--accent-dark);}
  .step.done{border-color:#a7f3d0;background:var(--success-light);}
  .step.done .step-label{color:var(--success);}
  .step.error-state{border-color:#fca5a5;background:var(--error-light);}
  .step.error-state .step-label{color:var(--error);}
  .step.skipped{opacity:.35;}

  .drop-zone{
    border:2px dashed var(--border);border-radius:var(--radius);
    padding:40px 24px;text-align:center;cursor:pointer;
    transition:all .2s;position:relative;background:var(--bg);
  }
  .drop-zone:hover,.drop-zone.drag-over{
    border-color:var(--accent);background:var(--accent-light);
  }
  .drop-zone input[type=file]{
    position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%;
  }
  .drop-icon{font-size:32px;margin-bottom:8px;}
  .drop-text{font-size:14px;font-weight:600;color:var(--text-2);margin-bottom:4px;}
  .drop-hint{font-size:12px;color:var(--muted);}
  .file-chosen{
    margin-top:12px;padding:10px 14px;background:var(--success-light);
    border:1px solid #a7f3d0;border-radius:8px;font-size:13px;
    color:var(--success);display:none;align-items:center;gap:8px;font-weight:500;
  }

  .field-group{margin-top:20px;}
  .field-label{
    font-size:12px;font-weight:600;color:var(--muted);
    text-transform:uppercase;letter-spacing:.8px;
    margin-bottom:7px;display:block;
  }
  .field-input{
    width:100%;padding:11px 14px;background:var(--bg);
    border:1.5px solid var(--border);border-radius:10px;
    color:var(--text);font-size:14px;font-family:'Inter',sans-serif;
    transition:border-color .2s,box-shadow .2s;
  }
  .field-input::placeholder{color:#94a3b8;}
  .field-input:focus{
    outline:none;border-color:var(--accent);
    box-shadow:0 0 0 3px var(--accent-light);
  }
  .field-hint{font-size:11.5px;color:var(--muted);margin-top:6px;line-height:1.5;}

  .info-box{
    background:var(--accent-light);border:1px solid #c7d2fe;
    border-radius:10px;padding:13px 15px;margin-top:16px;
    font-size:12.5px;color:var(--accent-dark);line-height:1.6;
  }
  .info-box strong{font-weight:600;}

  .btn{
    width:100%;padding:13px;border:none;border-radius:var(--radius);
    font-size:14px;font-weight:600;cursor:pointer;color:#fff;
    display:flex;align-items:center;justify-content:center;gap:8px;
    margin-top:18px;transition:opacity .2s,transform .1s,box-shadow .2s;
    letter-spacing:.01em;
  }
  .btn:hover:not(:disabled){opacity:.9;transform:translateY(-1px);}
  .btn:active:not(:disabled){transform:translateY(0);}
  .btn:disabled{opacity:.4;cursor:not-allowed;transform:none;}
  .btn-indigo{background:var(--accent);box-shadow:0 2px 4px rgba(79,70,229,.3);}
  .btn-indigo:hover:not(:disabled){box-shadow:0 4px 10px rgba(79,70,229,.35);}
  .btn-teal{background:var(--teal);box-shadow:0 2px 4px rgba(13,148,136,.3);}
  .btn-teal:hover:not(:disabled){box-shadow:0 4px 10px rgba(13,148,136,.35);}
  .btn-amber{background:#d97706;box-shadow:0 2px 4px rgba(217,119,6,.3);color:#fff;}
  .btn-amber:hover:not(:disabled){box-shadow:0 4px 10px rgba(217,119,6,.35);}
  .btn-violet{background:var(--violet);box-shadow:0 2px 4px rgba(124,58,237,.3);}
  .btn-violet:hover:not(:disabled){box-shadow:0 4px 10px rgba(124,58,237,.35);}

  .progress-wrap{
    height:4px;background:var(--border);border-radius:4px;
    margin-top:14px;overflow:hidden;display:none;
  }
  .progress-bar{
    height:100%;width:0%;transition:width .5s ease;border-radius:4px;
  }
  .bar-indigo{background:linear-gradient(90deg,var(--accent),#818cf8);}
  .bar-teal{background:linear-gradient(90deg,var(--teal),#2dd4bf);}
  .bar-amber{background:linear-gradient(90deg,#d97706,#fbbf24);}
  .bar-violet{background:linear-gradient(90deg,var(--violet),#a78bfa);}

  .console-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:22px;width:100%;max-width:700px;box-shadow:var(--shadow);display:none;}
  .console-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;}
  .console-title{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:var(--muted);}
  .live-dot{width:7px;height:7px;background:var(--success);border-radius:50%;animation:pulse 1.2s infinite;display:none;}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.2}}
  #console{
    background:#0f172a;border-radius:8px;padding:14px 16px;
    height:260px;overflow-y:auto;
    font-family:'JetBrains Mono',monospace;font-size:12px;line-height:2;
  }
  .log-line{display:block;color:#475569;}
  .log-line.product{color:#e2e8f0;}
  .log-line.ok{color:#34d399;}
  .log-line.fail{color:#f87171;}

  .status-badge{
    display:none;align-items:center;gap:10px;
    padding:12px 16px;border-radius:10px;
    font-size:13px;font-weight:500;margin-top:14px;
  }
  .status-badge.success{background:var(--success-light);color:var(--success);border:1px solid #a7f3d0;}
  .status-badge.error{background:var(--error-light);color:var(--error);border:1px solid #fca5a5;}

  /* ── Status pill (active/draft/archived) badges in hint ── */
  .pill{
    display:inline-block;padding:1px 8px;border-radius:999px;
    font-size:11px;font-weight:600;margin:0 2px;
  }
  .pill-green{background:#dcfce7;color:#15803d;}
  .pill-slate{background:#f1f5f9;color:#475569;border:1px solid var(--border);}
  .pill-red{background:#fee2e2;color:#991b1b;}
</style>
</head>
<body>

<header>
  <div class="brand">
    <img src="https://maplestore.in/cdn/shop/files/Maple_logo_286X123_5faa72df-e223-4470-b20f-8134a5d9a533.png?v=1763625931&width=300" alt="Maple"/>
    <div>
      <div class="brand-name">Maple Uploader</div>
      <div class="brand-sub">Shopify sync dashboard</div>
    </div>
  </div>
  <a href="/logout" class="logout-btn">🔒 Logout</a>
</header>

<div class="card">

  <!-- Tab navigation -->
  <nav class="tab-nav">
    <button class="tab-btn active" data-tab="products" onclick="switchTab('products')">
      <span class="tab-icon">📦</span> Products
    </button>
    <button class="tab-btn" data-tab="videos" onclick="switchTab('videos')">
      <span class="tab-icon">🎥</span> Videos
    </button>
    <button class="tab-btn" data-tab="prices" onclick="switchTab('prices')">
      <span class="tab-icon">💰</span> Prices
    </button>
    <button class="tab-btn" data-tab="status" onclick="switchTab('status')">
      <span class="tab-icon">🔄</span> Status
    </button>
  </nav>

  <div id="tab-products" class="tab-panel">

    <div class="steps">
      <div class="step" id="step1"><span class="step-icon">📂</span><span class="step-label">Excel selected</span></div>
      <div class="step" id="stepMid"><span class="step-icon">⚡</span><span class="step-label">Processing</span></div>
      <div class="step" id="step2"><span class="step-icon">🛍️</span><span class="step-label">Shopify</span></div>
      <div class="step" id="step3"><span class="step-icon">🎥</span><span class="step-label">Videos</span></div>
    </div>

    <div class="drop-zone" id="dropZone">
      <input type="file" id="fileInput" accept=".xlsx,.xls"/>
      <div class="drop-icon">📊</div>
      <div class="drop-text">Drop your Excel file here</div>
      <div class="drop-hint">or click to browse — .xlsx / .xls</div>
    </div>
    <div class="file-chosen" id="fileChosen">
      <span>📄</span><span id="fileName">—</span>
    </div>

    <div class="field-group">
      <label class="field-label">Google Drive Folder ID <span style="font-weight:400;text-transform:none;">(optional)</span></label>
      <input type="text" class="field-input" id="driveFolderId" placeholder="e.g. 1C1K99uj5U8YwOFRLawC1bAzO6-ImgJf7"/>
      <div class="field-hint">Leave empty to skip video upload. When provided, .mp4 files are matched by SKU and attached to Shopify products.</div>
    </div>

    <button class="btn btn-indigo" id="uploadBtn" disabled onclick="startUpload()">
      <span>🚀</span> Upload to Shopify
    </button>
    <div class="progress-wrap" id="progressWrap"><div class="progress-bar bar-indigo" id="progressBar"></div></div>
  </div>

  <!-- ════════ TAB: VIDEOS ════════ -->
  <div id="tab-videos" class="tab-panel hidden">

    <div class="field-group" style="margin-top:0;">
      <label class="field-label">Google Drive Folder ID</label>
      <input type="text" class="field-input" id="videoOnlyFolderId" placeholder="e.g. 1C1K99uj5U8YwOFRLawC1bAzO6-ImgJf7"/>
      <div class="field-hint">Each .mp4 filename must match the product SKU exactly. Videos are attached and set as cover media.</div>
    </div>

    <button class="btn btn-teal" id="videoOnlyBtn" onclick="startVideoOnlyUpload()">
      <span>🎥</span> Upload Videos Only
    </button>
    <div class="progress-wrap" id="videoProgressWrap"><div class="progress-bar bar-teal" id="videoProgressBar"></div></div>
  </div>

  <div id="tab-prices" class="tab-panel hidden">

    <div class="drop-zone" id="priceDropZone">
      <input type="file" id="priceFileInput" accept=".xlsx,.xls"/>
      <div class="drop-icon">💰</div>
      <div class="drop-text">Drop your Price Excel here</div>
      <div class="drop-hint">or click to browse — requires columns: SKU · Price · Compare At Price (optional)</div>
    </div>
    <div class="file-chosen" id="priceFileChosen">
      <span>📄</span><span id="priceFileName">—</span>
    </div>

    <div class="info-box">
      <strong>Required columns:</strong> <code>SKU</code> (or <code>Variant SKU</code>) and <code>Price</code> (or <code>Variant Price</code>).
      Optionally include <code>Compare At Price</code> to set a strikethrough price.
    </div>

    <button class="btn btn-amber" id="priceUpdateBtn" disabled onclick="startPriceUpdate()">
      <span>💰</span> Update Prices
    </button>
    <div class="progress-wrap" id="priceProgressWrap"><div class="progress-bar bar-amber" id="priceProgressBar"></div></div>
  </div>

  <div id="tab-status" class="tab-panel hidden">

    <div class="drop-zone" id="statusDropZone">
      <input type="file" id="statusFileInput" accept=".xlsx,.xls"/>
      <div class="drop-icon">🔄</div>
      <div class="drop-text">Drop your Status Excel here</div>
      <div class="drop-hint">or click to browse — requires columns: SKU · Status</div>
    </div>
    <div class="file-chosen" id="statusFileChosen">
      <span>📄</span><span id="statusFileName">—</span>
    </div>

    <div class="info-box">
      <strong>Required columns:</strong> <code>SKU</code> (or <code>Variant SKU</code>) and <code>Status</code>.
      Valid status values:
      <span class="pill pill-green">active</span>
      <span class="pill pill-slate">draft</span>
      <span class="pill pill-red">archived</span>
      — matched by SKU, one product updated once per run.
    </div>

    <button class="btn btn-violet" id="statusUpdateBtn" disabled onclick="startStatusUpdate()">
      <span>🔄</span> Update Statuses
    </button>
    <div class="progress-wrap" id="statusProgressWrap"><div class="progress-bar bar-violet" id="statusProgressBar"></div></div>
  </div>

</div><!-- /card -->

<div class="console-card" id="consoleCard">
  <div class="console-header">
    <span class="console-title">Live Logs</span>
    <div class="live-dot" id="liveDot"></div>
  </div>
  <div id="console"></div>
  <div class="status-badge" id="statusBadge"></div>
</div>


<script>
if (!sessionStorage.getItem('maple_logged_in')) { window.location.href = '/logout'; }

let selectedFile       = null;
let selectedPriceFile  = null;
let selectedStatusFile = null;
let evtSource          = null;
let currentTab         = 'products';

const fileInput         = document.getElementById('fileInput');
const dropZone          = document.getElementById('dropZone');
const uploadBtn         = document.getElementById('uploadBtn');
const fileChosen        = document.getElementById('fileChosen');
const fileName          = document.getElementById('fileName');
const driveFolderId     = document.getElementById('driveFolderId');

const videoOnlyBtn      = document.getElementById('videoOnlyBtn');
const videoOnlyFolderId = document.getElementById('videoOnlyFolderId');

const priceFileInput    = document.getElementById('priceFileInput');
const priceDropZone     = document.getElementById('priceDropZone');
const priceFileChosen   = document.getElementById('priceFileChosen');
const priceFileName     = document.getElementById('priceFileName');
const priceUpdateBtn    = document.getElementById('priceUpdateBtn');

const statusFileInput   = document.getElementById('statusFileInput');
const statusDropZone    = document.getElementById('statusDropZone');
const statusFileChosen  = document.getElementById('statusFileChosen');
const statusFileName    = document.getElementById('statusFileName');
const statusUpdateBtn   = document.getElementById('statusUpdateBtn');

const consoleEl         = document.getElementById('console');
const consoleCard       = document.getElementById('consoleCard');
const statusBadge       = document.getElementById('statusBadge');
const liveDot           = document.getElementById('liveDot');
const step1             = document.getElementById('step1');
const stepMid           = document.getElementById('stepMid');
const step2             = document.getElementById('step2');
const step3             = document.getElementById('step3');

function switchTab(tabId) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tabId));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.add('hidden'));
  document.getElementById('tab-' + tabId).classList.remove('hidden');
  currentTab = tabId;
}

function makeDrop(zone, input, onFile) {
  input.addEventListener('change', e => { if (e.target.files[0]) onFile(e.target.files[0]); });
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag-over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
  zone.addEventListener('drop', e => {
    e.preventDefault(); zone.classList.remove('drag-over');
    if (e.dataTransfer.files[0]) onFile(e.dataTransfer.files[0]);
  });
}

makeDrop(dropZone, fileInput, f => {
  selectedFile = f; fileName.textContent = f.name;
  fileChosen.style.display = 'flex'; uploadBtn.disabled = false;
  step1.className = 'step done'; statusBadge.style.display = 'none';
});
makeDrop(priceDropZone, priceFileInput, f => {
  selectedPriceFile = f; priceFileName.textContent = f.name;
  priceFileChosen.style.display = 'flex'; priceUpdateBtn.disabled = false;
  statusBadge.style.display = 'none';
});
makeDrop(statusDropZone, statusFileInput, f => {
  selectedStatusFile = f; statusFileName.textContent = f.name;
  statusFileChosen.style.display = 'flex'; statusUpdateBtn.disabled = false;
  statusBadge.style.display = 'none';
});

function clearConsole() { consoleEl.innerHTML = ''; }
function addLog(text, cls) {
  const span = document.createElement('span');
  span.className = 'log-line ' + (cls || '');
  span.textContent = text;
  consoleEl.appendChild(span);
  consoleEl.scrollTop = consoleEl.scrollHeight;
}
function setAllBtns(disabled) {
  uploadBtn.disabled = videoOnlyBtn.disabled = priceUpdateBtn.disabled = statusUpdateBtn.disabled = disabled;
}

function parseLog(raw) {
  let m = raw.match(/Creating new product[: ]+(.+)/i);
  if (m) return { text: 'Uploading: ' + m[1].trim(), cls: 'product' };
  m = raw.match(/Product created.*ID[: ]+(\d+)/i);
  if (m) return { text: 'Done — Product ID ' + m[1], cls: 'ok' };
  m = raw.match(/File:\s*(.+)/i);
  if (m) return { text: 'Video file: ' + m[1].trim(), cls: 'product' };
  if (raw.includes('Video set as first position') || raw.includes('pehli position'))
    return { text: 'Video attached & set as cover', cls: 'ok' };
  if (raw.includes('Drive Folder ID not provided') || raw.includes('skipped'))
    return { text: raw.replace(/[^\x20-\x7E]/g, '').trim(), cls: '' };
  if (raw.includes('❌') || raw.toLowerCase().includes('failed') || raw.toLowerCase().includes('error'))
    return { text: raw.replace(/[^\x20-\x7E]/g, '').trim(), cls: 'fail' };
  if (raw.includes('ALL DONE')) return { text: 'All done!', cls: 'ok' };
  return null;
}

function parsePriceLog(raw) {
  let m = raw.match(/SKU:\s*(.+?)\s*→/);
  if (m) return { text: 'Checking SKU: ' + m[1].trim(), cls: 'product' };
  if (raw.includes('Price updated') || raw.includes('price updated'))
    return { text: raw.replace(/[^\x20-\x7E]/g, '').trim(), cls: 'ok' };
  if (raw.includes('nahi mila') || raw.includes('FAILED'))
    return { text: raw.replace(/[^\x20-\x7E]/g, '').trim(), cls: 'fail' };
  if (raw.includes('SUMMARY') || raw.includes('Updated') || raw.includes('Skipped') || raw.includes('Failed'))
    return { text: raw.replace(/[^\x20-\x7E]/g, '').trim(), cls: '' };
  if (raw.includes('ERROR')) return { text: raw.replace(/[^\x20-\x7E]/g, '').trim(), cls: 'fail' };
  if (raw.includes('ALL DONE')) return { text: 'All done!', cls: 'ok' };
  return null;
}

function parseStatusLog(raw) {
  let m = raw.match(/SKU:\s*(.+?)\s*→/);
  if (m) return { text: 'Checking SKU: ' + m[1].trim(), cls: 'product' };
  if (raw.includes('Status updated') || raw.includes('status updated'))
    return { text: raw.replace(/[^\x20-\x7E]/g, '').trim(), cls: 'ok' };
  if (raw.includes('Invalid status') || raw.includes('nahi mila') || raw.includes('FAILED'))
    return { text: raw.replace(/[^\x20-\x7E]/g, '').trim(), cls: 'fail' };
  if (raw.includes('SUMMARY') || raw.includes('Updated') || raw.includes('Skipped') || raw.includes('Failed'))
    return { text: raw.replace(/[^\x20-\x7E]/g, '').trim(), cls: '' };
  if (raw.includes('ERROR')) return { text: raw.replace(/[^\x20-\x7E]/g, '').trim(), cls: 'fail' };
  if (raw.includes('ALL DONE') || raw.includes('STATUS UPDATE: DONE')) return { text: 'All done!', cls: 'ok' };
  return null;
}

function listenStream(progressBar, parser, onDone) {
  if (evtSource) { evtSource.close(); evtSource = null; }
  evtSource = new EventSource('/stream');

  evtSource.onmessage = e => {
    const raw = e.data;
    if (raw === '__DONE__') {
      evtSource.close(); evtSource = null;
      progressBar.style.width = '100%';
      onDone(true); return;
    }
    if (raw === '__LOST__') {
      evtSource.close(); evtSource = null;
      addLog('Server restarted — upload beech mein ruk gaya. Shopify check karke dobara chalaiye.', 'fail');
      onDone(false); return;
    }
    const cur = parseFloat(progressBar.style.width) || 15;
    if (cur < 92) progressBar.style.width = (cur + 0.4) + '%';
    if (raw.startsWith('📧')) {
      addLog('Mail: ' + raw.replace(/[^\x20-\x7E]/g, '').trim(), /failed|skipped/i.test(raw) ? 'fail' : 'ok');
      return;
    }
    const parsed = parser(raw);
    if (parsed) addLog(parsed.text, parsed.cls);
  };

  evtSource.onerror = () => {
    // Network thodi der ke liye toota ho to browser khud reconnect karta hai — job server par chalta rehta hai
    if (evtSource && evtSource.readyState === EventSource.CONNECTING) {
      addLog('Connection dropped — reconnecting…', '');
      return;
    }
    if (evtSource) { evtSource.close(); evtSource = null; }
    onDone(false);
  };
}

function showFinish(success, successMsg, errorMsg) {
  setAllBtns(false);
  liveDot.style.display = 'none';
  statusBadge.style.display = 'flex';
  if (success) {
    statusBadge.className = 'status-badge success';
    statusBadge.innerHTML = '✅ &nbsp;' + successMsg;
  } else {
    statusBadge.className = 'status-badge error';
    statusBadge.innerHTML = '❌ &nbsp;' + errorMsg;
  }
}

function prepareRun(progressWrap, progressBar) {
  clearConsole();
  statusBadge.style.display = 'none';
  consoleCard.style.display = 'block';
  progressWrap.style.display = 'block';
  progressBar.style.width = '5%';
  setAllBtns(true);
  liveDot.style.display = 'block';
}

async function startUpload() {
  if (!selectedFile) return;
  const pw = document.getElementById('progressWrap');
  const pb = document.getElementById('progressBar');
  const folderId = driveFolderId.value.trim();

  prepareRun(pw, pb);
  uploadBtn.innerHTML = '<span>⏳</span> Running…';

  step1.className = 'step done';
  stepMid.className = 'step active';
  step2.className = 'step';
  step3.className = folderId ? 'step' : 'step skipped';

  let shopifyStarted = false, videoStarted = false;

  const formData = new FormData();
  formData.append('file', selectedFile);
  formData.append('drive_folder_id', folderId);

  try {
    const res  = await fetch('/upload', { method:'POST', body:formData });
    const data = await res.json();
    if (!data.ok) { addLog(data.error || 'Upload not started.', 'fail'); finishProducts(false, !!folderId); return; }
  } catch(err) { addLog('Network error: ' + err.message, 'fail'); finishProducts(false, !!folderId); return; }

  pb.style.width = '15%';

  listenStream(pb, raw => {
    if (raw.includes('STEP 2') || raw.includes('Shopify Upload')) {
      if (!shopifyStarted) { shopifyStarted=true; stepMid.className='step done'; step2.className='step active'; }
    }
    if (raw.includes('STEP 3') || raw.includes('Video Upload')) {
      if (!videoStarted) { videoStarted=true; step2.className='step done'; step3.className='step active'; }
    }
    return parseLog(raw);
  }, ok => finishProducts(ok, !!folderId));
}

function finishProducts(success, hadFolder) {
  uploadBtn.innerHTML = '<span>🚀</span> Upload to Shopify';
  step1.className = 'step done';
  stepMid.className = step2.className = 'step ' + (success ? 'done' : 'error-state');
  step3.className = 'step ' + (hadFolder ? (success ? 'done' : 'error-state') : 'skipped');
  showFinish(success, 'All products uploaded to Shopify!', 'Some errors occurred — check logs above.');
}

async function startVideoOnlyUpload() {
  const folderId = videoOnlyFolderId.value.trim();
  if (!folderId) { alert('Please enter the Google Drive Folder ID.'); return; }
  const pw = document.getElementById('videoProgressWrap');
  const pb = document.getElementById('videoProgressBar');

  prepareRun(pw, pb);
  videoOnlyBtn.innerHTML = '<span>⏳</span> Running…';

  step1.className = step2.className = stepMid.className = 'step skipped';
  step3.className = 'step active';

  try {
    const res  = await fetch('/upload_videos', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({drive_folder_id:folderId}) });
    const data = await res.json();
    if (!data.ok) { addLog(data.error || 'Video upload not started.', 'fail'); finishVideos(false); return; }
  } catch(err) { addLog('Network error: ' + err.message, 'fail'); finishVideos(false); return; }

  pb.style.width = '15%';
  listenStream(pb, parseLog, ok => finishVideos(ok));
}

function finishVideos(success) {
  videoOnlyBtn.innerHTML = '<span>🎥</span> Upload Videos Only';
  step3.className = 'step ' + (success ? 'done' : 'error-state');
  showFinish(success, 'Videos uploaded to Shopify!', 'Some errors occurred — check logs above.');
}

async function startPriceUpdate() {
  if (!selectedPriceFile) return;
  const pw = document.getElementById('priceProgressWrap');
  const pb = document.getElementById('priceProgressBar');

  prepareRun(pw, pb);
  priceUpdateBtn.innerHTML = '<span>⏳</span> Updating…';
  step1.className = step2.className = stepMid.className = step3.className = 'step skipped';

  const formData = new FormData();
  formData.append('file', selectedPriceFile);

  try {
    const res  = await fetch('/upload_prices', { method:'POST', body:formData });
    const data = await res.json();
    if (!data.ok) { addLog(data.error || 'Price update not started.', 'fail'); finishPrices(false); return; }
  } catch(err) { addLog('Network error: ' + err.message, 'fail'); finishPrices(false); return; }

  pb.style.width = '15%';
  listenStream(pb, parsePriceLog, ok => finishPrices(ok));
}

function finishPrices(success) {
  priceUpdateBtn.innerHTML = '<span>💰</span> Update Prices';
  showFinish(success, 'Price update completed!', 'Some errors occurred — check logs above.');
}

async function startStatusUpdate() {
  if (!selectedStatusFile) return;
  const pw = document.getElementById('statusProgressWrap');
  const pb = document.getElementById('statusProgressBar');

  prepareRun(pw, pb);
  statusUpdateBtn.innerHTML = '<span>⏳</span> Updating…';
  step1.className = step2.className = stepMid.className = step3.className = 'step skipped';

  const formData = new FormData();
  formData.append('file', selectedStatusFile);

  try {
    const res  = await fetch('/upload_status', { method:'POST', body:formData });
    const data = await res.json();
    if (!data.ok) { addLog(data.error || 'Status update not started.', 'fail'); finishStatus(false); return; }
  } catch(err) { addLog('Network error: ' + err.message, 'fail'); finishStatus(false); return; }

  pb.style.width = '15%';
  listenStream(pb, parseStatusLog, ok => finishStatus(ok));
}

function finishStatus(success) {
  statusUpdateBtn.innerHTML = '<span>🔄</span> Update Statuses';
  showFinish(success, 'Status update completed!', 'Some errors occurred — check logs above.');
}
</script>
</body>
</html>"""


@app.route("/login")
def login_page():
    if session.get("authenticated"):
        return redirect(url_for("index"))
    return render_template_string(LOGIN_HTML)


@app.route("/send_otp", methods=["POST"])
def send_otp():
    data = request.get_json(silent=True) or {}
    to_email = str(data.get("email", "")).strip().lower()

    if not to_email:
        return jsonify({"ok": False, "error": "Please enter your email address"})

    if not EMAIL_RE.match(to_email):
        return jsonify({"ok": False, "error": "Please enter a valid email address"})

    code = f"{random.randint(0, 999999):06d}"
    otp_store[to_email] = {"code": code, "expires": time.time() + config.OTP_EXPIRY_SECONDS}
    session["pending_email"] = to_email
    try:
        send_otp_email(code, to_email)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error sending OTP email: {e}"})


@app.route("/verify_otp", methods=["POST"])
def verify_otp():
    data = request.get_json(silent=True) or {}
    entered = str(data.get("otp", "")).strip()

    email = session.get("pending_email")
    entry = otp_store.get(email) if email else None
    if not entry:
        return jsonify({"ok": False, "error": "Please request an OTP first"})

    if time.time() > entry["expires"]:
        otp_store.pop(email, None)
        return jsonify({"ok": False, "error": "OTP expired — please resend"})

    if entered != entry["code"]:
        return jsonify({"ok": False, "error": "Incorrect OTP"})

    otp_store.pop(email, None)
    session.pop("pending_email", None)
    session["authenticated"] = True
    session["email"] = email
    return jsonify({"ok": True})


@app.route("/logout")
def logout():
    session.pop("authenticated", None)
    session.pop("email", None)
    return redirect(url_for("login_page"))


@app.route("/")
@login_required
def index():
    return render_template_string(HTML)


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    global upload_running
    if upload_running:
        return jsonify({"ok": False, "error": "An upload is already in progress"})

    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "File not found"})

    drive_folder_id = request.form.get("drive_folder_id", "").strip()

    suffix = ".xlsx" if f.filename.lower().endswith(".xlsx") else ".xls"
    tmp_excel = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    f.save(tmp_excel.name)
    tmp_excel.close()

    tmp_csv = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
    tmp_csv.close()

    t = threading.Thread(target=run_upload, args=(tmp_excel.name, tmp_csv.name, drive_folder_id, session.get("email")), daemon=True)
    upload_running = True   # thread start hone se pehle hi lock, taaki do upload ek saath na chalein
    t.start()
    return jsonify({"ok": True})


@app.route("/upload_videos", methods=["POST"])
@login_required
def upload_videos():
    global upload_running
    if upload_running:
        return jsonify({"ok": False, "error": "An upload is already in progress"})

    data = request.get_json(silent=True) or {}
    drive_folder_id = str(data.get("drive_folder_id", "")).strip()

    if not drive_folder_id:
        return jsonify({"ok": False, "error": "Drive Folder ID is required"})

    t = threading.Thread(target=run_video_only, args=(drive_folder_id, session.get("email")), daemon=True)
    upload_running = True   # thread start hone se pehle hi lock, taaki do upload ek saath na chalein
    t.start()
    return jsonify({"ok": True})


@app.route("/upload_prices", methods=["POST"])
@login_required
def upload_prices():
    global upload_running
    if upload_running:
        return jsonify({"ok": False, "error": "An upload is already in progress"})

    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "File not found"})

    suffix = ".xlsx" if f.filename.lower().endswith(".xlsx") else ".xls"
    tmp_excel = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    f.save(tmp_excel.name)
    tmp_excel.close()

    t = threading.Thread(target=run_price_update, args=(tmp_excel.name, session.get("email")), daemon=True)
    upload_running = True   # thread start hone se pehle hi lock, taaki do upload ek saath na chalein
    t.start()
    return jsonify({"ok": True})


@app.route("/upload_status", methods=["POST"])
@login_required
def upload_status():
    global upload_running
    if upload_running:
        return jsonify({"ok": False, "error": "An upload is already in progress"})

    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "File not found"})

    suffix = ".xlsx" if f.filename.lower().endswith(".xlsx") else ".xls"
    tmp_excel = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    f.save(tmp_excel.name)
    tmp_excel.close()

    t = threading.Thread(target=run_status_update, args=(tmp_excel.name, session.get("email")), daemon=True)
    upload_running = True   # thread start hone se pehle hi lock, taaki do upload ek saath na chalein
    t.start()
    return jsonify({"ok": True})


@app.route("/stream")
@login_required
def stream():
    def generate():
        # Server restart ho gaya ho (Render free plan par ho sakta hai) to koi job nahi chal raha
        if not upload_running and log_queue.empty():
            yield "data: __LOST__\n\n"
            return
        while True:
            try:
                msg = log_queue.get(timeout=20)
                yield f"data: {msg}\n\n"
                if msg == "__DONE__":
                    break
            except queue.Empty:
                if not upload_running and log_queue.empty():
                    yield "data: __LOST__\n\n"
                    break
                # SSE comment — browser ise ignore karta hai, bas connection zinda rehta hai
                yield ": keepalive\n\n"
    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(" Maple Shopify Uploader")
    print(f"    http://localhost:{port} ")
    app.run(debug=False, threaded=True, host="0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1", port=port)
