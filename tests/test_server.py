import asyncio
import json

import httpx
import pytest
import respx
from mcp.server.fastmcp.exceptions import ToolError

from freshdesk_mcp import server
from .conftest import BASE, FAKE_KEY


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("FRESHDESK_DOMAIN", "acme")
    monkeypatch.setenv("FRESHDESK_API_KEY", FAKE_KEY)
    monkeypatch.setattr(server, "_client", None)


def call(name, args):
    out = asyncio.run(server.mcp.call_tool(name, args))
    return json.loads(out[0].text)


def test_tools_are_registered_and_read_only():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert names == {"list_tickets", "get_ticket", "search_tickets", "list_contacts", "get_contact", "search_contacts"}


@respx.mock
def test_list_tickets_tool():
    respx.get(f"{BASE}/tickets").respond(200, json=[{"id": 1, "subject": "Synthetic"}])
    out = call("list_tickets", {"per_page": 5})
    assert out["count"] == 1 and out["tickets"][0]["id"] == 1


@respx.mock
def test_get_and_search_tools():
    respx.get(f"{BASE}/tickets/1").respond(200, json={"id": 1})
    respx.get(f"{BASE}/search/tickets").respond(200, json={"total": 1, "results": [{"id": 1}]})
    assert call("get_ticket", {"ticket_id": 1})["id"] == 1
    assert call("search_tickets", {"query": "status:2"})["total"] == 1


@respx.mock
def test_tool_error_for_not_found_and_rate_limit(monkeypatch):
    monkeypatch.setattr(server.get_client(), "_sleep", lambda s: None)
    respx.get(f"{BASE}/tickets/404").respond(404, json={})
    with pytest.raises(ToolError, match="Not found"):
        call("get_ticket", {"ticket_id": 404})
    respx.get(f"{BASE}/contacts").mock(return_value=httpx.Response(429, headers={"Retry-After": "7"}))
    with pytest.raises(ToolError, match="rate limit"):
        call("list_contacts", {})


def test_missing_credentials_gives_clear_tool_error(monkeypatch):
    monkeypatch.delenv("FRESHDESK_API_KEY")
    with pytest.raises(ToolError, match="FRESHDESK_API_KEY"):
        call("list_tickets", {})
