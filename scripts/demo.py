"""Live demo: an MCP client (standing in for the agent) calls the connector over stdio.

Needs FRESHDESK_DOMAIN / FRESHDESK_API_KEY (env or .env) for a trial account with a few
synthetic tickets. Without credentials it still lists the tools and shows the clean error.
Run:  python scripts/demo.py
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from freshdesk_mcp.client import load_dotenv


def show(title, result):
    text = result.content[0].text
    print(f"\n== {title} {'(ERROR)' if result.isError else ''}\n{text[:700]}{' ...' if len(text) > 700 else ''}")
    return None if result.isError else json.loads(text)


async def main():
    load_dotenv()
    params = StdioServerParameters(command=sys.executable, args=["-m", "freshdesk_mcp.server"], env=dict(os.environ))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        print("Tools:", [t.name for t in (await s.list_tools()).tools])

        tickets = show("list_tickets", await s.call_tool("list_tickets", {"per_page": 5, "updated_since": "2020-01-01T00:00:00Z"}))
        if tickets and tickets["tickets"]:
            tid = tickets["tickets"][0]["id"]
            show(f"get_ticket {tid}", await s.call_tool("get_ticket", {"ticket_id": tid}))
        show("search_tickets (open)", await s.call_tool("search_tickets", {"query": "status:2"}))
        show("list_contacts", await s.call_tool("list_contacts", {"per_page": 3}))
        show("get_ticket 999999999 (expected not-found)", await s.call_tool("get_ticket", {"ticket_id": 999999999}))


asyncio.run(main())
