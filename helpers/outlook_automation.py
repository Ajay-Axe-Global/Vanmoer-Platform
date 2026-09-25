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
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

logger = logging.getLogger("outlook_automation")

EMAIL = os.getenv("OUTLOOK_EMAIL")
PASSWORD = os.getenv("OUTLOOK_PASSWORD")
# Lives inside this project's own source directory (a top-level runtime-data
# folder, same pattern as helpers/screenshot_queue.py's SCREENSHOTS_DIR) —
# a one-time copy of the already-authenticated profile Screenshot/outlook.js
# originally used, so the completed-MFA session carries over without living
# outside the Vanmoer-Platform tree. `executable_path` (not Playwright's
# `channel="chrome"` resolution) launches the literal same Chrome install
# that session was authenticated against, removing any chance of Playwright
# silently picking a different Chrome.
PROFILE_DIR = os.getenv("OUTLOOK_PROFILE_DIR", str(Path(__file__).parent.parent / "outlook_profile"))
CHROME_PATH = os.getenv("OUTLOOK_CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
OUTLOOK_URL = "https://outlook.office.com/mail/"
FORWARD_TO = [addr.strip() for addr in os.getenv("OUTLOOK_FORWARD_TO", "").split(",") if addr.strip()]
FORWARD_INTRO_TEXT = os.getenv("OUTLOOK_FORWARD_INTRO_TEXT", "").strip()
MFA_MAX_RETRIES = int(os.getenv("OUTLOOK_MFA_MAX_RETRIES", "3"))
# Testing switch: runs every real step (search, open thread, Forward, fill
# To, paste all 3 screenshots inline) but stops short of clicking Send —
# lets the search/forward/paste mechanics be verified without actually
# emailing anyone. Defaults to OFF (dry run) so nothing sends by accident
# until this is deliberately set to "true" in .env when ready for real use.
SEND_ENABLED = os.getenv("OUTLOOK_SEND_ENABLED", "false").strip().lower() in ("1", "true", "yes")


class EmailAutomationError(Exception):
    """Raised on any automation failure, with a human-readable reason so the
    worker can log a real last_error."""


class LoginRequiredError(EmailAutomationError):
    """The saved Outlook session is no longer valid and needs a human to
    manually complete the phone-call MFA challenge on this machine before
    any further email jobs can succeed."""


def _is_login_url(url: str) -> bool:
    return "login.microsoftonline.com" in url or "login.live.com" in url


def _needs_login(page, timeout_seconds: int = 25) -> bool:
    """After navigating to OUTLOOK_URL, Microsoft's OAuth redirect to the
    login domain can take anywhere from under a second to 15+ seconds
    depending on network/server conditions (confirmed by direct
    measurement — not consistent run to run). A fixed short sleep before a
    single page.url check is unreliable and can miss the redirect
    entirely, silently skipping login-handling altogether — which is
    exactly what caused "it opens Outlook, asks for a username, but never
    types anything": the code checked page.url too early, saw the old
    outlook.office.com URL, and concluded no login was needed.

    Polls until EITHER the login domain is reached OR the mailbox itself
    has clearly loaded (the search box appears) — whichever happens first
    — instead of guessing a fixed wait."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _is_login_url(page.url):
            return True
        try:
            if page.locator("#topSearchInput").is_visible():
                return False
        except Exception:
            pass
        time.sleep(1)
    return _is_login_url(page.url)


def _perform_login(page):
    """Automates the parts of the Microsoft sign-in that DON'T need a
    human: picking the remembered account if Outlook offers one, or typing
    OUTLOOK_EMAIL then OUTLOOK_PASSWORD for a fresh sign-in (ported from
    outlook.js's Step A/Step B). The phone-call MFA challenge itself still
    genuinely requires a human and is handled separately by
    _wait_for_outlook_or_raise() — this function only gets the sign-in far
    enough to reach that MFA step (or, if the saved session is still good,
    straight past it)."""
    if not EMAIL:
        return

    pick_account_row = page.locator("div.table-row").filter(has_text=EMAIL)
    email_box = page.get_by_role("textbox", name="Enter your email, phone, or")

    winner = None
    deadline = time.time() + 20
    while time.time() < deadline and winner is None:
        try:
            if pick_account_row.is_visible():
                winner = "pick"
                break
        except Exception:
            pass
        try:
            if email_box.is_visible():
                winner = "email"
                break
        except Exception:
            pass
        time.sleep(0.5)

    used_pick_account = False
    if winner == "pick":
        pick_account_row.click()
        used_pick_account = True
        time.sleep(4)
    elif winner == "email":
        email_box.fill(EMAIL)
        email_box.press("Enter")
        time.sleep(4)
    # else: login page not detected in time — fall through, the MFA wait
    # below will judge whether we actually got past this or need to raise.

    # Always attempted, even after picking a remembered account — Microsoft
    # can still re-prompt for the password after account-pick if that
    # account's token has expired, so skipping this step whenever
    # used_pick_account is True (the previous behavior) could leave a
    # password prompt untouched. get_by_role("textbox", name=...) is the
    # exact same selector outlook.js uses — verified live against the real
    # login page (not a fake/nonexistent one) that it DOES match here, with
    # count=1 and visible=True; an earlier claim that ARIA role="textbox"
    # can never match a password input was wrong, based on an untested
    # assumption from the ARIA spec rather than this actual page's behavior.
    if PASSWORD:
        try:
            pass_box = page.get_by_role("textbox", name=re.compile(r"Enter the password", re.IGNORECASE))
            pass_box.wait_for(state="visible", timeout=10000)
            pass_box.fill(PASSWORD)
            logger.warning("Password field filled — clicking Sign in.")
            page.get_by_role("button", name="Sign in").click()
            time.sleep(4)
        except Exception as e:
            # Previously a silent `pass` here — the exact reason this step
            # failed was never visible anywhere, making every past failure
            # unguessable without live diagnostics. Now it's a real log line.
            logger.warning("Password step skipped (not shown or already past): %s", e)
def _dismiss_post_login_prompts(page):
    """Handles all post-MFA screens in order:
      1. "Secure your account" → click "Not now"
      2. "Stay signed in?" → click "Yes"
      3. Wait for Outlook mail to fully load
    """
    # ── Step 1: "Secure your account" → click "Not now" ──────
    try:
        not_now = page.locator("#skipMfaRegistrationLink")
        not_now.wait_for(state="visible", timeout=10000)
        not_now.click()
        logger.warning("Clicked 'Not now' (skip secure account).")
        time.sleep(3)
    except Exception:
        pass  # page didn't appear — that's fine
 
    # ── Step 2: "Stay signed in?" → click "Yes" ─────────────
    try:
        stay_yes = page.get_by_role("button", name="Yes")
        stay_yes.wait_for(state="visible", timeout=15000)
        stay_yes.click()
        logger.warning("Clicked 'Stay signed in → Yes'.")
    except Exception:
        pass  # already past it
 
    # ── Step 3: Wait for Outlook mail to fully load ──────────
    try:
        page.wait_for_url(
            re.compile(r"outlook\.office\.com/mail", re.IGNORECASE),
            timeout=60000,
        )
    except Exception:
        pass
    time.sleep(8)
    logger.warning("Post-login prompts handled, Outlook should be loaded.")


def _click_call_option(page) -> bool:
    """Selects a phone-call verification method on the "Verify your
    identity" screen — ported from outlook.js's clickCallOption(). Without
    this, nothing ever triggers the call at all (confirmed: the automation
    was previously just staring at this screen indefinitely with no
    verification method ever selected). Prefers a "Call" row whose masked
    number ends in "11" (matching the
    verification method already used for this account); falls back to
    whichever "Call" row appears first otherwise."""
    try:
        page.wait_for_selector("div.table-row", timeout=30000)
    except Exception:
        return False
    time.sleep(2)

    rows = page.locator("div.table-row")
    try:
        count = rows.count()
    except Exception:
        count = 0
    for i in range(count):
        try:
            text = rows.nth(i).inner_text()
        except Exception:
            continue
        if "Call" in text and "11" in text:
            rows.nth(i).click()
            return True

    try:
        page.locator("div.table-row").filter(has_text="Call").first.click()
        return True
    except Exception:
        return False


def _wait_for_outlook_or_raise(page):
    """Handles the phone-call MFA challenge end to end:
 
      1. Detects "Verify your identity" page → clicks Call option
      2. Waits for the call to be answered (up to 2 min per attempt)
      3. If call FAILED (red error text visible):
           → clicks "Sign in another way"
           → back to "Verify your identity"
           → retries up to MFA_MAX_RETRIES times
      4. If call SUCCEEDED (no red error text):
           → checks "Don't ask again for 20 days"
           → clicks Verify/Yes button
           → runs _dismiss_post_login_prompts()
      5. If URL leaves login domain at any point → done
 
    The key fix: the "Don't ask again for 20 days" checkbox and the
    "Sign in another way" link both appear on the SUCCESS page AND the
    FAILURE page. The ONLY reliable way to tell them apart is the red
    error text: "We called your phone but didn't receive the expected
    response." Previous code checked checkbox visibility, which was
    always True, so it always assumed success and never retried.
    """
    for attempt in range(1, MFA_MAX_RETRIES + 1):
        # ── Check if we're on the "Verify your identity" page ────
        on_verify_page = False
        try:
            on_verify_page = page.locator("div.table-row").count() > 0
        except Exception:
            pass
        if not on_verify_page:
            try:
                on_verify_page = page.get_by_text("Verify your identity").is_visible()
            except Exception:
                pass
 
        # ── Click the Call option if on the verify page ──────────
        if on_verify_page:
            if _click_call_option(page):
                logger.warning("MFA attempt %d/%d — clicked Call option.", attempt, MFA_MAX_RETRIES)
            else:
                logger.warning("MFA attempt %d/%d — could not click Call.", attempt, MFA_MAX_RETRIES)
 
        logger.warning(
            "MFA attempt %d/%d — phone should be ringing; answer and press #.",
            attempt, MFA_MAX_RETRIES,
        )
 
        # ── Wait for one of three outcomes (up to 2 minutes) ─────
        #   1. URL leaves login domain → MFA fully done
        #   2. "Sign in another way" link appears → check if success or failure
        #   3. Timeout
        result = None
        deadline = time.time() + 120
        while time.time() < deadline:
            # Check if we've left the login domain entirely
            if not _is_login_url(page.url):
                result = "done"
                break
            # Check if "Sign in another way" appeared
            try:
                if page.locator("#signInAnotherWay").is_visible():
                    result = "check_page"
                    break
            except Exception:
                pass
            time.sleep(1)
 
        # ── Outcome 1: URL left login domain → fully done ────────
        if result == "done":
            logger.warning("MFA complete — URL left login domain.")
            _dismiss_post_login_prompts(page)
            return
 
        # ── Outcome 2: "Sign in another way" visible → inspect ───
        if result == "check_page":
            # Give the page a moment to settle (error text renders
            # slightly after the link appears)
            time.sleep(2)
 
            # Check for the RED ERROR TEXT — this is the ONLY reliable
            # way to distinguish failure from success on this page
            error_visible = False
            try:
                error_visible = page.get_by_text(
                    re.compile(r"didn't receive the expected response", re.IGNORECASE)
                ).is_visible()
            except Exception:
                pass
 
            if error_visible:
                # ── CALL FAILED — red error text present ─────────
                logger.warning(
                    "MFA attempt %d/%d FAILED — phone not answered or wrong response.",
                    attempt, MFA_MAX_RETRIES,
                )
                if attempt < MFA_MAX_RETRIES:
                    # Click "Sign in another way" → back to verify page → retry
                    try:
                        page.locator("#signInAnotherWay").click()
                        logger.warning("Clicked 'Sign in another way' — will retry.")
                        time.sleep(3)
                    except Exception:
                        logger.warning("Could not click 'Sign in another way'.")
                    continue  # loop back → will click Call again
 
                # All retries exhausted — fall through to raise below
 
            else:
                # ── CALL SUCCEEDED — no error text ───────────────
                logger.warning("MFA attempt %d/%d SUCCEEDED — call was approved.", attempt, MFA_MAX_RETRIES)
 
                # Check "Don't ask again for 20 days"
                try:
                    cb = page.get_by_role("checkbox", name="Don't ask again for 20 days")
                    if cb.is_visible():
                        cb.check()
                        logger.warning("Checked 'Don't ask again for 20 days'.")
                except Exception:
                    try:
                        page.locator("#idLbl_SAOTCC_TD_Cb").click()
                        logger.warning("Checked 'Don't ask again' via label click.")
                    except Exception:
                        pass
 
                time.sleep(3)
 
                # Click Yes / Verify button if present
                try:
                    verify_btn = page.locator(
                        'input[type="submit"][value="Yes"], '
                        'input[type="submit"][value="Verify"]'
                    ).first
                    verify_btn.wait_for(state="visible", timeout=5000)
                    verify_btn.click()
                    logger.warning("Clicked Verify/Yes on MFA page.")
                except Exception:
                    pass  # might auto-proceed
 
                # Handle "Not now" + "Stay signed in" + wait for Outlook
                _dismiss_post_login_prompts(page)
                return
 
        # ── Outcome 3: Timeout — neither signal detected ─────────
        if result is None:
            logger.warning("MFA attempt %d/%d timed out (2 min).", attempt, MFA_MAX_RETRIES)
            if attempt < MFA_MAX_RETRIES:
                # Try clicking "Sign in another way" in case the page loaded
                # but we missed it
                try:
                    if page.locator("#signInAnotherWay").is_visible():
                        page.locator("#signInAnotherWay").click()
                        time.sleep(3)
                except Exception:
                    pass
                continue
 
    # ── All retries exhausted ────────────────────────────────
    raise LoginRequiredError(
        "Outlook MFA (phone call) was never answered/succeeded after "
        f"{MFA_MAX_RETRIES} retries — needs manual re-login on the server. "
        "Open the Chrome window this automation uses, complete the sign-in, "
        "and the saved session will carry future jobs again."
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


_CLICK_FIRST_RESULT_JS = """
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
"""


def _click_first_result(page) -> bool:
    """Clicks the same first "All results" search-result item — used both
    to open the conversation initially and, later, to click it again as a
    way of navigating AWAY from an in-progress Forward compose without
    sending it (see _leave_compose_saving_draft())."""
    return bool(page.evaluate(_CLICK_FIRST_RESULT_JS))


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

    if not _click_first_result(page):
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


def _save_draft_and_confirm(page, timeout_seconds: int = 15) -> bool:
    """Explicitly triggers Outlook Web's "Save draft" (Ctrl+S) — confirmed
    against a real run to show a "Draft saved at HH:MM" indicator near the
    compose header once it completes. Waiting for that indicator (instead
    of a blind sleep before the browser is killed) is what actually
    guarantees the draft — with its To field and pasted images — persists:
    abruptly closing the browser (context.close()) otherwise skips
    Outlook's own save entirely, since the page's JS never gets a chance to
    run anything once the whole browser is torn down mid-session. Returns
    whether the indicator was actually seen (best-effort — the exact
    wording can vary by Outlook version/locale)."""
    page.keyboard.press("Control+s")
    try:
        page.get_by_text(re.compile("draft saved", re.IGNORECASE)).first.wait_for(
            state="visible", timeout=timeout_seconds * 1000
        )
        return True
    except Exception:
        return False


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

            if _needs_login(page):
                _perform_login(page)
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
                time.sleep(5)
                saved = _save_draft_and_confirm(page)
                logger.warning(
                    "OUTLOOK_SEND_ENABLED is off — DRY RUN for reference %s: search/forward/paste "
                    "completed, Send was NOT clicked. Draft save %s. Set OUTLOOK_SEND_ENABLED=true "
                    "in .env for real sends.",
                    reference, "confirmed" if saved else "was attempted but not confirmed",
                )
        finally:
            context.close()
