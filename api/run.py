"""One serverless endpoint (Vercel) for the live demo UI. POST JSON {action, domain?, key?, ...}.

Actions: config | tools | unit (run the pytest suite for real) | smoke (live calls) | tool (call an MCP tool).
No key is stored or logged. Without a visitor-supplied key it falls back to server-side env vars.
"""
import asyncio
import contextlib
import io
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from freshdesk_mcp import server  # noqa: E402
from freshdesk_mcp.client import Config, ConnectorError, FreshdeskClient, NotFoundError, ValidationError  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)  # keep request URLs out of logs
LOCK = threading.Lock()  # server._client and respx mocking are process-global
HITS: dict[str, list[float]] = {}  # best-effort per-IP throttle for the shared demo key


class Skip(Exception): ...


def make_client(body, ip):
    domain, key = (body.get("domain") or "").strip(), (body.get("key") or "").strip()
    if not (domain and key):  # shared demo account
        domain, key = os.environ.get("FRESHDESK_DOMAIN", ""), os.environ.get("FRESHDESK_API_KEY", "")
        if not (domain and key):
            raise ConnectorError("No shared demo account configured. Enter your own domain and API key.")
        now = time.time()
        recent = [t for t in HITS.get(ip, []) if now - t < 60]
        if len(recent) >= 30:
            raise ConnectorError("Demo throttle: max 30 live calls per minute per visitor. Try again shortly.")
        HITS[ip] = recent + [now]
    # short timeouts/retries so a request finishes inside the serverless time limit
    return FreshdeskClient(Config.from_env({
        "FRESHDESK_DOMAIN": domain, "FRESHDESK_API_KEY": key,
        "FRESHDESK_TIMEOUT": "8", "FRESHDESK_MAX_RETRIES": "2", "FRESHDESK_MAX_RETRY_WAIT": "8"}))


def run_unit_tests():
    try:
        import pytest
    except ImportError:
        raise ConnectorError("pytest is not installed in this deployment (see README, Live demo section).") from None
    rows = []
    names = {"passed": "pass", "failed": "fail", "skipped": "skip"}

    class Collect:
        def pytest_runtest_logreport(self, report):
            if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
                rows.append({"id": report.nodeid.removeprefix("tests/"), "outcome": names[report.outcome],
                             "ms": round(report.duration * 1000, 1),
                             "detail": str(report.longrepr)[-300:] if report.failed else ""})

    t0, buf = time.time(), io.StringIO()
    with LOCK, contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        pytest.main([os.path.join(ROOT, "tests"), "-q", "-p", "no:cacheprovider", "--rootdir", ROOT],
                    plugins=[Collect()])
    return {"ok": True, "tests": rows, "ms": round((time.time() - t0) * 1000)}


def smoke(c):
    """Real calls against the Freshdesk account. Read-only; ~9 API calls."""
    rows, ctx = [], {}

    def step(name, fn):
        t0 = time.time()
        try:
            status, detail = "pass", fn() or ""
        except Skip as e:
            status, detail = "skip", str(e)
        except ConnectorError as e:
            status, detail = "fail", str(e)
        except Exception as e:  # never echo arbitrary exception text
            status, detail = "fail", type(e).__name__
        rows.append({"id": name, "outcome": status, "detail": detail, "ms": round((time.time() - t0) * 1000)})

    def expect(exc, fn, strict):
        try:
            fn()
        except exc as e:
            return f"raised {type(e).__name__} as expected: {e}"
        if strict:
            raise ConnectorError("call succeeded but an error was expected")
        raise Skip("Freshdesk accepted this input, so there was no error to check")

    def list_t():
        r = c.list_tickets(per_page=5, updated_since="2020-01-01T00:00:00Z")
        ctx["tid"] = r["tickets"][0]["id"] if r["tickets"] else None
        return f"{r['count']} ticket(s) returned, has_more={r['has_more']}"

    def get_t():
        if not ctx.get("tid"):
            raise Skip("no tickets in the account; create a synthetic one")
        return f"ticket #{c.get_ticket(ctx['tid'])['id']} fetched"

    def list_c():
        r = c.list_contacts(per_page=5)
        if r["contacts"]:
            ctx["cid"], ctx["cname"] = r["contacts"][0]["id"], r["contacts"][0].get("name") or ""
        return f"{r['count']} contact(s) returned"

    def get_c():
        if not ctx.get("cid"):
            raise Skip("no contacts in the account")
        return f"contact #{c.get_contact(ctx['cid'])['id']} fetched"

    def search_c():
        if len(ctx.get("cname", "")) < 2:
            raise Skip("no contact name to search for")
        return f"{c.search_contacts(ctx['cname'][:3])['count']} match(es) for the first 3 letters of a name"

    step("auth + list_tickets", list_t)
    if rows[0]["outcome"] == "fail":  # bad key/domain: no point continuing
        return rows
    step("get_ticket", get_t)
    step("search_tickets (status:2)", lambda: f"total={c.search_tickets('status:2')['total']}")
    step("list_contacts", list_c)
    step("get_contact", get_c)
    step("search_contacts", search_c)
    step("not found (ticket 999999999)", lambda: expect(NotFoundError, lambda: c.get_ticket(999999999), True))
    step("invalid search query", lambda: expect(ValidationError, lambda: c.search_tickets("notafield:1"), False))
    return rows


def dispatch(body, ip):
    a = body.get("action")
    if a == "config":
        return {"ok": True, "demo": bool(os.environ.get("FRESHDESK_DOMAIN") and os.environ.get("FRESHDESK_API_KEY"))}
    if a == "tools":
        tools = asyncio.run(server.mcp.list_tools())
        return {"ok": True, "tools": [{"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in tools]}
    if a == "unit":
        return run_unit_tests()
    if a == "smoke":
        return {"ok": True, "tests": smoke(make_client(body, ip))}
    if a == "tool":
        args = body.get("args") or {}
        if not isinstance(args, dict):
            raise ConnectorError("args must be an object")
        client = make_client(body, ip)
        with LOCK:  # runs the real MCP tool layer (schemas, validation, errors) in-process
            server._client = client
            try:
                res = asyncio.run(server.mcp.call_tool(str(body.get("tool")), args))
            except Exception as e:
                return {"ok": False, "error": str(e)}
            finally:
                server._client = None
        return {"ok": True, "result": json.loads(res[0].text)}
    raise ConnectorError("unknown action")


class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("cache-control", "no-store")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        try:
            n = int(self.headers.get("content-length") or 0)
            if n > 10_000:
                return self._send(413, {"ok": False, "error": "request too large"})
            body = json.loads(self.rfile.read(n) or b"{}")
            ip = (self.headers.get("x-forwarded-for") or self.client_address[0]).split(",")[0].strip()
            self._send(200, dispatch(body, ip))
        except ConnectorError as e:
            self._send(200, {"ok": False, "error": str(e)})
        except Exception as e:
            self._send(500, {"ok": False, "error": f"Server error: {type(e).__name__}"})

    def log_message(self, *args):  # never log request details
        pass
