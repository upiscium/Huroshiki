"""Small, conservative redaction helpers for human-readable diagnostics."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "api_key", "api-key", "apikey", "access_token", "access-token",
        "authorization", "cookie", "credential", "password", "secret", "token",
    }
)
_URL_RE = re.compile(r"(?i)https?://[^\s]+")
_SENSITIVE_QUERY_VALUE_RE = re.compile(
    r"(?i)((?<![a-z0-9%])(?:api(?:[-_]|%5f|%2d)?key|"
    r"access(?:[-_]|%5f|%2d)?token|authorization|cookie|credential|"
    r"password|secret|token)=)[^\s&#;]*"
)


def redact_url(value: str) -> str:
    """Return a safe display representation without mutating the input identity."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
            return "<redacted-url>"
        # parse_qsl is intentional: encoded key spellings are decoded before matching.
        # Treat legacy semicolon separators conservatively. If a semicolon was
        # data rather than a separator, strict parsing fails closed below.
        query_for_parsing = parsed.query.replace(";", "&")
        pairs = parse_qsl(query_for_parsing, keep_blank_values=True, strict_parsing=True)
        query = urlencode(
            [(key, "<redacted>" if key.casefold() in _SENSITIVE_QUERY_KEYS else item)
             for key, item in pairs],
            doseq=True,
        )
        hostname = parsed.hostname
        if hostname is None:
            return "<redacted-url>"
        host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        # Omit userinfo completely; this is clearer and matches URL identity display.
        netloc = host
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))
    except (TypeError, ValueError):
        # Diagnostics must fail closed rather than risk echoing malformed credentials.
        return "<redacted-url>"


def redact_embedded_text(value: str) -> str:
    """Redact every HTTP(S) URL embedded in arbitrary diagnostic text."""
    return _URL_RE.sub(lambda match: redact_url(match.group(0)), value)


def redact_diagnostic_text(value: str) -> str:
    """Redact embedded URLs and standalone sensitive query-value fragments."""
    redacted = redact_embedded_text(value)
    return _SENSITIVE_QUERY_VALUE_RE.sub(r"\1<redacted>", redacted)
