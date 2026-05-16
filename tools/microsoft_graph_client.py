"""Reusable Microsoft Graph REST client helpers."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

from agent.retry_utils import parse_retry_after_seconds
from tools.microsoft_graph_auth import GraphCredentials, MicrosoftGraphTokenProvider


DEFAULT_GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


class MicrosoftGraphClientError(RuntimeError):
    """Base class for Graph client failures."""


class MicrosoftGraphAPIError(MicrosoftGraphClientError):
    """Raised when a Graph API request fails."""

    def __init__(
        self,
        status_code: int,
        method: str,
        url: str,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        payload: Any = None,
    ) -> None:
        self.status_code = status_code
        self.method = method
        self.url = url
        self.retry_after_seconds = retry_after_seconds
        self.payload = payload
        super().__init__(
            f"Microsoft Graph API error {status_code} for {method} {url}: {message}"
        )


class MicrosoftGraphClient:
    """Minimal async Microsoft Graph client with retries and pagination."""

    def __init__(
        self,
        token_provider: Any,
        *,
        base_url: str = DEFAULT_GRAPH_BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        user_agent: str = "Hermes-Agent/graph-client",
    ) -> None:
        self.token_provider = token_provider
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self._transport = transport
        self._sleep = sleep or asyncio.sleep
        self.user_agent = user_agent

    @classmethod
    def from_env(cls, **kwargs: Any) -> "MicrosoftGraphClient":
        credentials = GraphCredentials.from_env()
        provider = MicrosoftGraphTokenProvider(credentials)
        return cls(provider, **kwargs)

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        response = await self._request("GET", path, params=params, headers=headers)
        return self._decode_json(response)

    async def post_json(
        self,
        path: str,
        *,
        json_body: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        response = await self._request("POST", path, json_body=json_body, headers=headers)
        return self._decode_json(response)

    async def patch_json(
        self,
        path: str,
        *,
        json_body: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        response = await self._request("PATCH", path, json_body=json_body, headers=headers)
        if response.status_code == 204 or not response.content:
            return {}
        return self._decode_json(response)

    async def delete(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = await self._request("DELETE", path, headers=headers)
        if response.status_code == 204 or not response.content:
            return {"deleted": True, "status_code": response.status_code}
        return self._decode_json(response)

    async def iterate_pages(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        next_url: str | None = self._resolve_url(path)
        next_params = dict(params or {})
        while next_url:
            response = await self._request(
                "GET",
                next_url,
                params=next_params or None,
                headers=headers,
            )
            payload = self._decode_json(response)
            if not isinstance(payload, dict):
                raise MicrosoftGraphClientError(
                    f"Expected paginated Graph response dict, got {type(payload).__name__}."
                )
            yield payload
            next_url = payload.get("@odata.nextLink")
            next_params = {}

    async def collect_paginated(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> list[Any]:
        items: list[Any] = []
        async for page in self.iterate_pages(path, params=params, headers=headers):
            value = page.get("value")
            if isinstance(value, list):
                items.extend(value)
        return items

    async def download_to_file(
        self,
        path: str,
        destination: str | Path,
        *,
        headers: dict[str, str] | None = None,
        chunk_size: int = 65536,
    ) -> dict[str, Any]:
        """Download a Graph resource to disk, streaming the response body.

        The body is written chunk-by-chunk via ``response.aiter_bytes`` with
        the ``httpx.AsyncClient`` kept open for the duration of the iteration,
        so recordings and other large artifacts do not need to fit in memory.
        """
        url = self._resolve_url(path)
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_target = target.with_suffix(target.suffix + ".part")

        attempt = 0
        last_error: Exception | None = None

        while attempt <= self.max_retries:
            token = await self.token_provider.get_access_token(
                force_refresh=attempt > 0 and self._should_refresh_token(last_error)
            )
            request_headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "*/*",
                "User-Agent": self.user_agent,
            }
            if headers:
                request_headers.update(headers)

            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self.timeout),
                    transport=self._transport,
                ) as client:
                    async with client.stream(
                        "GET",
                        url,
                        headers=request_headers,
                    ) as response:
                        if response.status_code >= 400:
                            # Materialize error body so we can surface a meaningful
                            # message; error bodies are small.
                            await response.aread()
                            api_error = self._build_api_error("GET", url, response)
                            last_error = api_error

                            if (
                                response.status_code == 401
                                and attempt < self.max_retries
                            ):
                                self.token_provider.clear_cache()
                                await self._sleep(
                                    self._retry_delay(response, attempt)
                                )
                                attempt += 1
                                continue

                            if (
                                self._should_retry(response)
                                and attempt < self.max_retries
                            ):
                                await self._sleep(
                                    self._retry_delay(response, attempt)
                                )
                                attempt += 1
                                continue

                            raise api_error

                        content_type = response.headers.get("content-type")
                        with tmp_target.open("wb") as handle:
                            async for chunk in response.aiter_bytes(
                                chunk_size=chunk_size
                            ):
                                if chunk:
                                    handle.write(chunk)
            except httpx.HTTPError as exc:
                last_error = exc
                tmp_target.unlink(missing_ok=True)
                if attempt >= self.max_retries:
                    raise MicrosoftGraphClientError(
                        f"Microsoft Graph download failed for GET {url}: {exc}"
                    ) from exc
                await self._sleep(self._retry_delay(None, attempt))
                attempt += 1
                continue

            os.replace(tmp_target, target)
            return {
                "path": str(target),
                "size_bytes": target.stat().st_size,
                "content_type": content_type,
            }

        tmp_target.unlink(missing_ok=True)
        raise MicrosoftGraphClientError(
            f"Microsoft Graph download exhausted retries for GET {url}."
        )

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

    async def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        url = self._resolve_url(path_or_url)
        attempt = 0
        last_error: Exception | None = None

        while attempt <= self.max_retries:
            token = await self.token_provider.get_access_token(
                force_refresh=attempt > 0 and self._should_refresh_token(last_error)
            )
            request_headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            }
            if json_body is not None:
                request_headers["Content-Type"] = "application/json"
            if headers:
                request_headers.update(headers)

            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self.timeout),
                    transport=self._transport,
                ) as client:
                    response = await client.request(
                        method,
                        url,
                        params=params,
                        json=json_body,
                        headers=request_headers,
                    )
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise MicrosoftGraphClientError(
                        f"Microsoft Graph request failed for {method} {url}: {exc}"
                    ) from exc
                await self._sleep(self._retry_delay(None, attempt))
                attempt += 1
                continue

            if response.status_code < 400:
                return response

            api_error = self._build_api_error(method, url, response)
            last_error = api_error

            if response.status_code == 401 and attempt < self.max_retries:
                self.token_provider.clear_cache()
                await self._sleep(self._retry_delay(response, attempt))
                attempt += 1
                continue

            if self._should_retry(response) and attempt < self.max_retries:
                await self._sleep(self._retry_delay(response, attempt))
                attempt += 1
                continue

            raise api_error

        raise MicrosoftGraphClientError(
            f"Microsoft Graph request exhausted retries for {method} {url}."
        )

    def _resolve_url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        path = path_or_url if path_or_url.startswith("/") else f"/{path_or_url}"
        return f"{self.base_url}{path}"

    @staticmethod
    def _decode_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise MicrosoftGraphClientError(
                "Microsoft Graph response was not valid JSON for "
                f"{response.request.method} {response.request.url}"
            ) from exc

    @staticmethod
    def _should_retry(response: httpx.Response | None) -> bool:
        if response is None:
            return True
        return response.status_code == 429 or 500 <= response.status_code < 600

    @staticmethod
    def _should_refresh_token(error: Exception | None) -> bool:
        return isinstance(error, MicrosoftGraphAPIError) and error.status_code == 401

    @staticmethod
    def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = parse_retry_after_seconds(response.headers)
            if retry_after is not None:
                return retry_after
        return min(8.0, 0.5 * (2 ** attempt))

    @staticmethod
    def _build_api_error(
        method: str,
        url: str,
        response: httpx.Response,
    ) -> MicrosoftGraphAPIError:
        payload: Any = None
        message = response.text.strip() or "unknown error"
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                inner_message = error.get("message")
                if code and inner_message:
                    message = f"{code}: {inner_message}"
                elif inner_message:
                    message = str(inner_message)
            elif isinstance(error, str):
                message = error

        retry_after: float | None = parse_retry_after_seconds(response.headers)

        return MicrosoftGraphAPIError(
            response.status_code,
            method,
            url,
            message,
            retry_after_seconds=retry_after,
            payload=payload,
        )
