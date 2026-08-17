import json
import os
import random
import threading
from pathlib import Path
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, render_template_string, request, session
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())

# ---- Cấu hình ----
KEY_LIFETIME = timedelta(days=1)           # Thời hạn 1 key kể từ lúc cấp
VERIFY_LIFETIME = timedelta(days=1)        # Vượt link4m 1 lần có hiệu lực trong 24h
PENDING_TOKEN_MAX_AGE = 60 * 60            # Token vượt link còn hiệu lực trong 1 giờ (giây)

LINK4M_API_TOKEN = os.environ.get("LINK4M_API_TOKEN", "69902dcc482df052bb6c2347")
LINK4M_API_URL = "https://link4m.co/api-shorten/v2"

# Token vượt link được TỰ KÝ (không lưu trong bộ nhớ server) để không bị mất
# khi Render restart/sleep giữa lúc người dùng đang vượt quảng cáo link4m.
_verify_serializer = URLSafeTimedSerializer(app.secret_key, salt="frox-verify")

# Đọc danh sách key từ file keys.json
KEYS_FILE = Path(__file__).parent / "keys.json"
with open(KEYS_FILE, "r", encoding="utf-8") as f:
    ALL_KEYS = json.load(f)["keys"]

# Trạng thái dùng chung (in-memory, share cho mọi request)
_lock = threading.Lock()
_state = {
    "available": ALL_KEYS.copy(),
    "issued": {},          # key -> datetime cấp ra (còn hạn, đang dùng)
    "used_count": 0,
    "created_count": len(ALL_KEYS),
    "logs": [],
}


def _add_log(msg: str):
    time_str = datetime.now().strftime("%H:%M:%S")
    _state["logs"].append(f"[{time_str}] {msg}")
    if len(_state["logs"]) > 20:
        _state["logs"].pop(0)


def _sweep_expired():
    """Thu hồi các key đã quá 24h, trả lại vào kho available."""
    now = datetime.now()
    expired = [k for k, issued_at in _state["issued"].items()
               if now - issued_at >= KEY_LIFETIME]
    for k in expired:
        del _state["issued"][k]
        _state["available"].append(k)
        _add_log(f"⏳ Key hết hạn (24h), thu hồi: {k}")


def _is_verified() -> bool:
    verified_at = session.get("verified_at")
    if not verified_at:
        return False
    verified_at = datetime.fromisoformat(verified_at)
    return datetime.now() - verified_at < VERIFY_LIFETIME


def _issue_key_locked():
    """Cấp 1 key mới. PHẢI gọi bên trong _lock. Trả về dict key hoặc None nếu hết key."""
    _sweep_expired()
    if not _state["available"]:
        _add_log("⚠ Hết key khả dụng, vui lòng chờ key hết hạn")
        return None

    idx = random.randrange(len(_state["available"]))
    key = _state["available"].pop(idx)
    issued_at = datetime.now()
    expires_at = issued_at + KEY_LIFETIME
    _state["issued"][key] = issued_at
    _state["used_count"] += 1
    _add_log(f"📌 Lấy key: {key} (hết hạn: {expires_at.strftime('%H:%M:%S %d/%m')})")

    return {
        "key": key,
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "created": _state["created_count"],
        "used": _state["used_count"],
        "remaining": len(_state["available"]),
        "logs": list(_state["logs"][-10:]),
    }


# =====================================================================
# GIAO DIỆN (HTML nhúng thẳng trong file, không dùng thư mục templates)
# =====================================================================

INDEX_HTML = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FROX PROXY</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box;font-family:'Courier New',monospace}
        body{background:#0a0e14;display:flex;justify-content:center;align-items:center;min-height:100vh;padding:15px}
        .container{background:#121a24;border:1px solid #2a3a4a;border-radius:14px;padding:30px 28px;max-width:520px;width:100%;box-shadow:0 10px 40px rgba(0,0,0,0.9)}
        .title{color:#7ab8f0;font-size:20px;font-weight:bold;text-align:center;letter-spacing:3px;border-bottom:1px solid #1a2a3a;padding-bottom:15px;margin-bottom:18px}
        .title small{color:#4a6a7a;font-size:11px;display:block;font-weight:normal;letter-spacing:1px}
        .stats{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:15px}
        .stat-box{background:#0b0e14;border:1px solid #1a2a3a;border-radius:6px;padding:8px 6px;text-align:center}
        .stat-box .label{color:#4a6a7a;font-size:9px;text-transform:uppercase;letter-spacing:0.5px}
        .stat-box .value{color:#b8d8e8;font-size:16px;font-weight:bold}
        .stat-box .value.green{color:#6fcf97}
        .verify-badge{text-align:center;font-size:11px;padding:6px;border-radius:6px;margin-bottom:12px}
        .verify-badge.ok{background:#123a2a;color:#6fcf97;border:1px solid #2a5a3a}
        .verify-badge.no{background:#3a2a12;color:#f0c060;border:1px solid #5a4a2a}
        .btn-group{display:flex;gap:10px;margin-top:15px}
        .btn{flex:1;padding:12px 10px;border-radius:6px;font-size:14px;font-weight:bold;cursor:pointer;transition:0.2s;text-align:center;border:1px solid #2a4a5a;background:#1a2a3a;color:#c0d8e8}
        .btn:hover{background:#2a3a4a;border-color:#4a7a8a;color:#fff}
        .btn-primary{background:#1a4a3a;border-color:#2a7a5a;color:#8fdfb0}
        .btn-primary:hover{background:#2a5a4a;border-color:#4a9a7a}
        .btn-primary:disabled{opacity:0.5;cursor:not-allowed}
        .btn-copy{background:#1a3a4a;border-color:#2a5a7a;color:#7ac0e0}
        .btn-copy:hover{background:#2a4a5a;border-color:#4a7aaa}
        .footer{color:#3a5a6a;font-size:10px;text-align:center;border-top:1px solid #1a2a3a;padding-top:14px;margin-top:18px}
        .log{background:#0b0e14;border-radius:4px;padding:6px 10px;border:1px solid #1a2a3a;margin-top:12px;color:#4a6a7a;font-size:10px;max-height:50px;overflow-y:auto}
        .log-entry{color:#5a7a8a}
    </style>
</head>
<body>
<div class="container">
    <div class="title">
        ⚡ FROX PROXY
        <small>Hệ thống Key tự động - Lấy key ngay khi có người dùng</small>
    </div>
    <div style="color:#5a7a8a;font-size:12px;text-align:center;margin-bottom:12px">
        Ngày: <strong style="color:#8ab0c8" id="dateDisplay"></strong>
    </div>
    <div class="verify-badge no" id="verifyBadge">🔒 Bạn cần vượt link 1 lần để lấy key (hiệu lực 24h)</div>
    <div class="stats">
        <div class="stat-box"><div class="label">Đã tạo</div><div class="value" id="created">0</div></div>
        <div class="stat-box"><div class="label">Đã dùng</div><div class="value" id="used">0</div></div>
        <div class="stat-box"><div class="label">Còn lại</div><div class="value green" id="remaining">0</div></div>
    </div>
    <div class="btn-group">
        <button class="btn btn-primary" id="getKeyBtn">🔗 VƯỢT LINK ĐỂ LẤY KEY</button>
        <button class="btn btn-copy" id="copyBtn">📋 COPY KEY</button>
    </div>
    <div style="color:#6fcf97;font-size:12px;text-align:center;margin-top:10px" id="expiryDisplay">Còn hạn: --</div>
    <div class="log" id="logArea"><div class="log-entry">[INIT] Hệ thống sẵn sàng</div></div>
    <div class="footer">Minh Hoang · FROX PROXY v2.0</div>
</div>
<script>
    (function() {
        let currentKey = null, expiresAt = null, countdownTimer = null, isVerified = false;
        const createdEl = document.getElementById('created');
        const usedEl = document.getElementById('used');
        const remainingEl = document.getElementById('remaining');
        const logEl = document.getElementById('logArea');
        const dateDisplay = document.getElementById('dateDisplay');
        const expiryEl = document.getElementById('expiryDisplay');
        const verifyBadge = document.getElementById('verifyBadge');
        const getKeyBtn = document.getElementById('getKeyBtn');

        dateDisplay.textContent = new Date().toLocaleDateString('vi-VN');

        function updateStats(data) {
            createdEl.textContent = data.created;
            usedEl.textContent = data.used;
            remainingEl.textContent = data.remaining;
        }
        function renderLogs(logs) {
            logEl.innerHTML = '';
            logs.forEach(msg => {
                const entry = document.createElement('div');
                entry.className = 'log-entry';
                entry.textContent = msg;
                logEl.appendChild(entry);
            });
            logEl.scrollTop = logEl.scrollHeight;
        }
        function setVerifiedUI(verified) {
            isVerified = verified;
            if (verified) {
                verifyBadge.className = 'verify-badge ok';
                verifyBadge.textContent = '✅ Đã vượt link - có thể lấy key thoải mái trong 24h';
                getKeyBtn.textContent = '▶ LẤY KEY NGAY';
            } else {
                verifyBadge.className = 'verify-badge no';
                verifyBadge.textContent = '🔒 Bạn cần vượt link 1 lần để lấy key (hiệu lực 24h)';
                getKeyBtn.textContent = '🔗 VƯỢT LINK ĐỂ LẤY KEY';
            }
        }
        function startCountdown() {
            if (countdownTimer) clearInterval(countdownTimer);
            countdownTimer = setInterval(() => {
                if (!expiresAt) return;
                const diff = expiresAt - Date.now();
                if (diff <= 0) { expiryEl.textContent = 'Key đã hết hạn'; clearInterval(countdownTimer); return; }
                const h = Math.floor(diff / 3600000), m = Math.floor((diff % 3600000) / 60000), s = Math.floor((diff % 60000) / 1000);
                expiryEl.textContent = `Còn hạn: ${h}h ${m}m ${s}s`;
            }, 1000);
        }
        async function checkVerifyStatus() {
            try {
                const res = await fetch('/api/verify-status');
                const data = await res.json();
                setVerifiedUI(data.verified);
            } catch (err) { console.error(err); }
        }
        async function startVerify() {
            getKeyBtn.disabled = true;
            getKeyBtn.textContent = 'Đang tạo link...';
            try {
                const res = await fetch('/api/start-verify', { method: 'POST' });
                const data = await res.json();
                if (!res.ok || !data.url) {
                    getKeyBtn.disabled = false;
                    setVerifiedUI(false);
                    verifyBadge.textContent = '⚠ Không thể tạo link, thử lại sau';
                    return;
                }
                window.location.href = data.url;
            } catch (err) { console.error(err); getKeyBtn.disabled = false; }
        }
        async function getKey() {
            try {
                const res = await fetch('/api/get-key', { method: 'POST' });
                const data = await res.json();
                if (res.status === 403 && data.error === 'need_verify') { setVerifiedUI(false); return; }
                if (!res.ok) {
                    expiryEl.textContent = 'Hết key khả dụng, thử lại sau';
                    updateStats(data); renderLogs(data.logs);
                    return;
                }
                currentKey = data.key;
                expiresAt = new Date(data.expires_at).getTime();
                updateStats(data); renderLogs(data.logs); startCountdown();
            } catch (err) { console.error(err); }
        }
        function copyKey() {
            if (!currentKey) return;
            navigator.clipboard.writeText(currentKey).catch(() => {
                const ta = document.createElement('textarea');
                ta.value = currentKey;
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
            });
        }
        getKeyBtn.addEventListener('click', () => { isVerified ? getKey() : startVerify(); });
        document.getElementById('copyBtn').addEventListener('click', copyKey);
        fetch('/api/stats').then(r => r.json()).then(updateStats);
        checkVerifyStatus();
    })();
</script>
</body>
</html>
"""

KEY_RESULT_HTML = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FROX PROXY - Key của bạn</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box;font-family:'Courier New',monospace}
        body{background:#0a0e14;display:flex;justify-content:center;align-items:center;min-height:100vh;padding:15px}
        .container{background:#121a24;border:1px solid #2a3a4a;border-radius:14px;padding:30px 28px;max-width:520px;width:100%;box-shadow:0 10px 40px rgba(0,0,0,0.9)}
        .title{color:#7ab8f0;font-size:20px;font-weight:bold;text-align:center;letter-spacing:3px;border-bottom:1px solid #1a2a3a;padding-bottom:15px;margin-bottom:18px}
        .title small{color:#4a6a7a;font-size:11px;display:block;font-weight:normal;letter-spacing:1px}
        .success-badge{text-align:center;font-size:12px;padding:8px;border-radius:6px;margin-bottom:15px;background:#123a2a;color:#6fcf97;border:1px solid #2a5a3a}
        .key-display{background:#0b0e14;border:1px solid #1a3a4a;border-radius:10px;padding:20px;margin:15px 0;text-align:center}
        .key-display .label{color:#5a8a9a;font-size:11px;text-transform:uppercase;letter-spacing:1px}
        .key-display .key-value{color:#8fdfb0;font-size:22px;font-weight:bold;word-break:break-all;margin:8px 0}
        .key-display .sub{color:#4a6a7a;font-size:12px;margin-top:6px}
        .stats{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin:15px 0}
        .stat-box{background:#0b0e14;border:1px solid #1a2a3a;border-radius:6px;padding:8px 6px;text-align:center}
        .stat-box .label{color:#4a6a7a;font-size:9px;text-transform:uppercase;letter-spacing:0.5px}
        .stat-box .value{color:#b8d8e8;font-size:16px;font-weight:bold}
        .stat-box .value.green{color:#6fcf97}
        .btn-group{display:flex;gap:10px;margin-top:15px}
        .btn{flex:1;padding:12px 10px;border-radius:6px;font-size:14px;font-weight:bold;cursor:pointer;transition:0.2s;text-align:center;border:1px solid #2a4a5a;background:#1a2a3a;color:#c0d8e8;text-decoration:none;display:block}
        .btn:hover{background:#2a3a4a;border-color:#4a7a8a;color:#fff}
        .btn-copy{background:#1a4a3a;border-color:#2a7a5a;color:#8fdfb0}
        .btn-copy:hover{background:#2a5a4a;border-color:#4a9a7a}
        .footer{color:#3a5a6a;font-size:10px;text-align:center;border-top:1px solid #1a2a3a;padding-top:14px;margin-top:18px}
    </style>
</head>
<body>
<div class="container">
    <div class="title">⚡ FROX PROXY<small>Vượt link thành công</small></div>
    <div class="success-badge">✅ Bạn đã vượt link thành công! Key của bạn đã sẵn sàng.</div>
    <div class="key-display">
        <div class="label">KEY CỦA BẠN</div>
        <div class="key-value" id="keyValue">{{ key }}</div>
        <div class="sub" id="expiryDisplay">Còn hạn: --</div>
    </div>
    <div class="stats">
        <div class="stat-box"><div class="label">Đã tạo</div><div class="value">{{ created }}</div></div>
        <div class="stat-box"><div class="label">Đã dùng</div><div class="value">{{ used }}</div></div>
        <div class="stat-box"><div class="label">Còn lại</div><div class="value green">{{ remaining }}</div></div>
    </div>
    <div class="btn-group">
        <button class="btn btn-copy" id="copyBtn">📋 COPY KEY</button>
        <a class="btn" href="/">🏠 VỀ TRANG CHỦ</a>
    </div>
    <div class="footer">Minh Hoang · FROX PROXY v2.0 · Key có hiệu lực trong 24h</div>
</div>
<script>
    const key = {{ key | tojson }};
    const expiresAt = new Date({{ expires_at | tojson }}).getTime();
    const expiryEl = document.getElementById('expiryDisplay');
    setInterval(() => {
        const diff = expiresAt - Date.now();
        if (diff <= 0) { expiryEl.textContent = 'Key đã hết hạn'; return; }
        const h = Math.floor(diff / 3600000), m = Math.floor((diff % 3600000) / 60000), s = Math.floor((diff % 60000) / 1000);
        expiryEl.textContent = `Còn hạn: ${h}h ${m}m ${s}s`;
    }, 1000);
    document.getElementById('copyBtn').addEventListener('click', () => {
        navigator.clipboard.writeText(key).then(() => {
            const btn = document.getElementById('copyBtn');
            const old = btn.textContent;
            btn.textContent = '✅ Đã copy!';
            setTimeout(() => { btn.textContent = old; }, 1500);
        }).catch(() => {
            const ta = document.createElement('textarea');
            ta.value = key;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
        });
    });
</script>
</body>
</html>
"""

VERIFY_FAILED_HTML = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FROX PROXY - Lỗi</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box;font-family:'Courier New',monospace}
        body{background:#0a0e14;display:flex;justify-content:center;align-items:center;min-height:100vh;padding:15px}
        .container{background:#121a24;border:1px solid #2a3a4a;border-radius:14px;padding:30px 28px;max-width:520px;width:100%;box-shadow:0 10px 40px rgba(0,0,0,0.9);text-align:center}
        .title{color:#7ab8f0;font-size:20px;font-weight:bold;letter-spacing:3px;border-bottom:1px solid #1a2a3a;padding-bottom:15px;margin-bottom:18px}
        .error-badge{padding:14px;border-radius:8px;margin-bottom:18px;background:#3a1a1a;color:#f08080;border:1px solid #5a2a2a;font-size:14px}
        .btn{display:inline-block;margin-top:10px;padding:12px 24px;border-radius:6px;font-size:14px;font-weight:bold;border:1px solid #2a4a5a;background:#1a2a3a;color:#c0d8e8;text-decoration:none}
        .btn:hover{background:#2a3a4a;border-color:#4a7a8a;color:#fff}
    </style>
</head>
<body>
<div class="container">
    <div class="title">⚡ FROX PROXY</div>
    {% if no_keys %}
    <div class="error-badge">⚠ Bạn đã vượt link thành công, nhưng hiện đang hết key khả dụng.<br>Vui lòng quay lại trang chủ và thử lấy key sau ít phút.</div>
    {% else %}
    <div class="error-badge">❌ Liên kết vượt link không hợp lệ hoặc đã hết hạn (quá 15 phút).<br>Vui lòng quay lại trang chủ và vượt link lại.</div>
    {% endif %}
    <a class="btn" href="/">🏠 VỀ TRANG CHỦ</a>
</div>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/api/verify-status", methods=["GET"])
def verify_status():
    verified = _is_verified()
    expires_at = None
    if verified:
        verified_at = datetime.fromisoformat(session["verified_at"])
        expires_at = (verified_at + VERIFY_LIFETIME).isoformat()
    return jsonify({"verified": verified, "expires_at": expires_at})


@app.route("/api/start-verify", methods=["POST"])
def start_verify():
    """Tạo link4m trỏ về /verify để người dùng vượt link."""
    token = _verify_serializer.dumps({"purpose": "verify"})
    return_url = request.url_root.rstrip("/") + f"/verify?token={token}"

    try:
        resp = requests.get(
            LINK4M_API_URL,
            params={"api": LINK4M_API_TOKEN, "url": return_url},
            timeout=10,
        )
        data = resp.json()
    except Exception as e:
        return jsonify({"error": "link4m_request_failed", "detail": str(e)}), 502

    if data.get("status") != "success":
        return jsonify({"error": "link4m_error", "detail": data}), 502

    with _lock:
        _add_log("🔗 Tạo link vượt link4m")

    return jsonify({"url": data["shortenedUrl"]})


@app.route("/verify", methods=["GET"])
def verify():
    """Link4m redirect người dùng về đây sau khi vượt link xong."""
    token = request.args.get("token", "")
    try:
        _verify_serializer.loads(token, max_age=PENDING_TOKEN_MAX_AGE)
        valid = True
    except (BadSignature, SignatureExpired):
        valid = False

    if not valid:
        return render_template_string(VERIFY_FAILED_HTML), 400

    with _lock:
        _add_log("✅ Người dùng đã vượt link4m thành công")

    session["verified_at"] = datetime.now().isoformat()

    with _lock:
        result = _issue_key_locked()

    if result is None:
        return render_template_string(VERIFY_FAILED_HTML, no_keys=True), 200

    return render_template_string(KEY_RESULT_HTML, **result)


@app.route("/api/get-key", methods=["POST"])
def get_key():
    if not _is_verified():
        return jsonify({"error": "need_verify"}), 403

    with _lock:
        result = _issue_key_locked()

    if result is None:
        return jsonify({
            "error": "no_keys_available",
            "created": _state["created_count"],
            "used": _state["used_count"],
            "remaining": 0,
            "logs": _state["logs"][-10:],
        }), 409

    return jsonify(result)


@app.route("/api/check-key/<key>", methods=["GET"])
def check_key(key):
    with _lock:
        _sweep_expired()
        if key in _state["issued"]:
            issued_at = _state["issued"][key]
            expires_at = issued_at + KEY_LIFETIME
            return jsonify({
                "valid": True,
                "issued_at": issued_at.isoformat(),
                "expires_at": expires_at.isoformat(),
            })
        return jsonify({"valid": False})


@app.route("/api/stats", methods=["GET"])
def stats():
    with _lock:
        _sweep_expired()
        return jsonify({
            "created": _state["created_count"],
            "used": _state["used_count"],
            "remaining": len(_state["available"]),
            "active_keys": len(_state["issued"]),
            "logs": _state["logs"][-10:],
        })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
