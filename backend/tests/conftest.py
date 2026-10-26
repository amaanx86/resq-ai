"""Test environment: force mock mode before any `app` module is imported.

Services are module-level singletons created at import time, so these variables must be
set here (pytest loads conftest.py before collecting test modules).
"""
import os

os.environ["MOCK_AWS"] = "True"
os.environ.pop("AWS_ENDPOINT_URL", None)
os.environ["AGENT_BACKEND"] = "local"
os.environ["AUTH_ENABLED"] = "False"
os.environ["SEED_DEMO_DATA"] = "True"
os.environ["DEMO_MODE"] = "False"
os.environ["WHATSAPP_PROVIDER"] = "meta"
os.environ["WHATSAPP_ACCESS_TOKEN"] = ""
os.environ["WHATSAPP_PHONE_NUMBER_ID"] = ""
os.environ.pop("WEBHOOK_QUEUE_URL", None)
os.environ.pop("PORTAL_EVENTS_TOPIC_ARN", None)
os.environ.pop("AGENTCORE_RUNTIME_ARN", None)
# Keep Transcribe polling tests fast.
os.environ["TRANSCRIBE_POLL_INTERVAL_SECONDS"] = "0"

import copy

import pytest

_STORE_ATTRS = ("_mock_users", "_mock_doctors", "_mock_doctor_phones", "_mock_admins", "_mock_cases", "_mock_messages",
                "_mock_idempotency", "_mock_direct_call_links")


@pytest.fixture(autouse=True)
def _isolated_store():
    """Each test starts from the same in-memory store (the seeded demo data) and its changes are
    undone afterwards. With one case per doctor (OFFER-4), cases a test leaves open would otherwise
    keep doctors busy for later tests."""
    from app.services.dynamo_service import dynamo_service
    saved = {name: copy.deepcopy(getattr(dynamo_service, name)) for name in _STORE_ATTRS
             if hasattr(dynamo_service, name)}
    yield
    for name, value in saved.items():
        setattr(dynamo_service, name, value)
