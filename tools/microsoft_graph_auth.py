"""Microsoft Graph authentication helpers — app-only and delegated (device code) flows."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine, Union

import httpx


DEFAULT_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
DEFAULT_GRAPH_AUTHORITY_URL = "https://login.microsoftonline.com"
DEFAULT_TOKEN_SKEW_SECONDS = 120

DEFAULT_DELEGATED_SCOPES = [
    "https://graph.microsoft.com/Calendars.ReadWrite",
    "https://graph.microsoft.com/Mail.ReadWrite",
    "offline_access",
]
DEFAULT_TOKEN_CACHE_PATH = "~/.hermes/.graph_token"


class MicrosoftGraphAuthError(RuntimeError):
    """Base class for Microsoft Graph auth failures."""


class MicrosoftGraphConfigError(MicrosoftGraphAuthError):
    """Raised when Graph credentials are missing or invalid."""


class MicrosoftGraphTokenError(MicrosoftGraphAuthError):
    """Raised when token acquisition fails."""


@dataclass(frozen=True)
class GraphCredentials:
    """Normalized Microsoft Graph app-only credentials."""

    tenant_id: str
    client_id: str
    client_secret: str
    scope: str = DEFAULT_GRAPH_SCOPE
    authority_url: str = DEFAULT_GRAPH_AUTHORITY_URL

    @property
    def token_url(self) -> str:
        base = self.authority_url.rstrip("/")
        tenant = self.tenant_id.strip().strip("/")
        return f"{base}/{tenant}/oauth2/v2.0/token"

    @classmethod
    def from_env(
        cls,
        environ: dict[str, str] | None = None,
        *,
        required: bool = True,
    ) -> "GraphCredentials | None":
        env = environ if environ is not None else os.environ
        tenant_id = (env.get("MSGRAPH_TENANT_ID") or "").strip()
        client_id = (env.get("MSGRAPH_CLIENT_ID") or "").strip()
        client_secret = (env.get("MSGRAPH_CLIENT_SECRET") or "").strip()
        scope = (env.get("MSGRAPH_SCOPE") or DEFAULT_GRAPH_SCOPE).strip()
        authority_url = (
            env.get("MSGRAPH_AUTHORITY_URL") or DEFAULT_GRAPH_AUTHORITY_URL
        ).strip()

        missing = [
            name
            for name, value in (
                ("MSGRAPH_TENANT_ID", tenant_id),
                ("MSGRAPH_CLIENT_ID", client_id),
                ("MSGRAPH_CLIENT_SECRET", client_secret),
            )
            if not value
        ]
        if missing:
            if not required:
                return None
            raise MicrosoftGraphConfigError(
                f"Missing Microsoft Graph configuration: {', '.join(missing)}"
            )

        return cls(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            scope=scope,
            authority_url=authority_url,
        )


@dataclass(frozen=True)
class DelegatedGraphCredentials:
    """Credentials for delegated (device code) Microsoft Graph auth."""

    tenant_id: str
    client_id: str
    client_secret: str = ""
    scopes: list[str] = field(default_factory=lambda: list(DEFAULT_DELEGATED_SCOPES))
    authority_url: str = DEFAULT_GRAPH_AUTHORITY_URL
    token_cache_path: str = DEFAULT_TOKEN_CACHE_PATH

    @property
    def device_code_url(self) -> str:
        base = self.authority_url.rstrip("/")
        tenant = self.tenant_id.strip().strip("/")
        return f"{base}/{tenant}/oauth2/v2.0/devicecode"

    @property
    def token_url(self) -> str:
        base = self.authority_url.rstrip("/")
        tenant = self.tenant_id.strip().strip("/")
        return f"{base}/{tenant}/oauth2/v2.0/token"

    @classmethod
    def from_env(
        cls,
        environ: dict[str, str] | None = None,
        *,
        required: bool = True,
    ) -> "DelegatedGraphCredentials | None":
        env = environ if environ is not None else os.environ
        tenant_id = (env.get("MSGRAPH_TENANT_ID") or "").strip()
        client_id = (env.get("MSGRAPH_CLIENT_ID") or "").strip()
        client_secret = (env.get("MSGRAPH_CLIENT_SECRET") or "").strip()
        token_cache_path = (
            env.get("MSGRAPH_TOKEN_CACHE") or DEFAULT_TOKEN_CACHE_PATH
        ).strip()
        authority_url = (
            env.get("MSGRAPH_AUTHORITY_URL") or DEFAULT_GRAPH_AUTHORITY_URL
        ).strip()

        # Parse scopes from optional env var (semicolon-delimited)
        raw_scopes = env.get("MSGRAPH_DELEGATED_SCOPES")
        if raw_scopes:
            scopes = [s.strip() for s in raw_scopes.split(";") if s.strip()]
        else:
            scopes = list(DEFAULT_DELEGATED_SCOPES)

        missing = [
            name
            for name, value in (
                ("MSGRAPH_TENANT_ID", tenant_id),
                ("MSGRAPH_CLIENT_ID", client_id),
            )
            if not value
        ]
        if missing:
            if not required:
                return None
            raise MicrosoftGraphConfigError(
                f"Missing delegated Graph configuration: {', '.join(missing)}"
            )

        return cls(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
            authority_url=authority_url,
            token_cache_path=token_cache_path,
        )


@dataclass
class CachedAccessToken:
    """Cached Graph access token."""

    access_token: str
    expires_at: float
    token_type: str = "Bearer"

    def is_expired(self, *, skew_seconds: int = DEFAULT_TOKEN_SKEW_SECONDS) -> bool:
        return self.expires_at <= (time.time() + max(0, int(skew_seconds)))

    @property
    def expires_in_seconds(self) -> int:
        return max(0, int(self.expires_at - time.time()))


class MicrosoftGraphTokenProvider:
    """Acquire and cache Microsoft Graph app-only access tokens."""

    def __init__(
        self,
        credentials: GraphCredentials,
        *,
        timeout: float = 20.0,
        skew_seconds: int = DEFAULT_TOKEN_SKEW_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.credentials = credentials
        self.timeout = timeout
        self.skew_seconds = max(0, int(skew_seconds))
        self._transport = transport
        self._cached_token: CachedAccessToken | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(
        cls,
        environ: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> "MicrosoftGraphTokenProvider":
        credentials = GraphCredentials.from_env(environ)
        return cls(credentials, **kwargs)

    def clear_cache(self) -> None:
        self._cached_token = None

    def inspect_token_health(self) -> dict[str, Any]:
        cached = self._cached_token
        return {
            "configured": True,
            "tenant_id": self.credentials.tenant_id,
            "client_id": self.credentials.client_id,
            "scope": self.credentials.scope,
            "authority_url": self.credentials.authority_url,
            "token_url": self.credentials.token_url,
            "cached": bool(cached),
            "expires_in_seconds": cached.expires_in_seconds if cached else None,
            "is_expired": cached.is_expired(skew_seconds=0) if cached else None,
            "refresh_skew_seconds": self.skew_seconds,
        }

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        cached = self._cached_token
        if not force_refresh and cached and not cached.is_expired(
            skew_seconds=self.skew_seconds
        ):
            return cached.access_token

        async with self._lock:
            cached = self._cached_token
            if not force_refresh and cached and not cached.is_expired(
                skew_seconds=self.skew_seconds
            ):
                return cached.access_token

            token = await self._fetch_access_token()
            self._cached_token = token
            return token.access_token

    async def _fetch_access_token(self) -> CachedAccessToken:
        data = {
            "grant_type": "client_credentials",
            "client_id": self.credentials.client_id,
            "client_secret": self.credentials.client_secret,
            "scope": self.credentials.scope,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            transport=self._transport,
        ) as client:
            response = await client.post(
                self.credentials.token_url,
                data=data,
                headers=headers,
            )

        if response.status_code >= 400:
            detail = _extract_error_detail(response)
            raise MicrosoftGraphTokenError(
                "Microsoft Graph token request failed with HTTP "
                f"{response.status_code}: {detail}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise MicrosoftGraphTokenError(
                "Microsoft Graph token response was not valid JSON."
            ) from exc

        access_token = str(payload.get("access_token") or "").strip()
        token_type = str(payload.get("token_type") or "Bearer").strip() or "Bearer"
        expires_in = payload.get("expires_in")

        if not access_token:
            raise MicrosoftGraphTokenError(
                "Microsoft Graph token response did not include access_token."
            )

        try:
            expires_in_seconds = int(expires_in)
        except (TypeError, ValueError) as exc:
            raise MicrosoftGraphTokenError(
                "Microsoft Graph token response did not include a valid expires_in."
            ) from exc

        return CachedAccessToken(
            access_token=access_token,
            token_type=token_type,
            expires_at=time.time() + max(0, expires_in_seconds),
        )


DeviceCodeCallback = Union[Callable[[str, str], Union[Coroutine[Any, Any, None], None]], Callable[[str, str], None]]
"""Callback signature for displaying device code to the user.

Takes (verification_uri, user_code). Can be async or sync.
"""


class MicrosoftGraphDelegatedTokenProvider:
    """Acquire and cache Microsoft Graph delegated (device code) access tokens.

    Uses OAuth 2.0 Device Authorization Grant (RFC 8628) to obtain tokens
    that act on behalf of a signed-in user. Tokens are persisted to a file
    cache so refresh tokens survive restarts and can be shared by parallel
    cron jobs.

    Usage:

        provider = MicrosoftGraphDelegatedTokenProvider.from_env(
            on_device_code=lambda uri, code: print(f"Go to {uri} and enter {code}")
        )
        token = await provider.get_access_token()
    """

    def __init__(
        self,
        credentials: DelegatedGraphCredentials,
        *,
        timeout: float = 20.0,
        skew_seconds: int = DEFAULT_TOKEN_SKEW_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        on_device_code: DeviceCodeCallback | None = None,
    ) -> None:
        self.credentials = credentials
        self.timeout = timeout
        self.skew_seconds = max(0, int(skew_seconds))
        self._transport = transport
        self._on_device_code = on_device_code
        self._cached_token: CachedAccessToken | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(
        cls,
        environ: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> "MicrosoftGraphDelegatedTokenProvider":
        creds = DelegatedGraphCredentials.from_env(environ)
        if creds is None:
            raise MicrosoftGraphConfigError(
                "MicrosoftGraphDelegatedTokenProvider.from_env() requires "
                "MSGRAPH_TENANT_ID, MSGRAPH_CLIENT_ID environment variables."
            )
        return cls(creds, **kwargs)

    def clear_cache(self) -> None:
        self._cached_token = None

    def inspect_token_health(self) -> dict[str, Any]:
        cached = self._cached_token
        file_path = self._token_cache_path()
        file_exists = file_path.exists()
        file_size = file_path.stat().st_size if file_exists else 0
        return {
            "configured": True,
            "tenant_id": self.credentials.tenant_id,
            "client_id": self.credentials.client_id,
            "scopes": self.credentials.scopes,
            "authority_url": self.credentials.authority_url,
            "token_cache_path": str(file_path),
            "token_cache_exists": file_exists,
            "token_cache_size_bytes": file_size if file_exists else None,
            "cached_in_memory": bool(cached),
            "expires_in_seconds": cached.expires_in_seconds if cached else None,
            "is_expired": cached.is_expired(skew_seconds=0) if cached else None,
            "refresh_skew_seconds": self.skew_seconds,
        }

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        # 1. Check in-memory cache first
        cached = self._cached_token
        if not force_refresh and cached and not cached.is_expired(
            skew_seconds=self.skew_seconds
        ):
            return cached.access_token

        async with self._lock:
            cached = self._cached_token
            if not force_refresh and cached and not cached.is_expired(
                skew_seconds=self.skew_seconds
            ):
                return cached.access_token

            # 2. Try file cache (shared with parallel cron jobs)
            if not force_refresh:
                file_token = self._load_token_from_file()
                if file_token is not None:
                    self._cached_token = file_token
                    if not file_token.is_expired(skew_seconds=self.skew_seconds):
                        return file_token.access_token

            # 3. Fetch a fresh token (refresh if we have one, device code if not)
            token = await self._fetch_or_refresh_token()
            self._cached_token = token
            await self._save_token_to_file(token)
            return token.access_token

    async def _fetch_or_refresh_token(self) -> CachedAccessToken:
        """Try to refresh using a stored refresh token, or fall back to device code flow."""
        # Try loading refresh token from file cache
        refresh_token = self._load_refresh_token_from_file()

        if refresh_token:
            try:
                return await self._refresh_access_token(refresh_token)
            except MicrosoftGraphTokenError:
                # Refresh failed — fall through to device code flow
                pass

        return await self._device_code_flow()

    def _token_cache_path(self) -> Path:
        return Path(self.credentials.token_cache_path).expanduser()

    # --- File-backed persistence (works for parallel cron jobs) ---

    def _load_tokens_from_file_raw(self) -> dict[str, Any] | None:
        """Read token JSON from file with fcntl locking for safety."""
        path = self._token_cache_path()
        if not path.exists():
            return None
        try:
            with open(path, "r") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    data = json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return data
        except (json.JSONDecodeError, IOError, OSError):
            return None

    def _load_token_from_file(self) -> CachedAccessToken | None:
        """Load and validate a cached token from the file cache."""
        data = self._load_tokens_from_file_raw()
        if data is None:
            return None

        access_token = str(data.get("access_token") or "").strip()
        token_type = str(data.get("token_type") or "Bearer").strip() or "Bearer"
        expires_at: float | None = None

        # Try expires_on (epoch float) first
        expires_on = data.get("expires_on")
        if expires_on is not None:
            try:
                expires_at = float(expires_on)
            except (ValueError, TypeError):
                pass

        # Fall back to expires_at (ISO string)
        if expires_at is None:
            expires_at_str = data.get("expires_at")
            if expires_at_str:
                try:
                    from datetime import datetime

                    dt = datetime.fromisoformat(
                        str(expires_at_str).replace("Z", "+00:00")
                    )
                    expires_at = dt.timestamp()
                except (ValueError, TypeError):
                    pass

        if expires_at is None:
            return None

        return CachedAccessToken(
            access_token=access_token,
            token_type=token_type,
            expires_at=expires_at,
        )

    def _load_refresh_token_from_file(self) -> str | None:
        """Extract the refresh token from the file cache."""
        data = self._load_tokens_from_file_raw()
        if data is None:
            return None
        refresh_token = str(data.get("refresh_token") or "").strip()
        return refresh_token or None

    async def _save_token_to_file(self, token: CachedAccessToken) -> None:
        """Persist token + refresh token to the file cache with locking."""
        path = self._token_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        # Preserve the existing refresh token if we don't have a new one
        existing = self._load_tokens_from_file_raw() or {}
        refresh_token = existing.get("refresh_token")

        cache = {
            "access_token": token.access_token,
            "token_type": token.token_type,
            "expires_on": token.expires_at,
            "expires_in": int(max(0, token.expires_at - time.time())),
        }
        if refresh_token:
            cache["refresh_token"] = refresh_token

        try:
            with open(path, "w") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    json.dump(cache, f, indent=2)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            os.chmod(path, 0o600)
        except (IOError, OSError) as exc:
            raise MicrosoftGraphAuthError(
                f"Failed to write token cache to {path}: {exc}"
            ) from exc

    def _save_refresh_token(self, refresh_token: str) -> None:
        """Save or update the refresh token in the file cache."""
        path = self._token_cache_path()
        if path.exists():
            data = self._load_tokens_from_file_raw() or {}
        else:
            data = {}
        data["refresh_token"] = refresh_token

        try:
            with open(path, "w") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    json.dump(data, f, indent=2)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            os.chmod(path, 0o600)
        except (IOError, OSError) as exc:
            raise MicrosoftGraphAuthError(
                f"Failed to write refresh token to {path}: {exc}"
            ) from exc

    # --- Device Code Flow ---

    async def _device_code_flow(self) -> CachedAccessToken:
        """Execute OAuth 2.0 Device Authorization Grant flow."""
        creds = self.credentials
        device_data: dict[str, str] = {
            "client_id": creds.client_id,
            "scope": " ".join(creds.scopes),
        }
        if creds.client_secret:
            device_data["client_secret"] = creds.client_secret

        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            transport=self._transport,
        ) as client:
            # Step 1: Request device code
            response = await client.post(
                creds.device_code_url,
                data=device_data,
                headers=headers,
            )

            if response.status_code >= 400:
                detail = _extract_error_detail(response)
                raise MicrosoftGraphTokenError(
                    f"Device code request failed with HTTP "
                    f"{response.status_code}: {detail}"
                )

            device_info: dict[str, Any] = response.json()

            verification_uri = str(device_info.get("verification_uri", ""))
            user_code = str(device_info.get("user_code", ""))
            device_code = str(device_info.get("device_code", ""))
            poll_interval = int(device_info.get("interval", 5))

            # Step 2: Notify user via callback (handles both sync and async)
            if self._on_device_code:
                cb_result = self._on_device_code(verification_uri, user_code)
                if cb_result is not None:
                    await cb_result

            # Step 3: Poll for completion
            poll_data: dict[str, str] = {
                "client_id": creds.client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "code": device_code,
            }
            if creds.client_secret:
                poll_data["client_secret"] = creds.client_secret

            current_interval = poll_interval
            max_attempts = 600  # 10 minutes at 1s intervals

            for _attempt in range(max_attempts):
                await asyncio.sleep(current_interval)

                poll_resp = await client.post(
                    creds.token_url,
                    data=poll_data,
                    headers=headers,
                )

                if poll_resp.status_code == 200:
                    payload = poll_resp.json()
                    token = self._parse_token_response(payload)

                    # Save refresh token
                    rt = payload.get("refresh_token")
                    if rt:
                        self._save_refresh_token(str(rt).strip())

                    return token

                try:
                    poll_body: dict[str, Any] = poll_resp.json()
                except ValueError:
                    raise MicrosoftGraphTokenError(
                        "Device code poll returned non-JSON response: "
                        f"{poll_resp.status_code}"
                    )

                error = str(poll_body.get("error", "")).strip()

                if error == "authorization_pending":
                    continue
                elif error == "slow_down":
                    current_interval += 5
                    continue
                elif error == "access_denied":
                    raise MicrosoftGraphTokenError(
                        "Authentication was cancelled by the user."
                    )
                elif error == "expired_token":
                    raise MicrosoftGraphTokenError(
                        "Device code expired. Please try again."
                    )
                else:
                    detail = _extract_error_detail(poll_resp)
                    raise MicrosoftGraphTokenError(
                        f"Device code poll failed: {error} - {detail}"
                    )

            raise MicrosoftGraphTokenError(
                "Device code poll timed out after 10 minutes."
            )

    # --- Token Refresh ---

    async def _refresh_access_token(self, refresh_token: str) -> CachedAccessToken:
        """Exchange a refresh token for a new access token."""
        creds = self.credentials
        data: dict[str, str] = {
            "client_id": creds.client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        if creds.client_secret:
            data["client_secret"] = creds.client_secret

        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            transport=self._transport,
        ) as client:
            response = await client.post(
                creds.token_url,
                data=data,
                headers=headers,
            )

            if response.status_code >= 400:
                detail = _extract_error_detail(response)
                raise MicrosoftGraphTokenError(
                    f"Token refresh failed with HTTP "
                    f"{response.status_code}: {detail}"
                )

            payload = response.json()
            cached = self._parse_token_response(payload)

            # Preserve or update refresh token
            rt = payload.get("refresh_token")
            if rt:
                self._save_refresh_token(str(rt).strip())

            return cached

    @staticmethod
    def _parse_token_response(payload: dict[str, Any]) -> CachedAccessToken:
        """Extract a CachedAccessToken from an OAuth token response."""
        access_token = str(payload.get("access_token") or "").strip()
        token_type = str(payload.get("token_type") or "Bearer").strip() or "Bearer"
        raw_expires_in = payload.get("expires_in")

        if not access_token:
            raise MicrosoftGraphTokenError(
                "Token response did not include access_token."
            )

        if raw_expires_in is None:
            expires_in_seconds = 3600
        else:
            try:
                expires_in_seconds = int(raw_expires_in)
            except (TypeError, ValueError) as exc:
                raise MicrosoftGraphTokenError(
                    "Token response did not include a valid expires_in."
                ) from exc

        return CachedAccessToken(
            access_token=access_token,
            token_type=token_type,
            expires_at=time.time() + max(0, expires_in_seconds),
        )


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text or "unknown error"

    if isinstance(payload, dict):
        if isinstance(payload.get("error_description"), str):
            return payload["error_description"]
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            code = error.get("code")
            if message and code:
                return f"{code}: {message}"
            if message:
                return str(message)
            if code:
                return str(code)
        if isinstance(error, str):
            return error
    return str(payload)
