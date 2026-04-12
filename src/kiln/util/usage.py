"""Subscription usage fetching for Anthropic and OpenAI.

Reads OAuth tokens from platform-specific stores (macOS Keychain for
Anthropic, ~/.codex/auth.json for OpenAI), refreshes if expired, and
fetches current usage/rate-limit data from each provider's API.

Returns a combined dict suitable for display formatting. All failures
are soft — individual providers degrade to None without raising.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# --- Anthropic constants ---

_KEYCHAIN_SERVICE = "Claude Code-credentials"
_ANTHROPIC_USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
_ANTHROPIC_TOKEN_ENDPOINT = "https://platform.claude.com/v1/oauth/token"
_ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_ANTHROPIC_BETA_HEADER = "oauth-2025-04-20"

# --- OpenAI/Codex constants ---

_CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"
_CODEX_USAGE_ENDPOINT = "https://chatgpt.com/backend-api/wham/usage"
_CODEX_TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


# ---------------------------------------------------------------------------
# HTTP helpers (curl-based to avoid Python urllib IPv6 latency)
# ---------------------------------------------------------------------------

def _parse_curl_output(raw: str) -> tuple[int, str]:
    stripped = raw.rstrip()
    last_nl = stripped.rfind("\n")
    if last_nl >= 0:
        maybe_code = stripped[last_nl + 1:]
        if maybe_code.isdigit():
            return (int(maybe_code), stripped[:last_nl])
    return (0, stripped)


def _curl_get(url: str, headers: dict) -> tuple[int, str]:
    cmd = ["curl", "-s", "-w", "\n%{http_code}", "--max-time", "10"]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return _parse_curl_output(result.stdout)


def _curl_post(url: str, data: dict, headers: dict) -> tuple[int, str]:
    cmd = ["curl", "-s", "-w", "\n%{http_code}", "--max-time", "15",
           "-X", "POST", "-d", json.dumps(data)]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    return _parse_curl_output(result.stdout)


# ---------------------------------------------------------------------------
# Anthropic token management
# ---------------------------------------------------------------------------

def _read_keychain() -> dict | None:
    try:
        username = os.environ.get("USER", "claude-code-user")
        result = subprocess.run(
            ["security", "find-generic-password", "-a", username, "-w", "-s", _KEYCHAIN_SERVICE],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass
    return None


def _write_keychain(data: dict) -> bool:
    try:
        username = os.environ.get("USER", "claude-code-user")
        json_str = json.dumps(data)
        hex_value = json_str.encode("utf-8").hex()
        cmd = f'add-generic-password -U -a "{username}" -s "{_KEYCHAIN_SERVICE}" -X "{hex_value}"\n'
        result = subprocess.run(
            ["security", "-i"],
            input=cmd, capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _refresh_anthropic_token(oauth: dict) -> dict | None:
    refresh_tok = oauth.get("refreshToken")
    if not refresh_tok:
        return None
    try:
        code, body = _curl_post(_ANTHROPIC_TOKEN_ENDPOINT, {
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok,
            "client_id": _ANTHROPIC_CLIENT_ID,
            "scope": "user:file_upload user:inference user:mcp_servers user:profile user:sessions:claude_code",
        }, {"Content-Type": "application/json"})

        if code != 200:
            return None

        data = json.loads(body)
        new_access = data.get("access_token")
        if not new_access:
            return None

        return {
            "accessToken": new_access,
            "refreshToken": data.get("refresh_token", refresh_tok),
            "expiresAt": int((time.time() + data.get("expires_in", 3600)) * 1000),
            "scopes": data.get("scope", "").split() if data.get("scope") else oauth.get("scopes", []),
            "subscriptionType": oauth.get("subscriptionType"),
            "rateLimitTier": oauth.get("rateLimitTier"),
        }
    except Exception:
        return None


def _ensure_anthropic_token() -> str | None:
    keychain_data = _read_keychain()
    if not keychain_data:
        return None

    oauth = keychain_data.get("claudeAiOauth")
    if not oauth or not oauth.get("accessToken"):
        return None

    expires_at = oauth.get("expiresAt", 0)
    if (time.time() * 1000) >= (expires_at - 60_000):
        new_oauth = _refresh_anthropic_token(oauth)
        if new_oauth:
            keychain_data["claudeAiOauth"] = new_oauth
            _write_keychain(keychain_data)
            oauth = new_oauth
        elif oauth.get("accessToken"):
            pass  # Try existing token — server might disagree on expiry
        else:
            return None

    return oauth["accessToken"]


# ---------------------------------------------------------------------------
# OpenAI/Codex token management
# ---------------------------------------------------------------------------

def _read_codex_auth() -> dict | None:
    try:
        if _CODEX_AUTH_FILE.exists():
            return json.loads(_CODEX_AUTH_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _write_codex_auth(data: dict) -> bool:
    try:
        tmp = _CODEX_AUTH_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(_CODEX_AUTH_FILE)
        return True
    except OSError:
        return False


def _is_codex_token_expired(auth_data: dict) -> bool:
    tokens = auth_data.get("tokens", {})
    access_token = tokens.get("access_token", "")
    if not access_token:
        return True
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return True
        payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        return time.time() >= (decoded.get("exp", 0) - 300)
    except Exception:
        return True


def _refresh_codex_token(auth_data: dict) -> dict | None:
    tokens = auth_data.get("tokens", {})
    refresh_tok = tokens.get("refresh_token")
    if not refresh_tok:
        return None
    try:
        code, body = _curl_post(_CODEX_TOKEN_ENDPOINT, {
            "grant_type": "refresh_token",
            "refresh_token": refresh_tok,
            "client_id": _CODEX_CLIENT_ID,
        }, {"Content-Type": "application/json"})

        if code != 200:
            return None

        data = json.loads(body)
        new_access = data.get("access_token")
        if not new_access:
            return None

        tokens["access_token"] = new_access
        if "refresh_token" in data:
            tokens["refresh_token"] = data["refresh_token"]
        if "id_token" in data:
            tokens["id_token"] = data["id_token"]
        auth_data["tokens"] = tokens
        auth_data["last_refresh"] = datetime.now(timezone.utc).isoformat()
        return auth_data
    except Exception:
        return None


def _ensure_codex_token() -> tuple[str, str] | None:
    auth_data = _read_codex_auth()
    if not auth_data:
        return None

    tokens = auth_data.get("tokens", {})
    access_token = tokens.get("access_token")
    account_id = tokens.get("account_id", "")
    if not access_token:
        return None

    if _is_codex_token_expired(auth_data):
        refreshed = _refresh_codex_token(auth_data)
        if refreshed:
            _write_codex_auth(refreshed)
            tokens = refreshed.get("tokens", {})
            access_token = tokens.get("access_token", access_token)

    return (access_token, account_id)


# ---------------------------------------------------------------------------
# API fetch
# ---------------------------------------------------------------------------

def _fetch_anthropic_usage(token: str) -> dict | None:
    try:
        code, body = _curl_get(_ANTHROPIC_USAGE_ENDPOINT, {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": _ANTHROPIC_BETA_HEADER,
            "Content-Type": "application/json",
            "User-Agent": "kiln-usage/1.0",
        })
        if code != 200:
            return None
        return json.loads(body)
    except Exception:
        return None


def _fetch_codex_usage(token: str, account_id: str) -> dict | None:
    try:
        code, body = _curl_get(_CODEX_USAGE_ENDPOINT, {
            "Authorization": f"Bearer {token}",
            "ChatGPT-Account-Id": account_id,
            "User-Agent": "codex-cli",
            "Content-Type": "application/json",
        })
        if code != 200:
            return None
        return json.loads(body)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_subscription_usage() -> dict[str, Any]:
    """Fetch subscription usage from Anthropic and OpenAI.

    Returns a dict with optional "anthropic" and "openai" keys.
    Each provider degrades independently — a failure in one doesn't
    affect the other. Returns an empty dict if both fail.

    This is synchronous (makes HTTP calls). For async contexts,
    use ``get_subscription_usage_async()``.
    """
    result: dict[str, Any] = {}

    # Anthropic
    try:
        token = _ensure_anthropic_token()
        if token:
            data = _fetch_anthropic_usage(token)
            if data:
                result["anthropic"] = data
    except Exception:
        log.debug("Anthropic usage fetch failed", exc_info=True)

    # OpenAI/Codex
    try:
        codex = _ensure_codex_token()
        if codex:
            token, account_id = codex
            data = _fetch_codex_usage(token, account_id)
            if data:
                result["openai"] = data
    except Exception:
        log.debug("OpenAI usage fetch failed", exc_info=True)

    return result


async def get_subscription_usage_async() -> dict[str, Any]:
    """Async wrapper — runs the synchronous fetch in a thread."""
    return await asyncio.to_thread(get_subscription_usage)
