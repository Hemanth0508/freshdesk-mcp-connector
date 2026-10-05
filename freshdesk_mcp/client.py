"""Minimal read-only Freshdesk API v2 client.

Verified against https://developers.freshdesk.com/api :
  - Auth: HTTP Basic, API key as username, any dummy password ("X").
  - Base URL: https://{domain}.freshdesk.com/api/v2
  - Pagination: page (from 1) + per_page (max 100); Link header carries rel="next".
  - Rate limit: per account per minute (trial = 50/min); 429 + Retry-After (seconds).
  - Filter tickets: GET /search/tickets?query="..." (double-quoted, URL-encoded,
    <=512 chars, 30 results/page, page <= 10, returns {"total", "results"}).
Only GET is ever issued, so the connector cannot modify the helpdesk.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

TICKET_ORDER_BY = {"created_at", "due_by", "updated_at", "status"}
TICKET_FILTERS = {"new_and_my_open", "watching"}  # spam/deleted intentionally not exposed


def load_dotenv(path: str = ".env") -> None:
    """Tiny .env reader (KEY=VALUE, '#' comments). Real environment variables win."""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = re.split(r"\s+#", value, maxsplit=1)[0].strip().strip("\"'")
        os.environ.setdefault(key.strip(), value)


class ConnectorError(Exception):
    """Base error; messages are safe to show to an agent (never contain the API key)."""


class ConfigError(ConnectorError): ...
class AuthError(ConnectorError): ...
class NotFoundError(ConnectorError): ...
class ValidationError(ConnectorError): ...


class RateLimitError(ConnectorError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class ApiError(ConnectorError): ...


@dataclass(frozen=True)
class Config:
    domain: str
    api_key: str
    timeout: float = 15.0
    max_retries: int = 3
    max_retry_wait: float = 60.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        raw_domain = (env.get("FRESHDESK_DOMAIN") or "").strip().lower()
        api_key = (env.get("FRESHDESK_API_KEY") or "").strip()
        if not raw_domain or not api_key:
            raise ConfigError("Set FRESHDESK_DOMAIN and FRESHDESK_API_KEY (see .env.example).")
        domain = re.sub(r"^https?://", "", raw_domain).split("/")[0]
        domain = domain.removesuffix(".freshdesk.com")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", domain):
            raise ConfigError("FRESHDESK_DOMAIN must be a subdomain like 'acme' or 'acme.freshdesk.com'.")
        try:
            return cls(
                domain=domain,
                api_key=api_key,
                timeout=float(env.get("FRESHDESK_TIMEOUT", 15)),
                max_retries=int(env.get("FRESHDESK_MAX_RETRIES", 3)),
                max_retry_wait=float(env.get("FRESHDESK_MAX_RETRY_WAIT", 60)),
            )
        except ValueError as e:
            raise ConfigError(f"Invalid numeric setting: {e}") from None


def _check_page(page: int, per_page: int | None = None) -> None:
    if page < 1:
        raise ValidationError("page must be >= 1")
    if per_page is not None and not 1 <= per_page <= 100:
        raise ValidationError("per_page must be between 1 and 100")


def _check_id(value: int, name: str) -> None:
    if not isinstance(value, int) or value < 1:
        raise ValidationError(f"{name} must be a positive integer")


class FreshdeskClient:
    def __init__(
        self,
        config: Config,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=f"https://{config.domain}.freshdesk.com/api/v2",
            auth=(config.api_key, "X"),
            timeout=config.timeout,
            headers={"Accept": "application/json"},
            transport=transport,
        )

    # ---- HTTP core: GET only, retries on 429 / 5xx / network errors ----
    def _get(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, httpx.Headers]:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        while True:
            try:
                resp = self._http.get(path, params=params)
            except httpx.TimeoutException:
                if attempt >= self.config.max_retries:
                    raise ApiError("Freshdesk request timed out after retries") from None
                self._backoff(attempt)
                attempt += 1
                continue
            except httpx.HTTPError as e:
                if attempt >= self.config.max_retries:
                    raise ApiError(f"Network error talking to Freshdesk: {type(e).__name__}") from None
                self._backoff(attempt)
                attempt += 1
                continue

            code = resp.status_code
            if code == 429:
                wait = self._retry_after(resp)
                if attempt >= self.config.max_retries or (wait or 0) > self.config.max_retry_wait:
                    raise RateLimitError(
                        f"Freshdesk rate limit hit; retry after {wait if wait is not None else 'a minute'}s",
                        retry_after=wait,
                    )
                self._sleep(wait if wait is not None else self._delay(attempt))
                attempt += 1
                continue
            if code >= 500:
                if attempt >= self.config.max_retries:
                    raise ApiError(f"Freshdesk server error (HTTP {code})")
                self._backoff(attempt)
                attempt += 1
                continue
            if 200 <= code < 300:
                return resp.json(), resp.headers
            raise self._map_error(resp)

    @staticmethod
    def _delay(attempt: int) -> float:
        return min(2.0**attempt, 30.0)

    def _backoff(self, attempt: int) -> None:
        self._sleep(self._delay(attempt))

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        try:
            return max(float(resp.headers["Retry-After"]), 0.0)
        except (KeyError, ValueError):
            return None

    @staticmethod
    def _map_error(resp: httpx.Response) -> ConnectorError:
        code = resp.status_code
        detail = ""
        try:
            body = resp.json()
            detail = body.get("description") or body.get("message") or ""
            errs = "; ".join(f"{e.get('field')}: {e.get('message')}" for e in body.get("errors", []))
            detail = f"{detail} ({errs})" if errs else detail
        except Exception:
            pass
        if code == 401:
            return AuthError("Authentication failed (HTTP 401): check FRESHDESK_API_KEY and FRESHDESK_DOMAIN")
        if code == 403:
            return AuthError(f"Access denied (HTTP 403): the API key's agent lacks permission. {detail}".strip())
        if code == 404:
            return NotFoundError("Not found (HTTP 404): no such record, or wrong FRESHDESK_DOMAIN")
        if code == 400:
            return ValidationError(f"Freshdesk rejected the request (HTTP 400): {detail}".strip())
        return ApiError(f"Freshdesk API error (HTTP {code}). {detail}".strip())

    # ---- Tickets ----
    def list_tickets(
        self,
        page: int = 1,
        per_page: int = 30,
        filter: str | None = None,
        updated_since: str | None = None,
        order_by: str | None = None,
        order_type: str | None = None,
        include_description: bool = False,
    ) -> dict[str, Any]:
        _check_page(page, per_page)
        if filter and filter not in TICKET_FILTERS:
            raise ValidationError(f"filter must be one of {sorted(TICKET_FILTERS)}")
        if order_by and order_by not in TICKET_ORDER_BY:
            raise ValidationError(f"order_by must be one of {sorted(TICKET_ORDER_BY)}")
        if order_type and order_type not in {"asc", "desc"}:
            raise ValidationError("order_type must be 'asc' or 'desc'")
        data, headers = self._get(
            "/tickets",
            {
                "page": page,
                "per_page": per_page,
                "filter": filter,
                "updated_since": updated_since,
                "order_by": order_by,
                "order_type": order_type,
                "include": "description" if include_description else None,
            },
        )
        return {
            "page": page,
            "per_page": per_page,
            "count": len(data),
            "has_more": 'rel="next"' in headers.get("link", ""),
            "tickets": data,
        }

    def get_ticket(self, ticket_id: int, include_conversations: bool = False) -> dict[str, Any]:
        _check_id(ticket_id, "ticket_id")
        data, _ = self._get(
            f"/tickets/{ticket_id}", {"include": "conversations" if include_conversations else None}
        )
        return data

    def search_tickets(self, query: str, page: int = 1) -> dict[str, Any]:
        """Filter tickets by field values, e.g. status:2 AND priority:>3. Not free-text search."""
        _check_page(page)
        query = (query or "").strip().strip('"')
        if not query:
            raise ValidationError("query is required, e.g. 'status:2 AND priority:3'")
        if len(query) > 512:
            raise ValidationError("query must be at most 512 characters")
        if page > 10:
            raise ValidationError("Freshdesk search only serves pages 1-10")
        data, _ = self._get("/search/tickets", {"query": f'"{query}"', "page": page})
        total = data.get("total", 0)
        return {
            "page": page,
            "total": total,
            "has_more": page < 10 and page * 30 < total,
            "tickets": data.get("results", []),
        }

    # ---- Contacts ----
    def list_contacts(self, page: int = 1, per_page: int = 30) -> dict[str, Any]:
        _check_page(page, per_page)
        data, headers = self._get("/contacts", {"page": page, "per_page": per_page})
        return {
            "page": page,
            "per_page": per_page,
            "count": len(data),
            "has_more": 'rel="next"' in headers.get("link", ""),
            "contacts": data,
        }

    def get_contact(self, contact_id: int) -> dict[str, Any]:
        _check_id(contact_id, "contact_id")
        data, _ = self._get(f"/contacts/{contact_id}")
        return data

    def search_contacts(self, term: str) -> dict[str, Any]:
        """Keyword (name prefix) lookup via /contacts/autocomplete?term=."""
        term = (term or "").strip()
        if not term:
            raise ValidationError("term is required")
        data, _ = self._get("/contacts/autocomplete", {"term": term})
        return {"count": len(data), "contacts": data}
