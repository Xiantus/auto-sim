"""qe_sim.py — QuestionablyEpic Upgrade Finder automation for healer specs."""

import logging
import re
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

log = logging.getLogger(__name__)

QE_BASE        = "https://questionablyepic.com"
QE_UF_URL      = QE_BASE + "/live/upgradefinder"
QE_REPORT_URL  = QE_BASE + "/live/upgradereport/{report_id}"
DEBUG_SHOT     = Path(__file__).parent / "qe_debug.png"

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


def is_healer(spec_id: int) -> bool:
    return int(spec_id) in HEALER_SPEC_IDS


def _js_click(page, locator, timeout: int = 10_000) -> None:
    """Wait for locator to be visible, then click via page.evaluate() with
    a raw element handle — bypasses Playwright pointer-event checks entirely."""
    locator.wait_for(state="visible", timeout=timeout)
    locator.scroll_into_view_if_needed(timeout=timeout)
    handle = locator.element_handle(timeout=timeout)
    page.evaluate("el => el.click()", handle)


def run_qe_upgradefinder(simc: str, timeout_minutes: int = 5) -> str:
    """
    Automate the QE Upgrade Finder with the given SimC string.
    Selects HEROIC and MYTHIC difficulties then clicks GO! to run the simulation
    and returns the shareable report URL.

    Raises RuntimeError if the automation fails.
    """
    timeout_ms = timeout_minutes * 60 * 1000

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx  = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        try:
            # Block cookie/consent scripts before navigating so overlays
            # never load and can't intercept clicks.
            for pattern in ["**/*ncmp*", "**/*privacymanager*", "**/*cookieconsent*",
                             "**/*consent-manager*", "**/*trustarc*", "**/*onetrust*"]:
                page.route(pattern, lambda route: route.abort())

            log.info("QE: navigating to upgrade finder...")
            page.goto(QE_UF_URL, timeout=30_000)
            page.wait_for_load_state("networkidle", timeout=30_000)

            page.screenshot(path=str(DEBUG_SHOT))
            log.info("QE: page loaded (screenshot saved).")

            # ── Open SimC import dialog ──────────────────────────────────────
            opened = False
            for pattern in ["SimC", "Import", "simc", "import"]:
                try:
                    btn = page.get_by_role("button").filter(has_text=re.compile(pattern, re.I)).first
                    _js_click(page, btn, timeout=5_000)
                    page.wait_for_selector('[aria-labelledby="simc-dialog-title"]', timeout=5_000)
                    opened = True
                    log.info("QE: SimC dialog opened via button matching '%s'", pattern)
                    break
                except PWTimeout:
                    continue

            if not opened:
                raise RuntimeError("Could not find or open the SimC import dialog on QE upgrade finder.")

            # ── Fill SimC string ─────────────────────────────────────────────
            textarea = page.locator("#simcentry")
            textarea.wait_for(state="visible", timeout=10_000)
            textarea.fill(simc)
            log.info("QE: SimC string entered.")

            # ── Step 1: Submit the SimC to import character/gear ─────────────
            dialog = page.locator('[role="dialog"]')
            try:
                submit_btn = dialog.get_by_role("button", name=re.compile(r"^submit$", re.I))
                _js_click(page, submit_btn, timeout=10_000)
                log.info("QE: Submit clicked, waiting for page to process...")
                page.wait_for_timeout(2_000)
            except PWTimeout:
                log.warning("QE: Submit button not found, proceeding...")

            # ── Step 2: Close the dialog so we can interact with the main page ─
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            # If Escape didn't close it, click outside the dialog
            if page.locator('[role="dialog"]').count() > 0:
                page.mouse.click(10, 10)
                page.wait_for_timeout(500)
            log.info("QE: dialog closed.")

            page.wait_for_timeout(1_000)
            page.screenshot(path=str(DEBUG_SHOT))

            # Dump button states so we know which are already selected
            btn_info = page.evaluate("""() => {
                return [...document.querySelectorAll('button')].map(b => ({
                    text: b.innerText.trim(),
                    cls: b.className,
                    disabled: b.disabled
                })).filter(b => b.text)
            }""")
            log.info("QE: buttons after dialog close: %s", btn_info)

            # ── Step 3: Ensure HEROIC is selected ────────────────────────────
            heroic_btn = page.locator("button").filter(has_text=re.compile(r"^\s*HEROIC\s*$", re.I)).first
            heroic_cls = heroic_btn.get_attribute("class") or ""
            log.info("QE: HEROIC button class before click: %s", heroic_cls)
            _js_click(page, heroic_btn, timeout=10_000)
            log.info("QE: HEROIC clicked.")
            page.wait_for_timeout(300)
            heroic_cls_after = heroic_btn.get_attribute("class") or ""
            log.info("QE: HEROIC button class after click: %s", heroic_cls_after)

            # ── Step 4: Ensure MYTHIC is selected ────────────────────────────
            mythic_btn = page.locator("button").filter(has_text=re.compile(r"^\s*MYTHIC\s*$", re.I)).first
            mythic_cls = mythic_btn.get_attribute("class") or ""
            log.info("QE: MYTHIC button class before click: %s", mythic_cls)
            _js_click(page, mythic_btn, timeout=10_000)
            log.info("QE: MYTHIC clicked.")
            page.wait_for_timeout(300)
            mythic_cls_after = mythic_btn.get_attribute("class") or ""
            log.info("QE: MYTHIC button class after click: %s", mythic_cls_after)

            page.screenshot(path=str(DEBUG_SHOT))
            log.info("QE: screenshot after difficulty selection saved.")

            # ── Step 5: Click GO! ─────────────────────────────────────────────
            go_btn = page.locator("button").filter(has_text=re.compile(r"^\s*go[!.]?\s*$", re.I)).first
            _js_click(page, go_btn, timeout=10_000)
            log.info("QE: GO! clicked, URL: %s", page.url)
            page.wait_for_timeout(3_000)
            page.screenshot(path=str(DEBUG_SHOT))
            log.info("QE: post-GO screenshot saved, URL: %s", page.url)

            # ── Wait for report URL (works for both full nav and pushState) ──
            log.info("QE: waiting for report URL...")
            page.wait_for_function(
                "window.location.href.includes('upgradereport')",
                timeout=timeout_ms,
            )
            report_url = page.url

            # Normalise to absolute URL
            if report_url.startswith("/"):
                report_url = QE_BASE + report_url
            elif not report_url.startswith("http"):
                report_url = QE_BASE + "/" + report_url

            log.info("QE: report ready → %s", report_url)
            return report_url

        finally:
            browser.close()
