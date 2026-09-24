"""Conservative URL handling: preserve query order, signed values and route fragments."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote, urlsplit, urlunsplit

from video_scout.domain.models import ScanConfig, ScoutError


def validate_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError
        _ = parts.port
        if any(ord(char) < 32 or ord(char) == 127 for char in url):
            raise ValueError
    except ValueError as exc:
        raise ScoutError("仅允许无内嵌账号密码的 HTTP/HTTPS 地址") from exc
    return url


def normalize_url(url: str) -> str:
    validate_url(url)
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, parts.fragment))


def in_scope(url: str, config: ScanConfig) -> bool:
    try:
        validate_url(url)
        parsed = urlsplit(url)
        start = urlsplit(config.start_url)
        allowed = {start.hostname, *(host.lower().strip() for host in config.allowed_hosts)}
        if parsed.hostname not in allowed:
            return False
        # Clients and servers can collapse dot segments after our check. Compare
        # restrictions conservatively without ever rewriting the request/signature.
        effective_path = parsed.path
        if config.allowed_paths:
            for _ in range(4):
                decoded = unquote(effective_path)
                if decoded == effective_path:
                    break
                effective_path = decoded
            if any(segment in {".", ".."} for segment in effective_path.replace("\\", "/").split("/")):
                return False
        return not config.allowed_paths or any(
            effective_path == prefix.rstrip("/") or effective_path.startswith(prefix.rstrip("/") + "/")
            for prefix in config.allowed_paths
        )
    except ScoutError:
        return False


def safe_text(value: object, limit: int = 4000) -> str:
    # Strip terminal controls, OSC/CSI, bidi overrides and other formatting controls.
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", str(value))
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(char for char in text if not unicodedata.category(char).startswith("C") or char == "\n")[:limit]


def redact(value: object) -> str:
    # Logs never retain URL query or fragment values, regardless of parameter name.
    text = safe_text(value)
    def clean(match: re.Match) -> str:
        try:
            p = urlsplit(match[0])
            return urlunsplit((p.scheme, p.hostname or "", p.path, "<redacted>" if p.query else "", ""))
        except ValueError:
            return "<url>"
    text = re.sub(r"https?://[^\s<>\"']+", clean, text)
    return re.sub(r"(?i)(authorization|cookie|set-cookie)[\"']?\s*[:=][^\n]*", r"\1: <redacted>", text)
