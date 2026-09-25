"""
Outlook forward-with-screenshots automation — Python Playwright port of
Screenshot/outlook.js.

Unlike helpers/itos_automation.py (which drives an on-screen remote-desktop
Edge window via literal mouse/keyboard simulation), this launches its OWN
isolated Chrome instance via Playwright and talks to Outlook Web over the
DOM/CDP — it never touches the physical desktop, so it does not compete
with the ITOS automation for the screen. Its single-concurrency constraint
is different: the persistent browser profile directory (OUTLOOK_PROFILE_DIR)
can only be held open by one process at a time, which is what
helpers/email_worker.py's single dedicated worker thread serializes against.

Uses Playwright's SYNC API (not asyncio) to match this codebase's
synchronous style (helpers/screenshot_worker.py is a plain blocking loop).

Login uses Microsoft's phone-call MFA challenge — genuinely requires a human
to answer a call and press # every ~20 days or whenever the saved session
expires (see "Don't ask again for 20 days" in the login flow below). This is
an accepted, documented limitation, not something this module tries to
route around: if the saved profile's session is no longer valid,
capture-time raises LoginRequiredError with a clear message instead of
hanging or guessing, and it's on a human to re-authenticate once, after
which the saved session carries the automation for another ~20 days.
"""

import base64
import logging
import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

logger = logging.getLogger("outlook_automation")

EMAIL = os.getenv("OUTLOOK_EMAIL")
PASSWORD = os.getenv("OUTLOOK_PASSWORD")
# Defaults point at the EXACT existing folder/binary Screenshot/outlook.js
# already used — that profile already holds a completed-MFA session, and
# `executable_path` (not Playwright's `channel="chrome"` resolution) launches
# the literal same Chrome install the session was authenticated against,
# removing any chance of Playwright silently picking a different Chrome.
PROFILE_DIR = os.getenv("OUTLOOK_PROFILE_DIR", "D:/Axe-Global/Screenshot/outlook_profile")
CHROME_PATH = os.getenv("OUTLOOK_CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
OUTLOOK_URL = "https://outlook.office.com/mail/"
FORWARD_TO = [addr.strip() for addr in os.getenv("OUTLOOK_FORWARD_TO", "").split(",") if addr.strip()]
FORWARD_INTRO_TEXT = os.getenv("OUTLOOK_FORWARD_INTRO_TEXT", "").strip()
LOGIN_WAIT_SECONDS = int(os.getenv("OUTLOOK_LOGIN_WAIT_SECONDS", "90"))
# Testing switch: runs every real step (search, open thread, Forward, fill
# To, paste all 3 screenshots inline) but stops short of clicking Send —
# lets the search/forward/paste mechanics be verified without actually
# emailing anyone. Defaults to OFF (dry run) so nothing sends by accident
# until this is deliberately set to "true" in .env when ready for real use.
SEND_ENABLED = os.getenv("OUTLOOK_SEND_ENABLED", "false").strip().lower() in ("1", "true", "yes")
# While in dry-run mode, how long the composed draft (with images pasted)
# stays on screen before the browser closes it unsent — just long enough
# for a human watching to glance at the result.
DRY_RUN_INSPECT_SECONDS = int(os.getenv("OUTLOOK_DRY_RUN_INSPECT_SECONDS", "15"))


class EmailAutomationError(Exception):
    """Raised on any automation failure, with a human-readable reason so the
    worker can log a real last_error."""


class LoginRequiredError(EmailAutomationError):
    """The saved Outlook session is no longer valid and needs a human to
    manually complete the phone-call MFA challenge on this machine before
    any further email jobs can succeed."""


def _is_login_url(url: str) -> bool:
    return "login.microsoftonline.com" in url or "login.live.com" in url


def _wait_for_outlook_or_raise(page):
    """Gives a human up to LOGIN_WAIT_SECONDS to notice the (visible,
    headless=False) browser window and manually complete MFA if needed,
    before giving up — never blocks the worker indefinitely."""
    deadline = time.time() + LOGIN_WAIT_SECONDS
    while time.time() < deadline:
        if not _is_login_url(page.url):
            return
        time.sleep(2)
    raise LoginRequiredError(
        "Outlook session expired — needs manual re-login (phone MFA) on the server. "
        "Open the Chrome window this automation uses, complete the sign-in, and the "
        "saved session will carry future jobs again."
    )


def _open_update_in_cts_folder(page):
    """Navigates to the CT-SabicOutbound shared mailbox's "UPDATE in CTS"
    subfolder (not Inbox — that's where the reference-number threads this
    automation searches actually live). Ported as-is from outlook.js's
    multi-strategy fallback approach for expanding the shared mailbox; the
    final folder click matches the exact selector confirmed working —
    these are page-structure selectors, not secrets, so they stay as code
    rather than .env config (same reasoning as itos_automation.py's JS
    selectors)."""
    time.sleep(5)

    try:
        folder_pane = page.locator('[role="tree"]').first
        folder_pane.wait_for(state="visible", timeout=15000)
        folder_pane.evaluate("el => el.scrollTop = el.scrollHeight")
        time.sleep(2)
    except Exception:
        pass

    try:
        sabic_root = page.locator('[id="sharedFolderRoot_Sabicoutbound@vanmoer.com"]')
        visible = sabic_root.is_visible()
        if not visible:
            sabic_root = page.locator('[id*="sabicoutbound" i], [id*="Sabicoutbound"]').first
            visible = sabic_root.is_visible()
        if not visible:
            sabic_root = page.locator('[role="treeitem"]').filter(has_text="sabicoutbound").first
            sabic_root.wait_for(state="visible", timeout=15000)

        if sabic_root.get_attribute("aria-expanded") != "true":
            sabic_root.locator("button").first.click()
            time.sleep(3)
    except Exception:
        pass

    try:
        sabic_item = page.get_by_role("treeitem", name="CT-SabicOutbound")
        sabic_item.wait_for(state="visible", timeout=10000)
        if sabic_item.get_attribute("aria-expanded") != "true":
            sabic_item.locator("button").first.click()
            time.sleep(2)
    except Exception:
        pass

    time.sleep(3)
    folder_locator = None
    for attempt in (
        lambda: page.locator('div[role="treeitem"][data-folder-name="update in cts"]').first,
        lambda: page.get_by_role("treeitem", name="UPDATE in CTS", exact=True).first,
        lambda: page.locator('[role="treeitem"]').filter(has_text="UPDATE in CTS").first,
    ):
        try:
            candidate = attempt()
            candidate.wait_for(state="visible", timeout=8000)
            candidate.click()
            folder_locator = candidate
            break
        except Exception:
            continue

    if folder_locator is None:
        raise EmailAutomationError('Could not find the "UPDATE in CTS" folder in the folder pane.')

    time.sleep(3)
    return folder_locator


def _search_and_open_latest(page, reference: str):
    search_box = page.locator("#topSearchInput")
    search_box.wait_for(state="visible", timeout=15000)
    search_box.click()
    time.sleep(0.5)
    search_box.fill("")
    search_box.fill(reference)
    search_box.press("Enter")

    time.sleep(5)

    try:
        all_results = page.locator("#groupHeaderAll\\ results")
        if all_results.get_attribute("aria-expanded") == "false":
            all_results.click()
            time.sleep(2)
    except Exception:
        pass

    clicked = page.evaluate("""
        () => {
            const allResultsHeader = document.getElementById('groupHeaderAll results');
            if (allResultsHeader) {
                const firstAllResult = allResultsHeader.nextElementSibling;
                if (firstAllResult && firstAllResult.getAttribute('role') === 'option') {
                    firstAllResult.click();
                    return 'all';
                }
            }
            const anyResult = document.querySelector('[role="option"].jGG6V');
            if (anyResult) { anyResult.click(); return 'fallback'; }
            return null;
        }
    """)
    if not clicked:
        raise EmailAutomationError(f'No Outlook results found for reference "{reference}".')
    time.sleep(3)


def _click_forward(page):
    time.sleep(2)
    clicked = page.evaluate("""
        () => {
            const btn = document.querySelector('div[role="menuitem"][aria-label="Forward"]');
            if (btn) { btn.click(); return true; }
            return false;
        }
    """)
    if not clicked:
        raise EmailAutomationError("Forward button not found on the opened message.")
    time.sleep(3)


def _fill_recipients(page, recipients: list[str]):
    for addr in recipients:
        filled = page.evaluate("""
            (email) => {
                const to = document.querySelector('div[role="group"] div[contenteditable="true"][aria-label="To"]');
                if (!to) return false;
                to.focus();
                to.textContent = email;
                to.dispatchEvent(new InputEvent('input', { bubbles: true }));
                return true;
            }
        """, addr)
        if not filled:
            raise EmailAutomationError("Could not find the To field on the forward compose window.")
        time.sleep(1.5)
        page.evaluate("""
            () => {
                const to = document.querySelector('div[role="group"] div[contenteditable="true"][aria-label="To"]');
                if (to) {
                    to.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));
                }
            }
        """)
        time.sleep(1.5)


_PASTE_IMAGE_JS = """
async (base64) => {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
        bytes[i] = binary.charCodeAt(i);
    }
    const blob = new Blob([bytes], { type: 'image/png' });
    const item = new ClipboardItem({ 'image/png': blob });
    await navigator.clipboard.write([item]);
}
"""


_MOVE_CARET_TO_BODY_START_JS = """
() => {
    const body = document.querySelector(
        'div[role="textbox"][aria-label="Message body"][contenteditable="true"]'
    );
    if (!body) return false;
    body.focus();
    const range = document.createRange();
    range.setStart(body, 0);
    range.collapse(true);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    return true;
}
"""


def _paste_screenshots_inline(page, screenshot_paths: list[Path]):
    """Pastes each of screenshot_paths inline, in order (01/02/03), all
    landing together ABOVE the forwarded content/signature.

    Deliberately does NOT click the body between pastes: Playwright's
    .click() hits the CENTER of the element's bounding box, which — once
    the body already contains a whole forwarded thread plus its signature
    — can land literally in the middle of that existing content (this is
    what caused images to appear interleaved with signature/address lines
    in testing). Instead, the caret is explicitly placed at the very start
    of the body ONCE via the Selection API before the first paste; after
    that, a normal paste naturally advances the caret to just after what it
    inserted, so each subsequent Ctrl+V lands right after the previous
    image without any further clicking to go wrong.

    NOTE: a single navigator.clipboard.write() call with multiple
    ClipboardItems was tried and does NOT work — Chrome raises
    "NotAllowedError: Support for multiple ClipboardItems is not
    implemented" (confirmed against a real run, not a hypothetical). A
    ClipboardItem can hold multiple *representations* of one clipboard
    entry (e.g. HTML + PNG of the same thing), but the web Clipboard API
    has no way to stage several separate images for one paste — this is a
    hard browser limitation, not something fixable in this code. So this
    writes+pastes one image at a time, back-to-back, still preserving the
    exact given order — the closest equivalent achievable."""
    body = page.locator(
        'div[role="textbox"][aria-label="Message body"][contenteditable="true"]'
    )
    body.wait_for(state="visible", timeout=15000)

    if not page.evaluate(_MOVE_CARET_TO_BODY_START_JS):
        raise EmailAutomationError("Could not position the cursor in the message body.")
    time.sleep(0.3)

    for path in screenshot_paths:
        b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        page.evaluate(_PASTE_IMAGE_JS, b64)
        body.press("Control+V")
        time.sleep(1.5)
        body.press("Enter")  # visual separation between stacked inline images
        time.sleep(0.5)


def _insert_intro_text(page, text: str):
    if not text:
        return
    try:
        page.evaluate("""
            (text) => {
                const body = document.querySelector(
                    'div[role="textbox"][aria-label="Message body"][contenteditable="true"]'
                );
                if (body) {
                    body.focus();
                    const newDiv = document.createElement('div');
                    newDiv.textContent = text;
                    body.insertBefore(newDiv, body.firstChild);
                    body.dispatchEvent(new InputEvent('input', {
                        bubbles: true, inputType: 'insertText', data: text,
                    }));
                }
            }
        """, text)
        time.sleep(1)
    except Exception:
        pass  # non-critical — better to send without the intro line than fail the send


def _click_send(page):
    sent = page.evaluate("""
        () => {
            const btn = document.querySelector('button[aria-label="Send"]');
            if (btn) { btn.click(); return true; }
            return false;
        }
    """)
    if not sent:
        raise EmailAutomationError("Send button not found on the compose window.")
    time.sleep(3)


def send_forwarded_screenshots(reference: str, screenshot_paths: list[Path]) -> None:
    """
    Opens the persistent Outlook profile, confirms the saved session is
    still valid, navigates to CT-SabicOutbound > UPDATE in CTS, searches
    `reference` (the OrderTracking business reference, e.g. a SABIC
    dispatch shipment number — the same value already shown in the tracking
    panel's Reference column), opens the latest ("All results") matching
    thread, forwards it to OUTLOOK_FORWARD_TO with each of
    `screenshot_paths` pasted inline (in the given order) via the
    clipboard-write + Ctrl+V technique, and sends.

    Raises EmailAutomationError (or its LoginRequiredError subtype) with a
    human-readable reason on any failure — never silently no-ops. Not
    called concurrently with itself: only one process may hold
    PROFILE_DIR open at a time (see helpers/email_worker.py).

    When OUTLOOK_SEND_ENABLED is not set to true (the default), every step
    up through pasting the images runs for real, but Send is never clicked
    — the caller (helpers/email_worker.py) still marks the row "sent" for
    testing purposes, since the point of dry-run mode is to verify the
    search/forward/paste mechanics, not to simulate a failure.
    """
    if not FORWARD_TO:
        raise EmailAutomationError("OUTLOOK_FORWARD_TO is not configured — no recipients to send to.")
    if not screenshot_paths:
        raise EmailAutomationError(f"No screenshot files to attach for reference {reference}.")

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            executable_path=CHROME_PATH,
            viewport={"width": 1366, "height": 768},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(OUTLOOK_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(5)

            if _is_login_url(page.url):
                _wait_for_outlook_or_raise(page)

            _open_update_in_cts_folder(page)
            _search_and_open_latest(page, reference)
            _click_forward(page)
            _fill_recipients(page, FORWARD_TO)
            _paste_screenshots_inline(page, screenshot_paths)
            _insert_intro_text(page, FORWARD_INTRO_TEXT)

            if SEND_ENABLED:
                _click_send(page)
            else:
                logger.warning(
                    "OUTLOOK_SEND_ENABLED is off — DRY RUN for reference %s: search/forward/paste "
                    "completed, Send was NOT clicked. Set OUTLOOK_SEND_ENABLED=true in .env for real sends.",
                    reference,
                )
                time.sleep(DRY_RUN_INSPECT_SECONDS)
        finally:
            context.close()
