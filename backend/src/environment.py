"""
Which server this is: the real one, or the beta (testing) one.

The beta server (deploy/uiu/BETA_SERVER.md) runs the same code at the same name
on port 9443, with AIMS_ENVIRONMENT=beta. Its pages look the same as the real
ones, so on beta every page says so plainly and links to the real dashboard
(25 Nov 2026, the owner's rule: nobody may mistake beta for the real site).
"""
from __future__ import annotations

import html
import os

REAL_SITE = "https://aimscribe.iriic.uiu.ac.bd/"
REAL_DASHBOARD = REAL_SITE + "dashboard"

BETA_LABEL = "BETA / TESTING SERVER"
BETA_NOTE = "Test data only - not the clinics' real recordings."


def is_beta() -> bool:
    return os.getenv("AIMS_ENVIRONMENT", "").strip().lower() == "beta"


def title(text: str) -> str:
    """A page's title, marked on beta so the browser tab says so too."""
    return f"BETA (testing) - {text}" if is_beta() else text


def banner_html(css_class: str = "") -> str:
    """The banner at the top of every beta page; nothing on the real server."""
    if not is_beta():
        return ""
    attrs = f' class="{css_class}"' if css_class else (
        ' style="background:#f59e0b;color:#1b1b1f;padding:10px 16px;'
        'font:600 15px/1.4 system-ui,Segoe UI,sans-serif;text-align:center"')
    return (f'<div{attrs} role="note"><strong>{html.escape(BETA_LABEL)}</strong> - '
            f'{html.escape(BETA_NOTE)} The real AIMScribe dashboard is '
            f'<a href="{REAL_DASHBOARD}">{html.escape(REAL_SITE.rstrip("/"))}</a>.</div>')


__all__ = ["REAL_SITE", "REAL_DASHBOARD", "BETA_LABEL", "BETA_NOTE", "is_beta", "title",
           "banner_html"]
