"""Stateless auth with only the standard library: PBKDF2 password hashes and
HS256 JWTs. No dependencies means no build step and a smaller cold start."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

import config
from store import get_store

PBKDF2_ITERATIONS = 60_000
ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}

_secret = None


def hash_password(password, iterations=PBKDF2_ITERATIONS):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        _, iterations, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signing_secret():
    global _secret
    if _secret is None:
        item = get_store().get("CONFIG", "SECRET")
        _secret = (item or {}).get("value") or config.JWT_FALLBACK_SECRET
    return _secret.encode()


def new_secret():
    return secrets.token_urlsafe(48)


def create_token(username, role):
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({"sub": username, "role": role,
                               "exp": int(time.time()) + config.TOKEN_TTL_S}).encode())
    sig = _b64(hmac.new(_signing_secret(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def decode_token(token):
    """Returns the claims dict, or None if the token is invalid or expired."""
    try:
        header, payload, sig = token.split(".")
        expected = _b64(hmac.new(_signing_secret(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        claims = json.loads(_unb64(payload))
        if claims.get("exp", 0) < time.time():
            return None
        return claims
    except (ValueError, AttributeError, json.JSONDecodeError):
        return None


def has_role(claims, required):
    return ROLE_RANK.get((claims or {}).get("role"), 0) >= ROLE_RANK.get(required, 99)
