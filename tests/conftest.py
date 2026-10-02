"""Tests run the fake policy unsandboxed unless a test asks for bwrap, and
"inherit" model knobs resolve to sonnet instead of the developer's settings."""

import pytest


@pytest.fixture(autouse=True)
def _no_sandbox(monkeypatch):
    monkeypatch.setenv("RRSI_EVOLVE_SANDBOX", "none")


@pytest.fixture(autouse=True)
def _pinned_model(monkeypatch):
    monkeypatch.setenv("RRSI_MODEL", "sonnet")
