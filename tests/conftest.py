"""Tests run the fake policy unsandboxed unless a test asks for bwrap."""

import pytest


@pytest.fixture(autouse=True)
def _no_sandbox(monkeypatch):
    monkeypatch.setenv("RRSI_EVOLVE_SANDBOX", "none")
