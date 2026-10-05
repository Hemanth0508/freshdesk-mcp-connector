"""MCP server (stdio) exposing read-only Freshdesk tools."""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .client import Config, ConnectorError, FreshdeskClient, load_dotenv

mcp = FastMCP("freshdesk-readonly")
_client: FreshdeskClient | None = None


def get_client() -> FreshdeskClient:
    """Lazy so the server starts (and lists tools) even before credentials are set."""
    global _client
    if _client is None:
        _client = FreshdeskClient(Config.from_env())
    return _client


def _run(fn, *args, **kwargs) -> dict[str, Any]:
    try:
        return fn(*args, **kwargs)
    except ConnectorError as e:
        raise ToolError(str(e)) from None


@mcp.tool()
def list_tickets(
    page: int = 1,
    per_page: int = 30,
    filter: str | None = None,
    updated_since: str | None = None,
    order_by: str | None = None,
    order_type: str | None = None,
    include_description: bool = False,
) -> dict:
    """List tickets (default: created in the last 30 days; use updated_since for older).

    filter: new_and_my_open | watching. updated_since: ISO 8601, e.g. 2026-01-01T00:00:00Z.
    order_by: created_at | due_by | updated_at | status. order_type: asc | desc.
    per_page max 100. include_description costs extra API credits. Returns has_more for paging.
    """
    return _run(
        get_client().list_tickets, page, per_page, filter, updated_since, order_by, order_type, include_description
    )


@mcp.tool()
def get_ticket(ticket_id: int, include_conversations: bool = False) -> dict:
    """Get one ticket by id. include_conversations adds up to 10 replies/notes (costs 2 API credits)."""
    return _run(get_client().get_ticket, ticket_id, include_conversations)


@mcp.tool()
def search_tickets(query: str, page: int = 1) -> dict:
    """Filter tickets by field values (NOT free-text). Examples: 'status:2 AND priority:>3',
    "type:'Problem' AND tag:'billing'", 'agent_id:null', "created_at:>'2026-01-01'".
    Fields: agent_id, group_id, priority, status, tag, type, due_by, fr_due_by, created_at,
    updated_at, closed_at, plus custom fields. Max 512 chars, pages 1-10, 30 per page.
    Status: 2 Open, 3 Pending, 4 Resolved, 5 Closed. Priority: 1 Low .. 4 Urgent.
    Newly changed tickets can take a few minutes to appear in search.
    """
    return _run(get_client().search_tickets, query, page)


@mcp.tool()
def list_contacts(page: int = 1, per_page: int = 30) -> dict:
    """List contacts (customers). per_page max 100."""
    return _run(get_client().list_contacts, page, per_page)


@mcp.tool()
def get_contact(contact_id: int) -> dict:
    """Get one contact by id."""
    return _run(get_client().get_contact, contact_id)


@mcp.tool()
def search_contacts(term: str) -> dict:
    """Find contacts by name keyword (autocomplete lookup). Returns matching contacts."""
    return _run(get_client().search_contacts, term)


def main() -> None:
    load_dotenv()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
