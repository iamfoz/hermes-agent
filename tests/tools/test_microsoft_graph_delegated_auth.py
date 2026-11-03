"""Tests for MicrosoftGraphDelegatedTokenProvider (device code flow)."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import httpx
import pytest

from tools.microsoft_graph_auth import (
    CachedAccessToken,
    DelegatedGraphCredentials,
    MicrosoftGraphConfigError,
    MicrosoftGraphDelegatedTokenProvider,
    MicrosoftGraphTokenError,
)


class TestDelegatedGraphCredentials:
    def test_from_env_raises_for_missing_required_values(self):
        with pytest.raises(MicrosoftGraphConfigError) as exc:
            DelegatedGraphCredentials.from_env({})
        assert "MSGRAPH_TENANT_ID" in str(exc.value)
        assert "MSGRAPH_CLIENT_ID" in str(exc.value)

    def test_from_env_optional_returns_none_when_not_configured(self):
        assert DelegatedGraphCredentials.from_env({}, required=False) is None

    def test_from_env_builds_normalized_credentials(self):
        creds = DelegatedGraphCredentials.from_env(
            {
                "MSGRAPH_TENANT_ID": "tenant-123",
                "MSGRAPH_CLIENT_ID": "client-456",
                "MSGRAPH_CLIENT_SECRET": "secret-789",
            }
        )
        assert creds is not None
        assert creds.tenant_id == "tenant-123"
        assert creds.client_id == "client-456"
        assert creds.client_secret == "secret-789"
        assert creds.scopes == [
            "https://graph.microsoft.com/Calendars.ReadWrite",
            "https://graph.microsoft.com/Mail.ReadWrite",
            "offline_access",
        ]
        assert creds.token_url.endswith("/tenant-123/oauth2/v2.0/token")

    def test_from_env_with_custom_scopes(self):
        creds = DelegatedGraphCredentials.from_env(
            {
                "MSGRAPH_TENANT_ID": "t",
                "MSGRAPH_CLIENT_ID": "c",
                "MSGRAPH_DELEGATED_SCOPES": (
                    "https://graph.microsoft.com/Mail.Read;offline_access"
                ),
            }
        )
        assert creds is not None
        assert len(creds.scopes) == 2
        assert "Mail.Read" in creds.scopes[0]
        assert "offline_access" in creds.scopes

    def test_from_env_with_custom_token_cache(self):
        creds = DelegatedGraphCredentials.from_env(
            {
                "MSGRAPH_TENANT_ID": "t",
                "MSGRAPH_CLIENT_ID": "c",
                "MSGRAPH_TOKEN_CACHE": "/tmp/my-cache.json",
            }
        )
        assert creds is not None
        assert creds.token_cache_path == "/tmp/my-cache.json"


@pytest.mark.anyio
class TestMicrosoftGraphDelegatedTokenProvider:
    async def test_device_code_flow_success(self):
        """Happy path: device code → user authenticates → tokens received."""
        call_count = [0]
        refresh_token = "RT-1"

        def handler(request: httpx.Request) -> httpx.Response:
            call_count[0] += 1
            if call_count[0] == 1:
                # Device code request
                return httpx.Response(200, json={
                    "device_code": "DC-123",
                    "user_code": "ABC-123",
                    "verification_uri": "https://microsoft.com/devicelogin",
                    "interval": 1,
                })
            elif call_count[0] == 2:
                # First poll — pending
                return httpx.Response(400, json={"error": "authorization_pending"})
            else:
                # Second poll — success
                return httpx.Response(200, json={
                    "access_token": "at-1",
                    "refresh_token": refresh_token,
                    "expires_in": 3600,
                    "token_type": "Bearer",
                })

        cb_called = [False]
        async def on_code(uri: str, code: str) -> None:
            cb_called[0] = True
            assert "microsoft.com" in uri
            assert code == "ABC-123"

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            cache_path = f.name

        creds = DelegatedGraphCredentials(
            tenant_id="tenant", client_id="client", client_secret="secret",
            token_cache_path=cache_path,
        )
        provider = MicrosoftGraphDelegatedTokenProvider(
            creds,
            transport=httpx.MockTransport(handler),
            on_device_code=on_code,
        )

        try:
            token = await provider.get_access_token()
            assert token == "at-1"
            assert cb_called[0]
            cached = json.loads(Path(cache_path).read_text())
            assert cached["access_token"] == "at-1"
            assert cached["refresh_token"] == refresh_token
        finally:
            Path(cache_path).unlink(missing_ok=True)

    async def test_access_denied_raises(self):
        """User cancels the device code flow → access_denied error."""
        call_count = [0]
        def handler(request: httpx.Request) -> httpx.Response:
            call_count[0] += 1
            if call_count[0] == 1:
                return httpx.Response(200, json={
                    "device_code": "DC-1",
                    "user_code": "ABC-123",
                    "verification_uri": "https://microsoft.com/devicelogin",
                    "interval": 1,
                })
            return httpx.Response(400, json={"error": "access_denied"})

        creds = DelegatedGraphCredentials(
            tenant_id="tenant", client_id="client", client_secret="secret",
            token_cache_path="/tmp/nonexistent/test_cache.json",
        )
        provider = MicrosoftGraphDelegatedTokenProvider(
            creds,
            transport=httpx.MockTransport(handler),
            on_device_code=lambda uri, code: None,
        )

        with pytest.raises(MicrosoftGraphTokenError) as exc:
            await provider.get_access_token()
        assert "cancelled" in str(exc.value).lower()

    async def test_refresh_token_success(self):
        """Refresh token exchange works."""
        call_count = [0]
        def handler(request: httpx.Request) -> httpx.Response:
            call_count[0] += 1
            return httpx.Response(200, json={
                "access_token": f"refreshed-at-{call_count[0]}",
                "expires_in": 3600,
                "token_type": "Bearer",
            })

        # Create a cache file with a refresh token
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({"refresh_token": "rt-1"}, f)
            cache_path = f.name

        creds = DelegatedGraphCredentials(
            tenant_id="tenant", client_id="client", client_secret="secret",
            token_cache_path=cache_path,
        )
        provider = MicrosoftGraphDelegatedTokenProvider(
            creds,
            transport=httpx.MockTransport(handler),
        )

        try:
            token = await provider.get_access_token()
            assert token == "refreshed-at-1"
            assert call_count[0] == 1

            # Second call should use the in-memory cache
            token2 = await provider.get_access_token()
            assert token2 == "refreshed-at-1"
            assert call_count[0] == 1  # No new HTTP call

        finally:
            Path(cache_path).unlink(missing_ok=True)

    async def test_file_cache_is_used_across_runs(self):
        """Token is loaded from file when in-memory cache is empty."""
        def handler(request: httpx.Request) -> httpx.Response:
            raise RuntimeError("Should not be called — token should come from file")

        # Write a valid token to the cache file
        now = 9_999_999_999
        expires_at = now + 3600  # Valid for an hour
        cached = {
            "access_token": "file-cached-token",
            "token_type": "Bearer",
            "expires_on": expires_at,
            "expires_in": 3600,
        }

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump(cached, f)
            cache_path = f.name

        creds = DelegatedGraphCredentials(
            tenant_id="tenant", client_id="client", client_secret="secret",
            token_cache_path=cache_path,
        )
        provider = MicrosoftGraphDelegatedTokenProvider(
            creds,
            transport=httpx.MockTransport(handler),
            skew_seconds=0,
        )

        try:
            token = await provider.get_access_token()
            assert token == "file-cached-token"
        finally:
            Path(cache_path).unlink(missing_ok=True)

    async def test_file_cache_expired_triggers_refresh(self):
        """Expired file token triggers a refresh if a refresh token exists."""
        refresh_token = "rt-still-valid"

        # Write an expired token with a refresh token
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({
                "access_token": "expired-at",
                "refresh_token": refresh_token,
                "expires_on": 100,  # Very expired
                "expires_in": 0,
            }, f)
            cache_path = f.name

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "access_token": "refreshed-from-expired",
                "expires_in": 3600,
                "token_type": "Bearer",
            })

        creds = DelegatedGraphCredentials(
            tenant_id="tenant", client_id="client", client_secret="secret",
            token_cache_path=cache_path,
        )
        provider = MicrosoftGraphDelegatedTokenProvider(
            creds,
            transport=httpx.MockTransport(handler),
            skew_seconds=0,
        )

        try:
            token = await provider.get_access_token()
            assert token == "refreshed-from-expired"

            # Verify the file was updated
            cached = json.loads(Path(cache_path).read_text())
            assert cached["access_token"] == "refreshed-from-expired"
            assert cached["refresh_token"] == refresh_token  # Preserved
        finally:
            Path(cache_path).unlink(missing_ok=True)

    async def test_concurrent_calls_share_one_auth(self):
        """Concurrent get_access_token calls share one token fetch."""
        call_count = [0]

        async def _fake_fetch() -> CachedAccessToken:
            call_count[0] += 1
            await asyncio.sleep(0)
            return CachedAccessToken(
                access_token="shared-token",
                token_type="Bearer",
                expires_at=9_999_999_999,
            )

        import tempfile
        tmp = tempfile.mktemp(suffix=".json")
        creds = DelegatedGraphCredentials(
            "tenant", "client", "secret",
            token_cache_path=tmp,
        )
        provider = MicrosoftGraphDelegatedTokenProvider(creds)
        provider._fetch_or_refresh_token = _fake_fetch  # type: ignore[method-assign]

        first, second = await asyncio.gather(
            provider.get_access_token(),
            provider.get_access_token(),
        )

        assert first == "shared-token"
        assert second == "shared-token"
        assert call_count[0] == 1

    async def test_from_env_propagates_on_device_code(self):
        """from_env() preserves the on_device_code callback via kwargs."""
        cb_called = [False]
        async def callback(uri: str, code: str) -> None:
            cb_called[0] = True

        provider = MicrosoftGraphDelegatedTokenProvider.from_env(
            environ={
                "MSGRAPH_TENANT_ID": "t",
                "MSGRAPH_CLIENT_ID": "c",
            },
            on_device_code=callback,
        )

        assert provider._on_device_code is not None

    async def test_inspect_token_health(self):
        """inspect_token_health returns structured diagnostics."""
        import tempfile
        tmp = tempfile.mktemp(suffix=".json")
        creds = DelegatedGraphCredentials(
            "tenant", "client", "secret",
            token_cache_path=tmp,
        )
        provider = MicrosoftGraphDelegatedTokenProvider(creds)

        health = provider.inspect_token_health()
        assert health["configured"]
        assert health["tenant_id"] == "tenant"
        assert health["client_id"] == "client"
        assert not health["token_cache_exists"]  # File doesn't exist yet
        assert not health["cached_in_memory"]
