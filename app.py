import json
import os
import random
import threading
import uuid
from pathlib import Path
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, render_template, request, redirect, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())

# ---- Cấu hình ----
KEY_LIFETIME = timedelta(days=1)          # Thời hạn 1 key kể từ lúc cấp
VERIFY_LIFETIME = timedelta(days=1)       # Vượt link4m 1 lần có hiệu lực trong 24h
PENDING_TOKEN_TTL = timedelta(minutes=15)  # Token chờ vượt link hết hạn sau 15 phút

LINK4M_API_TOKEN = os.environ.get("LINK4M_API_TOKEN", "69902dcc482df052bb6c2347")
LINK4M_API_URL = "https://link4m.co/api-shorten/v2"

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
    "pending_tokens": {},  # verify_token -> datetime tạo (chờ người dùng vượt link)
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


def _sweep_pending_tokens():
    now = datetime.now()
    expired = [t for t, created_at in _state["pending_tokens"].items()
               if now - created_at >= PENDING_TOKEN_TTL]
    for t in expired:
        del _state["pending_tokens"][t]


def _is_verified() -> bool:
    verified_at = session.get("verified_at")
    if not verified_at:
        return False
    verified_at = datetime.fromisoformat(verified_at)
    return datetime.now() - verified_at < VERIFY_LIFETIME


@app.route("/")
def index():
    return render_template("index.html")


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
    with _lock:
        _sweep_pending_tokens()
        token = uuid.uuid4().hex
        _state["pending_tokens"][token] = datetime.now()

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
    with _lock:
        _sweep_pending_tokens()
        valid = token in _state["pending_tokens"]
        if valid:
            del _state["pending_tokens"][token]
            _add_log("✅ Người dùng đã vượt link4m thành công")

    if valid:
        session["verified_at"] = datetime.now().isoformat()
        return redirect("/?verified=1")
    return redirect("/?verified=0")


@app.route("/api/get-key", methods=["POST"])
def get_key():
    if not _is_verified():
        return jsonify({"error": "need_verify"}), 403

    with _lock:
        _sweep_expired()

        if not _state["available"]:
            _add_log("⚠ Hết key khả dụng, vui lòng chờ key hết hạn")
            return jsonify({
                "error": "no_keys_available",
                "created": _state["created_count"],
                "used": _state["used_count"],
                "remaining": 0,
                "logs": _state["logs"][-10:],
            }), 409

        idx = random.randrange(len(_state["available"]))
        key = _state["available"].pop(idx)
        issued_at = datetime.now()
        expires_at = issued_at + KEY_LIFETIME
        _state["issued"][key] = issued_at
        _state["used_count"] += 1
        _add_log(f"📌 Lấy key: {key} (hết hạn: {expires_at.strftime('%H:%M:%S %d/%m')})")

        return jsonify({
            "key": key,
            "issued_at": issued_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "created": _state["created_count"],
            "used": _state["used_count"],
            "remaining": len(_state["available"]),
            "logs": _state["logs"][-10:],
        })


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
