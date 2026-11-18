"""Identifiers and hashes shared by the API, the CLI and the pipeline."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets

from .settings import settings

_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"       # no 0/o/1/l, easy to read aloud


def new_id(prefix: str) -> str:
    return prefix + "".join(secrets.choice(_ALPHABET) for _ in range(12))


def new_api_key(role: str) -> str:
    """'rq_', 'rv_' or 'ad_' + 40 random characters; only its HMAC is stored."""
    return {"requester": "rq_", "reviewer": "rv_", "admin": "ad_"}[role] + secrets.token_urlsafe(30)


def key_hash(api_key: str) -> bytes:
    return hmac.new(settings.key_pepper, api_key.encode(), hashlib.sha256).digest()


def canonical_hash(obj) -> bytes:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).digest()


def url_hash(url: str) -> bytes:
    """sha256 of the normalised URL: lower-case scheme and host, no fragment, no tracking parameters."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    p = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "fbclid", "gclid"))]
    norm = urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", urlencode(sorted(q)), ""))
    return hashlib.sha256(norm.encode()).digest()
