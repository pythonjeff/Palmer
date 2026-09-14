"""Shared fixtures. Each one exists to delete a block that was copy-pasted
across the suite.

The env placeholders are set before any palmer module is imported: a few
modules read their key at import time (datafeeds raises KeyError without
TAVILY_API_KEY), and PALMER_NO_SCHEDULER keeps `from palmer import main` from
starting the job loop. Nothing in the suite needs a real value, and `.env` is
deliberately not loaded — real keys reaching a test is how the suite used to
go to the network by accident.
"""
import os
import socket

import pytest

for _k, _v in {
    "ANTHROPIC_API_KEY": "test",
    "TWILIO_ACCOUNT_SID": "ACtest",
    "TWILIO_AUTH_TOKEN": "test",
    "TWILIO_PHONE_NUMBER": "+15550000000",
    "TAVILY_API_KEY": "test",
    "TOMTOM_API_KEY": "test",
    "SERP_API_KEY": "test",
    "PALMER_NO_SCHEDULER": "1",
}.items():
    os.environ.setdefault(_k, _v)

from palmer import db  # after the env block, on purpose


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Every test is offline. A fetch that escapes its patch used to succeed
    silently on a machine with live keys and show up only as a slower suite;
    now it fails, naming itself. Socket level, because fetch functions are
    re-exported and patched under many module names."""
    def _refuse(*_a, **_k):
        raise RuntimeError("network call inside the test suite — patch the fetch")
    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse)


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """An Anthropic call nobody patched fails at once instead of reaching the
    network. Found the day the socket guard landed: a dozen tests were
    calling the real API with a placeholder key on every run and taking
    1.4s each in the SDK's retry backoff, then swallowing the error. Class
    level, so `patch.object(x.client.messages, "create")` in a test still
    wins — an instance attribute shadows this."""
    from palmer.llm import client
    def _refuse(*_a, **_k):
        raise RuntimeError("unpatched Anthropic call inside the test suite")
    monkeypatch.setattr(type(client.messages), "create", _refuse)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """An empty schema in a temp file, in place of the repo-root palmer.db."""
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test.db", raising=False)
    db.init_db()
    return db
