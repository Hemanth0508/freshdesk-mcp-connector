import base64

import httpx
import pytest
import respx

from freshdesk_mcp.client import (
    ApiError, AuthError, Config, ConfigError, FreshdeskClient, NotFoundError,
    RateLimitError, ValidationError,
)
from .conftest import BASE, FAKE_KEY

TICKET = {"id": 1, "subject": "Synthetic ticket", "status": 2, "priority": 3}


# ---- configuration / authentication ----
def test_config_requires_credentials():
    with pytest.raises(ConfigError):
        Config.from_env({})
    with pytest.raises(ConfigError):
        Config.from_env({"FRESHDESK_DOMAIN": "acme"})


@pytest.mark.parametrize("raw", ["acme", "ACME.freshdesk.com", "https://acme.freshdesk.com/"])
def test_config_normalizes_domain(raw):
    assert Config.from_env({"FRESHDESK_DOMAIN": raw, "FRESHDESK_API_KEY": "k"}).domain == "acme"


def test_config_rejects_odd_domain():
    with pytest.raises(ConfigError):
        Config.from_env({"FRESHDESK_DOMAIN": "evil.com/x", "FRESHDESK_API_KEY": "k"})


@respx.mock
def test_basic_auth_uses_api_key_with_dummy_password(client):
    route = respx.get(f"{BASE}/tickets/1").respond(200, json=TICKET)
    client.get_ticket(1)
    sent = route.calls.last.request.headers["authorization"]
    assert sent == "Basic " + base64.b64encode(f"{FAKE_KEY}:X".encode()).decode()


@respx.mock
def test_invalid_credentials_401(client):
    respx.get(f"{BASE}/tickets/1").respond(401, json={"code": "invalid_credentials"})
    with pytest.raises(AuthError) as e:
        client.get_ticket(1)
    assert FAKE_KEY not in str(e.value)


@respx.mock
def test_forbidden_403(client):
    respx.get(f"{BASE}/contacts").respond(403, json={"description": "Access denied", "code": "access_denied"})
    with pytest.raises(AuthError):
        client.list_contacts()


# ---- list / get / search ----
@respx.mock
def test_list_tickets_params_and_paging(client):
    route = respx.get(f"{BASE}/tickets").respond(
        200, json=[TICKET], headers={"link": f'<{BASE}/tickets?page=2>; rel="next"'}
    )
    out = client.list_tickets(page=1, per_page=10, order_by="updated_at", order_type="desc")
    params = dict(route.calls.last.request.url.params)
    assert params == {"page": "1", "per_page": "10", "order_by": "updated_at", "order_type": "desc"}
    assert out["count"] == 1 and out["has_more"] is True and out["tickets"] == [TICKET]


@respx.mock
def test_list_tickets_last_page_has_no_more(client):
    respx.get(f"{BASE}/tickets").respond(200, json=[TICKET])
    assert client.list_tickets()["has_more"] is False


@respx.mock
def test_get_ticket_with_conversations(client):
    route = respx.get(f"{BASE}/tickets/7").respond(200, json={**TICKET, "id": 7})
    assert client.get_ticket(7, include_conversations=True)["id"] == 7
    assert route.calls.last.request.url.params["include"] == "conversations"


@respx.mock
def test_search_tickets_wraps_query_in_double_quotes(client):
    route = respx.get(f"{BASE}/search/tickets").respond(200, json={"total": 45, "results": [TICKET]})
    out = client.search_tickets("status:2 AND priority:>3")
    assert route.calls.last.request.url.params["query"] == '"status:2 AND priority:>3"'
    assert out["total"] == 45 and out["has_more"] is True and out["tickets"] == [TICKET]


@respx.mock
def test_contacts_list_get_search(client):
    respx.get(f"{BASE}/contacts").respond(200, json=[{"id": 5, "name": "Test User"}])
    respx.get(f"{BASE}/contacts/5").respond(200, json={"id": 5, "name": "Test User"})
    ac = respx.get(f"{BASE}/contacts/autocomplete").respond(200, json=[{"id": 5, "name": "Test User"}])
    assert client.list_contacts()["count"] == 1
    assert client.get_contact(5)["id"] == 5
    assert client.search_contacts("Test")["count"] == 1
    assert ac.calls.last.request.url.params["term"] == "Test"


# ---- invalid / not-found requests ----
@respx.mock
def test_not_found_404(client):
    respx.get(f"{BASE}/tickets/999").respond(404, json={})
    with pytest.raises(NotFoundError):
        client.get_ticket(999)


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.get_ticket(0),
        lambda c: c.list_tickets(per_page=101),
        lambda c: c.list_tickets(page=0),
        lambda c: c.list_tickets(filter="deleted"),
        lambda c: c.search_tickets("  "),
        lambda c: c.search_tickets("x" * 513),
        lambda c: c.search_tickets("status:2", page=11),
        lambda c: c.search_contacts(""),
    ],
)
def test_bad_input_rejected_before_any_http_call(client, call):
    with respx.mock:  # any real request would raise (unmocked)
        with pytest.raises(ValidationError):
            call(client)


@respx.mock
def test_400_surfaces_freshdesk_message(client):
    body = {"description": "Validation failed", "errors": [{"field": "query", "message": "bad query"}]}
    respx.get(f"{BASE}/search/tickets").respond(400, json=body)
    with pytest.raises(ValidationError, match="bad query"):
        client.search_tickets("nonsense:::")


# ---- rate limiting ----
@respx.mock
def test_429_honors_retry_after_then_succeeds(client, sleeps):
    route = respx.get(f"{BASE}/tickets/1").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "34"}), httpx.Response(200, json=TICKET)]
    )
    assert client.get_ticket(1)["id"] == 1
    assert sleeps == [34.0] and route.call_count == 2


@respx.mock
def test_429_without_retry_after_uses_backoff(client, sleeps):
    respx.get(f"{BASE}/tickets/1").mock(side_effect=[httpx.Response(429), httpx.Response(200, json=TICKET)])
    client.get_ticket(1)
    assert sleeps == [1.0]


@respx.mock
def test_429_gives_up_after_max_retries(client, sleeps):
    route = respx.get(f"{BASE}/tickets/1").respond(429, headers={"Retry-After": "5"})
    with pytest.raises(RateLimitError) as e:
        client.get_ticket(1)
    assert route.call_count == 4  # 1 try + 3 retries
    assert e.value.retry_after == 5.0 and len(sleeps) == 3


@respx.mock
def test_429_with_huge_retry_after_fails_fast(client, sleeps):
    route = respx.get(f"{BASE}/tickets/1").respond(429, headers={"Retry-After": "3600"})
    with pytest.raises(RateLimitError):
        client.get_ticket(1)
    assert route.call_count == 1 and sleeps == []


# ---- API failures ----
@respx.mock
def test_5xx_retried_then_succeeds(client, sleeps):
    respx.get(f"{BASE}/tickets/1").mock(side_effect=[httpx.Response(503), httpx.Response(200, json=TICKET)])
    assert client.get_ticket(1)["id"] == 1
    assert sleeps == [1.0]


@respx.mock
def test_persistent_5xx_raises_api_error(client):
    respx.get(f"{BASE}/tickets/1").respond(500)
    with pytest.raises(ApiError, match="500"):
        client.get_ticket(1)


@respx.mock
def test_timeout_retried_then_api_error(client, sleeps):
    respx.get(f"{BASE}/tickets/1").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ApiError, match="timed out"):
        client.get_ticket(1)
    assert len(sleeps) == 3


@respx.mock
def test_network_error_becomes_api_error(client):
    respx.get(f"{BASE}/tickets/1").mock(side_effect=httpx.ConnectError("dns"))
    with pytest.raises(ApiError, match="Network error"):
        client.get_ticket(1)


def test_client_has_no_write_methods():
    public = {n for n in dir(FreshdeskClient) if not n.startswith("_")}
    assert not {n for n in public if n.split("_")[0] in {"create", "update", "delete", "post", "put", "reply"}}
