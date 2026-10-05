# Freshdesk MCP Connector (read-only)

A small private connector that lets an Agent Studio-style agent **read** support data from Freshdesk through MCP tools. Built for Razorpay Forward-Deployed Engineer Assignment 3.

Deployed domain: https://freshdesk-mcp-connector-chi.vercel.app/

## 1. What it does
Authenticates to Freshdesk with an API key and exposes six read-only MCP tools: list/get/search for tickets, and list/get/search for contacts. It handles HTTP 429 (honoring `Retry-After`), auth errors, not-found, timeouts and 5xx failures, and returns errors an agent can act on. It issues `GET` requests only.

## 2. Architecture / data flow
```
Agent (MCP client) --stdio--> freshdesk_mcp/server.py (FastMCP tools, input schemas)
                                   |
                                   v
                      freshdesk_mcp/client.py (auth, validation, retry/backoff, error mapping)
                                   |  HTTPS GET, Basic auth
                                   v
                      https://<domain>.freshdesk.com/api/v2
```
Two files, no database, no state, no frontend.

## 3. Prerequisites
Python 3.10+, and a Freshdesk trial/developer account holding **synthetic data only**.

## 4. Authentication
Freshdesk API v2 uses HTTP Basic auth with the agent's API key as username and any dummy password (`X`). Find the key in Freshdesk: profile picture > Profile settings > API key. The connector acts with that agent's permissions; use a low-privilege agent where possible. OAuth is not offered by this API for this use, so API key is used.

## 5. Environment variables
| Name | Required | Default | Meaning |
|---|---|---|---|
| `FRESHDESK_DOMAIN` | yes | | `acme` or `acme.freshdesk.com` |
| `FRESHDESK_API_KEY` | yes | | Agent API key |
| `FRESHDESK_TIMEOUT` | no | 15 | Seconds per request |
| `FRESHDESK_MAX_RETRIES` | no | 3 | Retries for 429 / 5xx / timeouts |
| `FRESHDESK_MAX_RETRY_WAIT` | no | 60 | Max seconds to sleep for one retry |

## 6. Installation
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env      # then edit .env (it is git-ignored)
```

## 7. Run
```bash
python -m freshdesk_mcp.server        # stdio MCP server (reads .env)
python scripts/demo.py                # MCP client calling every tool against your trial account
```
Register with any MCP client, for example:
```json
{ "mcpServers": { "freshdesk": {
    "command": "/abs/path/.venv/bin/python",
    "args": ["-m", "freshdesk_mcp.server"],
    "env": { "FRESHDESK_DOMAIN": "acme", "FRESHDESK_API_KEY": "<set locally>" } } } }
```

## 8. MCP tools
| Tool | Parameters | Returns |
|---|---|---|
| `list_tickets` | `page`=1, `per_page`=30 (max 100), `filter` (`new_and_my_open`\|`watching`), `updated_since` (ISO 8601), `order_by` (`created_at`\|`due_by`\|`updated_at`\|`status`), `order_type`, `include_description` | `{page, per_page, count, has_more, tickets[]}` |
| `get_ticket` | `ticket_id`, `include_conversations`=false | Ticket object (plus up to 10 conversations if requested) |
| `search_tickets` | `query` (field filter, see below), `page`=1 (1-10) | `{page, total, has_more, tickets[]}` |
| `list_contacts` | `page`, `per_page` | `{page, per_page, count, has_more, contacts[]}` |
| `get_contact` | `contact_id` | Contact object |
| `search_contacts` | `term` (name keyword) | `{count, contacts[]}` |

`search_tickets` wraps Freshdesk's *filter tickets* API, so queries are field-based, not free text: `status:2 AND priority:>3`, `type:'Problem' AND tag:'billing'`, `agent_id:null`, `created_at:>'2026-01-01'`. Status 2/3/4/5 = Open/Pending/Resolved/Closed; priority 1-4 = Low..Urgent. Max 512 chars, 30 per page, pages 1-10; recent changes can take a few minutes to be indexed.

## 9. Example
Agent asks "what urgent tickets are still open?" and calls:
```json
{"tool": "search_tickets", "arguments": {"query": "status:2 AND priority:4"}}
```
then `get_ticket` with an id from the result, using `include_conversations: true` for the thread.

## 10. Rate limits and errors
Freshdesk limits calls per minute per account (trial: 50/min) and replies `429` with `Retry-After` seconds. The client sleeps for that value and retries up to `FRESHDESK_MAX_RETRIES`; if the header is missing it uses exponential backoff (1s, 2s, 4s); if the wait would exceed `FRESHDESK_MAX_RETRY_WAIT` it fails fast with a message containing the wait time, so the agent can decide. 5xx and timeouts retry with backoff. Mapped errors: 400 validation (with Freshdesk's message), 401/403 auth, 404 not found, other statuses generic. Inputs are validated locally first, so bad calls cost no API credit. Note that `include_*` options cost extra credits.

## 11. What the agent CAN do
Browse recent tickets and contacts, page through them, fetch a ticket with its conversation thread, filter tickets by status, priority, type, tag, agent, group, dates and custom fields, and look up contacts by name.

## 12. What the agent CANNOT do
Create, update, reply to, delete, merge or assign anything. No free-text search over ticket subject/body. No archived tickets in search results, no attachments, no admin/config endpoints, no bulk export, no spam/deleted tickets.

## 13. Security
Credentials come from environment variables only; `.env` is git-ignored and `.env.example` holds placeholders. The API key is never logged or included in error messages. Only `GET` is implemented, and the domain is validated to a bare subdomain so requests only go to `*.freshdesk.com`. Tests use synthetic data and mocked HTTP. Ticket content is customer data: run it against test accounts, and treat anything returned to an agent as sensitive.

## 14. Limitations
Single account per process, stdio transport only, no caching, no streaming of large result sets, no OAuth. Rate limit is per account, so other API users share the budget. Ticket list defaults to the last 30 days unless `updated_since` is set (Freshdesk behavior). Contact endpoints (`/contacts`, `/contacts/{id}`, `/contacts/autocomplete`) are taken from Freshdesk's API index and covered by mocked tests; confirm them with `scripts/demo.py` on your account.

## 15. Tests
```bash
python -m pytest -q
```
37 offline tests (mocked HTTP, no network or credentials needed) cover config/auth header, list, get, search, validation and not-found, 429 with and without `Retry-After`, retry exhaustion, 5xx, timeouts, no write methods, and MCP tool invocation including error paths.

## 16. Live demo UI (optional, Vercel)
`public/index.html` + `api/run.py` give a hosted page where the unit tests, live smoke tests and each MCP tool run for real on the server. Nothing is canned.
- **Unit tests** run the repo's pytest suite against a mocked Freshdesk. They verify connector logic, not Freshdesk itself.
- **Live smoke tests** make about 9 read-only calls to a real account.
- **Keys:** set `FRESHDESK_DOMAIN` and `FRESHDESK_API_KEY` as Vercel environment variables for the shared demo account (never commit them), or paste your own key in the page; it is used for that request only and not stored or logged.
- **Safeguards:** read-only, synthetic data, best-effort throttle of 30 live calls/min per visitor on the shared key, short timeouts (2 retries, max 8s wait) to fit serverless limits. Revoke the key after review.
- Local preview: `python dev_server.py` then open http://localhost:8000. Deploy: import the repo in Vercel (or run `vercel`).
"# freshdesk-mcp-connector" 
