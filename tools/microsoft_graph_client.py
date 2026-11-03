"""Reusable Microsoft Graph REST client helpers."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from agent.retry_utils import parse_retry_after_seconds
from tools.microsoft_graph_auth import MicrosoftGraphTokenProvider, format_graph_error


DEFAULT_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

Headers = dict[str, str] | None
Params = dict[str, Any] | None


class MicrosoftGraphClientError(RuntimeError):
    """Base class for Graph client failures."""


class MicrosoftGraphAPIError(MicrosoftGraphClientError):
    """Raised when a Graph API request fails."""

    def __init__(self, status_code: int, method: str, url: str, message: str, *,
                 retry_after_seconds: float | None = None, payload: Any = None) -> None:
        self.status_code, self.method, self.url = status_code, method, url
        self.retry_after_seconds, self.payload = retry_after_seconds, payload
        super().__init__(f"Microsoft Graph API error {status_code} for {method} {url}: {message}")


class MicrosoftGraphClient:
    """Minimal async Graph client. Retry policy (JSON requests and streaming downloads
    alike): transport errors back off exponentially; 401 clears the token cache and
    refetches; 429/5xx honor ``Retry-After``. Each attempt uses a fresh ``AsyncClient``."""

    def __init__(self, token_provider: MicrosoftGraphTokenProvider, *,
                 base_url: str = DEFAULT_GRAPH_BASE_URL, timeout: float = 60.0, max_retries: int = 3,
                 transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] | None = None,
                 user_agent: str = "Hermes-Agent/graph-client") -> None:
        self.token_provider, self.base_url, self.timeout = token_provider, base_url.rstrip("/"), timeout
        self.max_retries, self.user_agent = max(0, int(max_retries)), user_agent
        self._transport, self._sleep = transport, sleep or asyncio.sleep

    async def get_json(self, path: str, *, params: Params = None, headers: Headers = None) -> Any:
        return self._decode_json(await self._request("GET", path, params=params, headers=headers))

    async def post_json(self, path: str, *, json_body: Any | None = None, headers: Headers = None) -> Any:
        return self._decode_json(await self._request("POST", path, json_body=json_body, headers=headers))

    async def patch_json(self, path: str, *, json_body: Any | None = None, headers: Headers = None) -> Any:
        """Decoded body, or ``{}`` for a 204 / bodiless response."""
        response = await self._request("PATCH", path, json_body=json_body, headers=headers)
        return self._decode_json(response) if response.status_code != 204 and response.content else {}

    async def delete(self, path: str, *, headers: Headers = None) -> dict[str, Any]:
        """Decoded body, or ``{"deleted": True, "status_code"}`` for a 204 / bodiless response."""
        response = await self._request("DELETE", path, headers=headers)
        if response.status_code != 204 and response.content:
            return self._decode_json(response)
        return {"deleted": True, "status_code": response.status_code}

    async def collect_paginated(self, path: str, *, params: Params = None, headers: Headers = None) -> list[Any]:
        """Follow ``@odata.nextLink`` and concatenate every page's ``value`` list."""
        items: list[Any] = []
        # Query params go on the first request only; @odata.nextLink already embeds them.
        next_url, next_params = self._resolve_url(path), dict(params or {})
        while next_url:
            payload = self._decode_json(await self._request("GET", next_url, params=next_params or None, headers=headers))
            if not isinstance(payload, dict):
                raise MicrosoftGraphClientError(
                    f"Expected paginated Graph response dict, got {type(payload).__name__}.")
            if isinstance(payload.get("value"), list):
                items.extend(payload["value"])
            next_url, next_params = payload.get("@odata.nextLink"), {}
        return items

    async def download_to_file(self, path: str, destination: str | Path, *, headers: Headers = None,
                               chunk_size: int = 65536) -> dict[str, Any]:
        """Stream a Graph resource to disk chunk-by-chunk (large recordings never
        fit in memory); written to ``.part`` and renamed into place only on success."""
        url, target = self._resolve_url(path), Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_target = target.with_suffix(target.suffix + ".part")

        async def perform(client: httpx.AsyncClient, request_headers: dict[str, str]):
            try:
                async with client.stream("GET", url, headers=request_headers) as response:
                    if response.status_code >= 400:
                        await response.aread()  # small error body -> meaningful message
                        return response, None
                    with tmp_target.open("wb") as handle:
                        async for chunk in response.aiter_bytes(chunk_size=chunk_size):
                            if chunk:
                                handle.write(chunk)
                    return response, response.headers.get("content-type")
            except httpx.HTTPError:
                tmp_target.unlink(missing_ok=True)
                raise

        content_type = await self._with_retries("GET", url, "*/*", None, headers, perform, "download")
        os.replace(tmp_target, target)
        return {"path": str(target), "size_bytes": target.stat().st_size, "content_type": content_type}

    # ── Mail operations ────────────────────────────────────────────────

    FOLDER_LOOKUP: dict[str, str] = {
        "inbox": "me/mailFolders/inbox/messages",
        "sent": "me/mailFolders/sentItems/messages",
        "drafts": "me/mailFolders/drafts/messages",
        "deleted": "me/mailFolders/deletedItems/messages",
        "archive": "me/mailFolders/archive/messages",
        "junk": "me/mailFolders/junkEmail/messages",
        "outbox": "me/mailFolders/outbox/messages",
    }

    async def get_messages(
        self,
        folder: str = "inbox",
        *,
        top: int = 50,
        filter_str: str | None = None,
        sort: str = "receivedDateTime desc",
        fields: list[str] | None = None,
        include_pagination: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch messages from a mailbox folder via ``/me/mailFolders/{folder}/messages``.

        Args:
            folder: Folder name (``inbox``, ``sent``, ``drafts``, ``deleted``,
                    ``archive``, ``junk``, ``outbox``, or an arbitrary folder path).
            top: Maximum messages to return (default 50, Graph cap 1000).
            filter_str: OData ``$filter`` expression.
            sort: OData ``$orderby`` expression.
            fields: Subset of fields to return (``$select``).
            include_pagination: If true, follow ``@odata.nextLink`` to collect all pages.
        """
        endpoint = self.FOLDER_LOOKUP.get(
            folder, f"me/mailFolders/{folder}/messages"
        )
        params: dict[str, Any] = {"$top": min(top, 1000), "$orderby": sort}
        if filter_str:
            params["$filter"] = filter_str
        if fields:
            params["$select"] = ",".join(fields)

        if include_pagination:
            return await self.collect_paginated(endpoint, params=params)  # type: ignore[arg-type]
        result = await self.get_json(endpoint, params=params)
        if not isinstance(result, dict):
            return []
        return result.get("value", [])

    async def send_mail(
        self,
        to: list[str],
        subject: str,
        body: str,
        *,
        html: bool = True,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Send an email via ``POST /me/sendMail``.

        Args:
            to: Primary recipient email addresses.
            subject: Email subject line.
            body: Email body content.
            html: True if body is HTML, False for plain text.
            cc: CC recipient email addresses.
            bcc: BCC recipient email addresses.
            attachments: Optional list of attachment dicts (each with
                ``@odata.type``, ``name``, ``contentType``, ``contentBytes``).
        """
        message: dict[str, Any] = {
            "message": {
                "subject": subject,
                "body": {
                    "contentType": "HTML" if html else "text",
                    "content": body,
                },
                "toRecipients": [
                    {"emailAddress": {"address": addr}} for addr in to
                ],
            },
        }
        if cc:
            message["message"]["ccRecipients"] = [
                {"emailAddress": {"address": addr}} for addr in cc
            ]
        if bcc:
            message["message"]["bccRecipients"] = [
                {"emailAddress": {"address": addr}} for addr in bcc
            ]
        if attachments:
            message["message"]["attachments"] = attachments

        return await self.post_json("me/sendMail", json_body=message)

    # ── Calendar operations ────────────────────────────────────────────

    async def get_calendar_events(
        self,
        start: str,
        end: str,
        *,
        calendar_id: str = "me",
        include_cancelled: bool = False,
        fields: list[str] | None = None,
        timezone: str = "Europe/London",
    ) -> list[dict[str, Any]]:
        """Fetch calendar events within a date range via ``GET /{calendarId}/calendarView``.

        Args:
            start: Start of range (ISO datetime or YYYY-MM-DD).
            end: End of range (ISO datetime or YYYY-MM-DD).
            calendar_id: Calendar scope (``me``, ``user@domain.com``, or a calendar ID).
            include_cancelled: If true, include cancelled events.
            fields: Subset of fields to return (``$select``).
            timezone: Timezone for date/time parameters.
        """
        # Normalise to ISO datetime if date-only
        if "T" not in start:
            start = f"{start}T00:00:00"
        if "T" not in end:
            end = f"{end}T23:59:00"

        params: dict[str, Any] = {
            "startDateTime": start,
            "endDateTime": end,
        }
        if fields:
            params["$select"] = ",".join(fields)
        if not include_cancelled:
            params.setdefault("$filter", "")
            if params["$filter"]:
                params["$filter"] += " and "
            params["$filter"] += "isCancelled eq false"

        endpoint = f"{calendar_id}/calendarView"
        result = await self.get_json(
            endpoint,
            params=params,
            headers={"Prefer": f'outlook.timezone="{timezone}"'},
        )
        if not isinstance(result, dict):
            return []
        return result.get("value", [])

    async def create_calendar_event(
        self,
        event_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a calendar event via ``POST /me/events``.

        Args:
            event_data: Event properties. Supported keys:
                - ``subject`` (required)
                - ``start``, ``end`` (required, ISO format)
                - ``location`` (string)
                - ``body`` (string, HTML)
                - ``attendees`` (list of email strings or dicts)
                - ``show_as`` (``busy``, ``free``, ``tentative``, ``oof``, ``workingElsewhere``)
                - ``visibility`` (``default``, ``private``, ``confidential``)
                - ``reminders`` (int, minutes before)
                - ``all_day`` (bool)
                - ``categories`` (list of strings)
        """
        body = self._format_event_body(event_data)
        return await self.post_json("me/events", json_body=body)

    async def update_calendar_event(
        self,
        event_id: str,
        event_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Update an existing calendar event via ``PATCH /me/events/{id}``.

        Args:
            event_id: The event's Graph ID.
            event_data: Properties to update (same keys as ``create_calendar_event``).
        """
        body = self._format_event_body(event_data)
        return await self.patch_json(f"me/events/{event_id}", json_body=body)

    async def delete_calendar_event(
        self,
        event_id: str,
    ) -> dict[str, Any]:
        """Delete a calendar event via ``DELETE /me/events/{id}``."""
        return await self.delete(f"me/events/{event_id}")

    @staticmethod
    def _format_event_body(data: dict[str, Any]) -> dict[str, Any]:
        """Convert user-friendly event dict to Graph API format."""
        body: dict[str, Any] = {}

        if "subject" in data:
            body["subject"] = data["subject"]

        if "start" in data and "end" in data:
            if data.get("all_day"):
                body["start"] = {"date": data["start"], "timeZone": "UTC"}
                body["end"] = {"date": data["end"], "timeZone": "UTC"}
                body["isAllDay"] = True
            else:
                body["start"] = {
                    "dateTime": data["start"],
                    "timeZone": data.get("timezone", "Europe/London"),
                }
                body["end"] = {
                    "dateTime": data["end"],
                    "timeZone": data.get("timezone", "Europe/London"),
                }

        if "location" in data:
            body["location"] = {"displayName": data["location"]}

        if "body" in data:
            body["body"] = {
                "contentType": "HTML",
                "content": data["body"],
            }

        if "attendees" in data:
            body["attendees"] = [
                {"emailAddress": {"address": a}, "type": "required"}
                if isinstance(a, str)
                else a
                for a in data["attendees"]
            ]

        if "show_as" in data:
            body["showAs"] = data["show_as"]
        if "visibility" in data:
            body["visibility"] = data["visibility"]
        if "reminders" in data:
            body["reminderMinutesBeforeStart"] = (
                data["reminders"]
                if isinstance(data["reminders"], int)
                else data["reminders"][0]
            )
        if "categories" in data:
            body["categories"] = data["categories"]

        return body

    async def _request(self, method: str, path_or_url: str, *, params: Params = None,
                       json_body: Any | None = None, headers: Headers = None) -> httpx.Response:
        url = self._resolve_url(path_or_url)

        async def perform(client: httpx.AsyncClient, request_headers: dict[str, str]):
            response = await client.request(method, url, params=params, json=json_body, headers=request_headers)
            return response, response

        return await self._with_retries(method, url, "application/json", json_body, headers, perform, "request")

    async def _with_retries(
        self, method: str, url: str, accept: str, json_body: Any | None, headers: Headers,
        perform: Callable[[httpx.AsyncClient, dict[str, str]], Awaitable[tuple[httpx.Response, Any]]],
        kind: str) -> Any:
        """Run ``perform`` (-> ``(response, result)``) under the retry policy. ``kind``
        only labels transport-failure messages. Raises ``MicrosoftGraphAPIError`` once
        retries are exhausted or the status is not retryable; only 401 forces a token refresh."""
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            token = await self.token_provider.get_access_token(
                force_refresh=isinstance(last_error, MicrosoftGraphAPIError) and last_error.status_code == 401)
            request_headers = {"Authorization": f"Bearer {token}", "Accept": accept, "User-Agent": self.user_agent,
                               **({"Content-Type": "application/json"} if json_body is not None else {}),
                               **(headers or {})}
            exhausted = attempt >= self.max_retries
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout), transport=self._transport) as client:
                    response, result = await perform(client, request_headers)
            except httpx.HTTPError as exc:
                last_error, response = exc, None
                if exhausted:
                    raise MicrosoftGraphClientError(
                        f"Microsoft Graph {kind} failed for {method} {url}: {exc}") from exc
            else:
                if response.status_code < 400:
                    return result
                last_error, status = self._build_api_error(method, url, response), response.status_code
                if exhausted or not (status in (401, 429) or 500 <= status < 600):
                    raise last_error
                if status == 401:
                    self.token_provider.clear_cache()
            await self._sleep(self._retry_delay(response, attempt))
        raise MicrosoftGraphClientError(f"Microsoft Graph {kind} exhausted retries for {method} {url}.")

    def _resolve_url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        return f"{self.base_url}{path_or_url if path_or_url.startswith('/') else '/' + path_or_url}"

    @staticmethod
    def _decode_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise MicrosoftGraphClientError(
                "Microsoft Graph response was not valid JSON for "
                f"{response.request.method} {response.request.url}") from exc

    @staticmethod
    def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
        retry_after = parse_retry_after_seconds(response.headers) if response is not None else None
        return min(8.0, 0.5 * (2 ** attempt)) if retry_after is None else retry_after

    @staticmethod
    def _build_api_error(method: str, url: str, response: httpx.Response) -> MicrosoftGraphAPIError:
        try:
            payload: Any = response.json()
        except ValueError:
            payload = None
        detail = format_graph_error(payload.get("error")) if isinstance(payload, dict) else None
        return MicrosoftGraphAPIError(
            response.status_code, method, url, response.text.strip() or "unknown error" if detail is None else detail,
            retry_after_seconds=parse_retry_after_seconds(response.headers), payload=payload)
