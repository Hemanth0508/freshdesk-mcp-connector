import pytest

from freshdesk_mcp.client import Config, FreshdeskClient

BASE = "https://acme.freshdesk.com/api/v2"
FAKE_KEY = "fake-test-key-123"  # synthetic, not a real credential


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def client(sleeps):
    cfg = Config(domain="acme", api_key=FAKE_KEY, max_retries=3, max_retry_wait=60)
    return FreshdeskClient(cfg, sleep=sleeps.append)
