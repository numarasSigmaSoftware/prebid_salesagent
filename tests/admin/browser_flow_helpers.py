"""Helpers for browser-driven UI tests."""

from __future__ import annotations

import re
from contextlib import contextmanager

import pytest

playwright = pytest.importorskip(
    "playwright.sync_api",
    reason="Install the project's ui-tests extras to run browser UI coverage.",
)

Page = playwright.Page
sync_playwright = playwright.sync_playwright


@contextmanager
def browser_page(base_url: str):
    """Launch a headless Chromium page for UI testing."""
    with sync_playwright() as session:
        try:
            browser = session.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Playwright browser runtime is not available: {exc}")

        context = browser.new_context(base_url=base_url)
        page = context.new_page()
        try:
            yield page
        finally:
            context.close()
            browser.close()


def install_dialog_recorder(page: Page) -> list[str]:
    """Capture browser dialogs and accept them automatically."""
    messages: list[str] = []

    def _handle(dialog):
        messages.append(dialog.message)
        dialog.accept()

    page.on("dialog", _handle)
    return messages


def login_as_tenant_admin(page: Page, base_url: str, tenant_id: str) -> None:
    """Authenticate using the real test-login page."""
    page.goto(f"{base_url}/tenant/{tenant_id}/login", wait_until="domcontentloaded")
    login_button = page.locator(
        "button:has-text('Log in as Tenant Admin'), "
        "button:has-text('Log in to Dashboard'), "
        "button:has-text('Log in with Test Credentials')"
    ).first
    login_button.wait_for(state="visible", timeout=10000)
    login_button.click()
    page.wait_for_url(re.compile(rf".*/tenant/{re.escape(tenant_id)}(?:/.*)?$"), timeout=15000)


def wait_for_flash_or_text(page: Page, text: str) -> None:
    """Wait for a success message or other key UI text."""
    page.get_by_text(text, exact=False).first.wait_for(state="visible", timeout=15000)
