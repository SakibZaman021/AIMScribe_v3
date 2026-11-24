"""
25 Nov 2026: the beta (testing) server says so on every page; the real one
does not change. Beta runs with AIMS_ENVIRONMENT=beta (docker-compose.beta.yml).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src"))

import environment                              # noqa: E402


@pytest.mark.asyncio
async def test_the_beta_dashboard_says_it_is_beta(monkeypatch):
    import dashboard
    monkeypatch.setenv("AIMS_ENVIRONMENT", "beta")
    page = (await dashboard.page()).body.decode()
    assert "BETA / TESTING SERVER" in page and 'class="beta-banner"' in page
    assert "<title>BETA (testing) - AIMScribe Server Dashboard</title>" in page
    assert "BETA - testing server" in page
    assert environment.REAL_DASHBOARD in page


@pytest.mark.asyncio
async def test_the_real_dashboard_is_unchanged(monkeypatch):
    import dashboard
    monkeypatch.delenv("AIMS_ENVIRONMENT", raising=False)
    page = (await dashboard.page()).body.decode()
    assert "BETA" not in page and "beta-banner" not in page
    assert "<title>AIMScribe Server Dashboard</title>" in page


def test_the_sign_in_pages_say_it_on_beta(monkeypatch):
    import portal
    monkeypatch.setenv("AIMS_ENVIRONMENT", "beta")
    beta = portal._page("Sign in", "<p>x</p>").body.decode()
    assert "BETA / TESTING SERVER" in beta and "BETA (testing) - Sign in" in beta
    monkeypatch.setenv("AIMS_ENVIRONMENT", "production")
    real = portal._page("Sign in", "<p>x</p>").body.decode()
    assert "BETA" not in real
