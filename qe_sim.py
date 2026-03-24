"""qe_sim.py — QuestionablyEpic Upgrade Finder automation for healer specs."""

import logging
import re
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

log = logging.getLogger(__name__)

QE_BASE    = "https://questionablyepic.com"
QE_UF_URL  = QE_BASE + "/live/upgradefinder"
DEBUG_SHOT = Path(__file__).parent / "qe_debug.png"

# Spec IDs that should be routed to QE instead of Raidbots
HEALER_SPEC_IDS: set[int] = {
    65,   # Holy Paladin
    105,  # Restoration Druid
    256,  # Discipline Priest
    257,  # Holy Priest
    264,  # Restoration Shaman
    270,  # Mistweaver Monk
    1468, # Preservation Evoker
}

# Maps spec_id → the exact name shown in the QE "Current Spec" dropdown
_QE_SPEC_NAMES: dict[int, str] = {
    65:   "Holy Paladin",
    105:  "Restoration Druid",
    256:  "Discipline Priest",
    257:  "Holy Priest",
    264:  "Restoration Shaman",
    270:  "Mistweaver Monk",
    1468: "Preservation Evoker",
}


def is_healer(spec_id: int) -> bool:
    return int(spec_id) in HEALER_SPEC_IDS


def _js_click(page, locator, timeout: int = 10_000) -> None:
    """Wait for locator to be visible, then click via page.evaluate() with
    a raw element handle — bypasses Playwright pointer-event checks entirely."""
    locator.wait_for(state="visible", timeout=timeout)
    locator.scroll_into_view_if_needed(timeout=timeout)
    handle = locator.element_handle(timeout=timeout)
    page.evaluate("el => el.click()", handle)


def run_qe_upgradefinder(simc: str, spec_id: int = 0, timeout_minutes: int = 5) -> str:
    """
    Automate the QE Upgrade Finder with the given SimC string.

    Flow (confirmed via manual testing):
      1. Select the correct spec from the Current Spec dropdown
      2. Dismiss cookie consent if present
      3. Click IMPORT GEAR to open the SimC paste dialog
      4. Paste the SimC string and click SUBMIT
      5. Wait for the dialog to auto-close (QE processes the SimC)
      6. Click GO! (HEROIC MAX + MYTHIC MAX are pre-selected by default)
      7. Wait for the URL to navigate to /upgradereport/...

    Returns the shareable report URL.  Raises RuntimeError on failure.
    """
    timeout_ms = timeout_minutes * 60 * 1000

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx  = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        try:
            # Block cookie/consent scripts so overlays never intercept clicks.
            for pattern in ["**/*ncmp*", "**/*privacymanager*", "**/*cookieconsent*",
                             "**/*consent-manager*", "**/*trustarc*", "**/*onetrust*"]:
                page.route(pattern, lambda route: route.abort())

            log.info("QE: navigating to upgrade finder...")
            page.goto(QE_UF_URL, timeout=30_000)
            page.wait_for_load_state("networkidle", timeout=30_000)
            log.info("QE: page loaded.")

            # ── Step 1: Dismiss cookie consent popup if present ───────────────
            try:
                accept_btn = page.locator("button").filter(
                    has_text=re.compile(r"^\s*accept\s*$", re.I)
                ).first
                accept_btn.wait_for(state="visible", timeout=5_000)
                _js_click(page, accept_btn, timeout=5_000)
                log.info("QE: cookie consent accepted.")
                page.wait_for_timeout(500)
            except PWTimeout:
                log.info("QE: no cookie consent popup.")

            # ── Step 2: Select the correct spec ──────────────────────────────
            qe_spec_name = _QE_SPEC_NAMES.get(int(spec_id)) if spec_id else None
            if qe_spec_name:
                # MUI Select needs a proper Playwright click (mousedown + mouseup)
                # to open — page.evaluate el.click() only fires a synthetic click
                # which MUI ignores for its dropdown trigger.
                # Cookie popup is already gone so Playwright click works fine here.
                select_btn = page.locator('.MuiSelect-select[role="button"]').first
                select_btn.click(timeout=10_000)
                log.info("QE: spec dropdown opened.")
                page.wait_for_timeout(200)
                page.screenshot(path=str(DEBUG_SHOT))
                log.info("QE: screenshot 200ms after dropdown click saved.")
                # Wait for the MUI portal listbox to appear then click the option
                page.wait_for_selector('[role="listbox"]', timeout=5_000)
                option = page.locator(f'[data-value="{qe_spec_name}"]')
                _js_click(page, option, timeout=5_000)
                log.info("QE: spec '%s' selected.", qe_spec_name)
                page.wait_for_timeout(500)

            # ── Step 3: Open the SimC import dialog via IMPORT GEAR ──────────
            import_btn = page.locator("button").filter(
                has_text=re.compile(r"^\s*IMPORT\s+GEAR\s*$", re.I)
            ).first
            _js_click(page, import_btn, timeout=10_000)
            page.wait_for_selector("textarea", timeout=10_000)
            log.info("QE: IMPORT GEAR dialog opened.")

            # ── Step 4: Paste the SimC string ────────────────────────────────
            textarea = page.locator("textarea").first
            textarea.wait_for(state="visible", timeout=10_000)
            textarea.fill(simc)
            log.info("QE: SimC string entered.")

            # ── Step 5: Submit ────────────────────────────────────────────────
            submit_btn = page.locator("button").filter(
                has_text=re.compile(r"^\s*SUBMIT\s*$", re.I)
            ).first
            _js_click(page, submit_btn, timeout=10_000)
            log.info("QE: SUBMIT clicked, waiting for dialog to close...")

            # QE closes the dialog itself once the character loads.
            try:
                page.wait_for_selector('[role="dialog"]', state="hidden", timeout=15_000)
                log.info("QE: dialog auto-closed.")
            except PWTimeout:
                log.warning("QE: dialog still open after 15s, forcing close...")
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
                if page.locator('[role="dialog"]').count() > 0:
                    page.mouse.click(10, 10)
                    page.wait_for_timeout(500)

            page.screenshot(path=str(DEBUG_SHOT))
            log.info("QE: pre-GO screenshot saved.")

            # ── Step 6: Click GO! ─────────────────────────────────────────────
            # HEROIC (MAX) and MYTHIC (MAX) are already selected by default.
            go_btn = page.locator("button").filter(
                has_text=re.compile(r"^\s*go[!.]?\s*$", re.I)
            ).first
            _js_click(page, go_btn, timeout=10_000)
            log.info("QE: GO! clicked.")

            # ── Step 7: Wait for report URL ───────────────────────────────────
            # Use wait_for_function so SPA pushState navigation is detected.
            page.wait_for_function(
                "window.location.href.includes('upgradereport')",
                timeout=timeout_ms,
            )
            report_url = page.url

            if report_url.startswith("/"):
                report_url = QE_BASE + report_url
            elif not report_url.startswith("http"):
                report_url = QE_BASE + "/" + report_url

            log.info("QE: report ready → %s", report_url)
            return report_url

        finally:
            browser.close()
