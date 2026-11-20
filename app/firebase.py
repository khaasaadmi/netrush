"""Firebase Phone Auth for SMS login codes.

Firebase sends the SMS and checks the code in the customer's browser. The browser then gives us a
Firebase ID token, and we verify it here: an RS256 signature against Google's published keys, plus
the project, issuer and expiry claims. Only then do we trust the phone number inside it.

Firebase only sends its own verification codes. It can't deliver our messages (booking
confirmations, door codes), which go through the WhatsApp / SMS providers in notify.py.
"""
import base64
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.request

from . import config

JWKS_URL = "https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com"
MAX_AUTH_AGE = 30 * 60  # the code must have been entered in the last 30 minutes
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")

_keys = {"exp": 0.0, "keys": {}}
_keys_lock = threading.Lock()


class FirebaseError(Exception):
    pass


def enabled():
    return bool(config.FIREBASE_PROJECT_ID and config.FIREBASE_API_KEY)


def client_config():
    """What the browser needs to start Firebase (all public values)."""
    if not enabled():
        return None
    return {"apiKey": config.FIREBASE_API_KEY, "authDomain": config.FIREBASE_AUTH_DOMAIN,
            "projectId": config.FIREBASE_PROJECT_ID, "appId": config.FIREBASE_APP_ID or None}


def _b64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _int(s):
    return int.from_bytes(_b64(s), "big")


def _fetch_keys():
    with urllib.request.urlopen(JWKS_URL, timeout=10) as r:
        data = json.loads(r.read())
        m = re.search(r"max-age=(\d+)", r.headers.get("Cache-Control", ""))
    keys = {k["kid"]: (_int(k["n"]), _int(k["e"])) for k in data.get("keys", []) if k.get("kty") == "RSA"}
    return keys, int(m.group(1)) if m else 3600


def _google_key(kid):
    with _keys_lock:
        if kid not in _keys["keys"] or time.time() > _keys["exp"]:
            try:
                keys, ttl = _fetch_keys()
            except Exception as e:  # noqa: BLE001
                raise FirebaseError(f"Couldn't reach Google to check the login: {e}")
            _keys.update(keys=keys, exp=time.time() + ttl)
        return _keys["keys"].get(kid)


def rsa_sha256_verify(n, e, message, signature):
    """RSASSA-PKCS1-v1_5 with SHA-256 (what RS256 means)."""
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    digest = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    expected = b"\x00\x01" + b"\xff" * (k - 3 - len(digest)) + b"\x00" + digest
    return hmac.compare_digest(em, expected)


def verify_id_token(token, now=None, key_lookup=None):
    """Returns the verified phone number (+91…) from a Firebase ID token, or raises FirebaseError."""
    parts = (token or "").split(".")
    if len(parts) != 3:
        raise FirebaseError("Login token is malformed")
    try:
        header, claims, sig = json.loads(_b64(parts[0])), json.loads(_b64(parts[1])), _b64(parts[2])
    except (ValueError, json.JSONDecodeError):
        raise FirebaseError("Login token is malformed")
    if header.get("alg") != "RS256":
        raise FirebaseError("Login token uses the wrong algorithm")
    key = (key_lookup or _google_key)(header.get("kid"))
    if not key or not rsa_sha256_verify(key[0], key[1], f"{parts[0]}.{parts[1]}".encode(), sig):
        raise FirebaseError("Login token signature is not valid")
    now = time.time() if now is None else now
    pid = config.FIREBASE_PROJECT_ID
    if claims.get("aud") != pid or claims.get("iss") != f"https://securetoken.google.com/{pid}":
        raise FirebaseError("Login token is for a different Firebase project")
    if not claims.get("sub"):
        raise FirebaseError("Login token has no user")
    if claims.get("exp", 0) < now - 30 or claims.get("iat", now) > now + 300:
        raise FirebaseError("Login token has expired. Please verify again.")
    if now - claims.get("auth_time", 0) > MAX_AUTH_AGE:
        raise FirebaseError("That verification is too old. Please verify again.")
    if (claims.get("firebase") or {}).get("sign_in_provider") != "phone" or not claims.get("phone_number"):
        raise FirebaseError("Login token is not a phone verification")
    return claims["phone_number"]
