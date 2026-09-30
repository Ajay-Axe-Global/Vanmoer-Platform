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

PER-USER ACCOUNTS: email/password/profile_dir are passed into
send_forwarded_screenshots() per call (resolved by helpers/email_worker.py
from whichever platform user requested the send — see User.outlook_username/
outlook_password/outlook_status in database/models.py), not read as fixed
globals here. Each platform user gets their own persistent Chrome profile
directory under OUTLOOK_PROFILE_BASE_DIR, so their saved Outlook session
(and MFA-verified device trust) is theirs alone — one user's expired session
never blocks another's send, and each user answers their own phone.
"""

import base64
import logging
import os
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

logger = logging.getLogger("outlook_automation")

# Parent directory holding one persistent Chrome profile subfolder per
# platform user (see profile_dir_for_user() below) — the per-user
# equivalent of the old single shared OUTLOOK_PROFILE_DIR.
PROFILE_BASE_DIR = Path(os.getenv("OUTLOOK_PROFILE_BASE_DIR", str(Path(__file__).parent.parent / "outlook_profiles")))
# `executable_path` (not Playwright's `channel="chrome"` resolution) launches
# the literal same Chrome install every saved profile was authenticated
# against, removing any chance of Playwright silently picking a different
# Chrome.
CHROME_PATH = os.getenv("OUTLOOK_CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
OUTLOOK_URL = "https://outlook.office.com/mail/"
FORWARD_TO = [addr.strip() for addr in os.getenv("OUTLOOK_FORWARD_TO", "").split(",") if addr.strip()]
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


def profile_dir_for_user(user_id: int) -> str:
    """Every platform user gets their own persistent Chrome profile
    subfolder, so their Outlook session/MFA device-trust is theirs alone —
    isolated cookie jars, never a shared login. Directory is created lazily
    by Chromium itself on first launch_persistent_context() call, same as
    the old single-profile setup."""
    return str(PROFILE_BASE_DIR / f"user_{user_id}")


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


def _perform_login(page, email: str | None, password: str | None):
    """Automates the parts of the Microsoft sign-in that DON'T need a
    human: picking the remembered account if Outlook offers one, or typing
    this user's own email then password for a fresh sign-in (ported from
    outlook.js's Step A/Step B). The phone-call MFA challenge itself still
    genuinely requires a human and is handled separately by
    _wait_for_outlook_or_raise() — this function only gets the sign-in far
    enough to reach that MFA step (or, if the saved session is still good,
    straight past it)."""
    if not email:
        return

    pick_account_row = page.locator("div.table-row").filter(has_text=email)
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
        email_box.fill(email)
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
    if password:
        try:
            pass_box = page.get_by_role("textbox", name=re.compile(r"Enter the password", re.IGNORECASE))
            pass_box.wait_for(state="visible", timeout=10000)
            pass_box.fill(password)
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
    # The real element here is `<input type="submit" id="idSIButton9"
    # value="Yes">` (confirmed from this exact page's live markup), not a
    # plain <button> — get_by_role("button", name="Yes") alone was matching
    # unreliably (this login page's knockout-bound markup can render more
    # than one role="button"-ish node before the real one settles), leaving
    # the automation stuck on this screen for the full 60s wait_for_url
    # timeout below with no visible error, since the old code swallowed the
    # failure with a bare `except: pass`. #idSIButton9 is Microsoft's own
    # stable element ID for this button across every account (it's their
    # login page, not anything account-specific), so it's tried first; the
    # role-based locator stays as a fallback in case Microsoft ever changes
    # that ID.
    clicked_yes = False
    for attempt in (
        lambda: page.locator("#idSIButton9"),
        lambda: page.locator('input[type="submit"][value="Yes"]'),
        lambda: page.get_by_role("button", name="Yes"),
    ):
        try:
            btn = attempt().first
            btn.wait_for(state="visible", timeout=15000)
            btn.click()
            clicked_yes = True
            break
        except Exception:
            continue
    if clicked_yes:
        logger.warning("Clicked 'Stay signed in → Yes'.")
    else:
        logger.warning("'Stay signed in?' Yes button not found/clicked (may already be past this screen).")
 
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
        logger.warning(
            "MFA attempt %d/%d — waiting for the Call option to render.",
            attempt, MFA_MAX_RETRIES,
        )

        # ── Wait for one of five outcomes (up to 2 minutes) ──────
        #   1. URL leaves login domain → MFA fully done
        #   2. "Sign in another way" link appears → check if success or failure
        #   3. "Let's keep your account secure" (proof-up) page appears →
        #      MFA already succeeded, just not left the login domain yet
        #      (proof-up-redirect-view is still served from
        #      login.microsoftonline.com, so _is_login_url() alone never
        #      catches this — without this check the loop just burns the
        #      full 2-minute timeout doing nothing on this screen)
        #   4. The Call row renders and gets clicked
        #   5. Timeout
        #
        # The Call-row check used to run ONCE at the top of each attempt,
        # before this wait loop started — if Microsoft's knockout-rendered
        # proof list hadn't finished painting at that exact instant (a real,
        # observed race), the click was skipped for the entire attempt and
        # the loop then just sat idle for the full 2 minutes with nothing
        # ever selected. Checking every second inside the loop instead means
        # a late-rendering row still gets clicked as soon as it appears.
        result = None
        clicked_call = False
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
            # Check if we've already landed on the post-MFA "keep your
            # account secure" prompt — that only ever appears after a
            # successful sign-in, so treat it the same as "done".
            try:
                if page.locator("#skipMfaRegistrationLink").is_visible():
                    result = "done"
                    break
            except Exception:
                pass
            # Click the Call option as soon as it's actually visible —
            # retried every iteration (not just once) until it succeeds.
            if not clicked_call:
                try:
                    call_row = page.locator("div.table-row").filter(has_text="Call").first
                    if call_row.is_visible():
                        call_row.click()
                        clicked_call = True
                        logger.warning(
                            "MFA attempt %d/%d — clicked Call option; phone should be ringing, answer and press #.",
                            attempt, MFA_MAX_RETRIES,
                        )
                except Exception:
                    pass
            time.sleep(1)
 
        # ── Outcome 1: URL left login domain, or proof-up page reached ──
        if result == "done":
            logger.warning("MFA complete — left login domain or reached the post-MFA prompts.")
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
    folder_locator = _try_click_update_in_cts(page)
    if folder_locator is not None:
        time.sleep(3)
        return folder_locator

    # Observed failure mode: the shared mailbox's folder drawer can
    # collapse back (or "UPDATE in CTS" just hasn't rendered under
    # CT-SabicOutbound yet) even though CT-SabicOutbound itself shows
    # expanded. Primary recovery: Outlook Web's own "Go to folder" dialog
    # (Ctrl+Y) — it jumps straight to the folder by name regardless of the
    # tree's current expand/collapse/render state, so it doesn't depend on
    # the tree structure at all (unlike clicking through it). Only if that
    # doesn't work do we fall back to the more fragile arrow-click, which
    # depends on the tree already being close to right.
    logger.warning('"UPDATE in CTS" not found on first try — trying "Go to folder" (Ctrl+Y).')
    if _try_go_to_folder(page, "UPDATE in CTS"):
        time.sleep(3)
        # "Go to folder" navigates the mailbox view directly; it doesn't
        # click/select a tree-item locator the way the direct approach
        # does, and nothing downstream uses the returned locator (see
        # send_forwarded_screenshots) — just confirm arrival is enough.
        return None

    logger.warning('"Go to folder" recovery did not work either — trying the folder arrow once, then retrying.')
    _try_click_folder_arrow(page)
    time.sleep(2)
    folder_locator = _try_click_update_in_cts(page)
    if folder_locator is not None:
        time.sleep(3)
        return folder_locator

    raise EmailAutomationError('Could not find the "UPDATE in CTS" folder in the folder pane.')


_GO_TO_FOLDER_JS = """
(folderName) => {
    const input = document.querySelector('[role="dialog"] input[placeholder="Type a folder name"]');
    if (!input) return false;
    const setter = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype, "value"
    ).set;
    setter.call(input, folderName);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.focus();
    input.dispatchEvent(new KeyboardEvent("keydown", {
        key: "Enter", code: "Enter", bubbles: true, cancelable: true
    }));
    return true;
}
"""


def _try_go_to_folder(page, folder_name: str) -> bool:
    """Opens Outlook Web's "Go to folder" dialog with Ctrl+Y and types +
    Enters `folder_name` to jump straight there — independent of the
    folder tree's current expand/collapse/render state entirely (unlike
    clicking through CT-SabicOutbound > UPDATE in CTS in the tree), so
    it's a more reliable recovery than the arrow-click when the tree
    itself is the problem. Uses the native HTMLInputElement value setter
    (not `.fill()`/typing) for the same reason _set_subject does — this
    dialog's input is a React-controlled Fluent UI SearchBox, so plain DOM
    assignment leaves React's internal value out of sync with what
    actually gets submitted on Enter."""
    try:
        page.keyboard.press("Control+y")
    except Exception:
        return False

    deadline = time.time() + 5
    dialog_seen = False
    while time.time() < deadline:
        try:
            if page.locator('[role="dialog"] input[placeholder="Type a folder name"]').is_visible():
                dialog_seen = True
                break
        except Exception:
            pass
        time.sleep(0.3)

    if not dialog_seen:
        logger.warning('"Go to folder" dialog (Ctrl+Y) did not open.')
        return False

    ok = False
    try:
        ok = bool(page.evaluate(_GO_TO_FOLDER_JS, folder_name))
    except Exception:
        pass

    if ok:
        logger.warning('Used "Go to folder" (Ctrl+Y) to jump to "%s".', folder_name)
        time.sleep(2)
    else:
        logger.warning('"Go to folder" dialog opened but could not fill/submit the folder name.')
    return ok


def _try_click_update_in_cts(page):
    """Multi-strategy attempt at finding+clicking "UPDATE in CTS" — returns
    the clicked locator, or None without raising (caller decides what to do
    next, e.g. the Inbox-first retry in _open_update_in_cts_folder)."""
    for attempt in (
        lambda: page.locator('div[role="treeitem"][data-folder-name="update in cts"]').first,
        lambda: page.get_by_role("treeitem", name="UPDATE in CTS", exact=True).first,
        lambda: page.locator('[role="treeitem"]').filter(has_text="UPDATE in CTS").first,
    ):
        try:
            candidate = attempt()
            candidate.wait_for(state="visible", timeout=8000)
            candidate.click()
            return candidate
        except Exception:
            continue
    return None


_CLICK_FOLDER_ARROW_JS = """
() => {
    const arrow = document.querySelector('.ppZg6 button');
    if (arrow) { arrow.click(); return true; }
    return false;
}
"""


def _try_click_folder_arrow(page) -> bool:
    """Clicks the CT-SabicOutbound drawer's expand/collapse arrow — a
    one-shot recovery step before retrying the "UPDATE in CTS" click when
    it wasn't found the first time (the drawer can collapse back or the
    subfolder can be slow to render).

    Primary selector is `.ppZg6 button` — this arrow's class in the
    current Outlook Web build. That's a Fluent UI/CSS-in-JS HASHED class,
    not a stable one, so it isn't guaranteed to survive a future Outlook
    Web rollout the way outlook.js's other page-structure selectors (IDs,
    aria-labels) are — Microsoft can silently ship a rebuild with a
    different hash for every user at once. If it doesn't match, falls back
    to the CT-SabicOutbound tree item's own expand button, found by its
    accessible name (role=treeitem, name="CT-SabicOutbound") instead of any
    class — the same semantic locator already used earlier in
    _open_update_in_cts_folder — which is what this recovery step is
    actually trying to toggle anyway."""
    clicked = False
    try:
        clicked = bool(page.evaluate(_CLICK_FOLDER_ARROW_JS))
    except Exception:
        pass

    if not clicked:
        try:
            sabic_item = page.get_by_role("treeitem", name="CT-SabicOutbound")
            sabic_item.wait_for(state="visible", timeout=5000)
            sabic_item.locator("button").first.click()
            clicked = True
        except Exception:
            pass

    if clicked:
        logger.warning("Clicked the folder arrow as a recovery step before retrying UPDATE in CTS.")
    else:
        logger.warning("Could not click the folder arrow during UPDATE in CTS recovery.")
    return clicked


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


_CLICK_FORWARD_JS = """
() => {
    const btn = document.querySelector('div[role="menuitem"][aria-label="Forward"]')
        || document.querySelector('button[aria-label="Forward"]');
    if (btn) { btn.click(); return true; }
    return false;
}
"""


def _compose_is_open(page) -> bool:
    """The Subject field only exists once a Forward compose pane has
    actually opened — used as the signal that the Shift+F shortcut below
    worked, since key presses give no direct success/failure feedback the
    way a button click's return value does."""
    try:
        return page.locator('input[aria-label="Subject"]').is_visible()
    except Exception:
        return False


def _click_forward(page, timeout_seconds: int = 15):
    """Primary: Outlook Web's Shift+F keyboard shortcut for Forward —
    faster and skips the toolbar-rendering race entirely, since it doesn't
    depend on the Forward button having painted yet. Falls back to polling
    for and clicking the button (menuitem or overflow-menu variant) if the
    shortcut doesn't visibly open a compose pane within a few seconds —
    covers both "shortcut didn't fire because reading pane wasn't focused
    yet" and any future case where Outlook changes/disables the shortcut.
    The polling-not-one-shot approach itself guards the same race
    documented in _wait_for_outlook_or_raise's docstring for the MFA Call
    row: a fixed sleep + single check can fire before the toolbar has
    rendered."""
    try:
        page.keyboard.press("Shift+F")
    except Exception:
        pass
    deadline = time.time() + 5
    while time.time() < deadline:
        if _compose_is_open(page):
            time.sleep(3)
            return
        time.sleep(0.3)

    logger.warning("Shift+F didn't open a Forward compose pane — falling back to clicking the Forward button.")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if page.evaluate(_CLICK_FORWARD_JS):
            time.sleep(3)
            return
        time.sleep(0.5)
    raise EmailAutomationError("Forward button not found on the opened message (keyboard shortcut and button both failed).")


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


_SET_SUBJECT_JS = """
(newSubject) => {
    const subject = document.querySelector('input[aria-label="Subject"]');
    if (!subject) return false;
    const setter = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype, "value"
    ).set;
    setter.call(subject, newSubject);
    subject.dispatchEvent(new Event("input", { bubbles: true }));
    subject.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
}
"""


def _set_subject(page, reference: str, itos_number: str | None):
    """Replaces the compose window's existing subject (Outlook pre-fills
    "Fw: <original subject>" on Forward) with the SABIC dispatch-request
    line. Uses the native HTMLInputElement value setter (not `.fill()`)
    because this is a React-controlled input (Fluent UI) — plain DOM
    assignment or Playwright's fill() leaves React's internal value out of
    sync with what's displayed/submitted, which is exactly why the setter
    + dispatchEvent('input'/'change') combo is required here, same as any
    other React-controlled field in this app's own automation elsewhere.

    `itos_number` is appended as " / VMR..." when known; omitted (not a
    blank " / ") when the row has no detected ITOS number yet, so the
    subject never ships with a trailing " / " for those rows."""
    subject_field = page.locator('input[aria-label="Subject"]')
    subject_field.wait_for(state="visible", timeout=10000)

    new_subject = f"SABIC OUTBOUND Please arrange dispatch for shipment: {reference}"
    if itos_number:
        new_subject += f" / {itos_number}"

    if not page.evaluate(_SET_SUBJECT_JS, new_subject):
        raise EmailAutomationError("Could not find the Subject field on the forward compose window.")
    time.sleep(0.5)


_INSERT_LINES_JS = """
(lines) => {
    const body = document.querySelector(
        'div[role="textbox"][aria-label="Message body"][contenteditable="true"]'
    );
    if (!body) return false;
    body.focus();
    const frag = document.createDocumentFragment();
    for (const line of lines) {
        const div = document.createElement('div');
        if (line === '') {
            div.innerHTML = '<br>';
        } else {
            div.textContent = line;
        }
        frag.appendChild(div);
    }
    body.insertBefore(frag, body.firstChild);
    body.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText' }));
    return true;
}
"""


def _insert_order_intro(page, reference: str):
    """Inserts the "Hi, / / This order was completed <reference>." lines as
    the very first thing in the body — called AFTER _paste_screenshots_inline
    on purpose: that function's pastes also land at the body's start (see its
    own docstring), so inserting this intro afterwards, at body.firstChild,
    pushes it above the images rather than the images pushing it down.
    Net visual order ends up: intro text, then the 3 images, then the
    forwarded thread — matching the requested layout without needing to
    touch _paste_screenshots_inline's own caret logic.

    Non-critical: swallows its own failure (logged) rather than failing the
    whole send, same reasoning as the intro text this replaces — better to
    send without the greeting line than to fail an otherwise-successful
    forward over it."""
    try:
        lines = ["Hi,", "", f"This order was completed {reference}."]
        if not page.evaluate(_INSERT_LINES_JS, lines):
            logger.warning("Could not find the message body to insert the intro text.")
        time.sleep(1)
    except Exception as e:
        logger.warning("Failed to insert order intro text: %s", e)


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


def send_forwarded_screenshots(reference: str, screenshot_paths: list[Path],
                                *, user_id: int, email: str | None, password: str | None,
                                itos_number: str | None = None) -> None:
    """
    Opens THIS USER's own persistent Outlook profile (profile_dir_for_user
    (user_id)), confirms the saved session is still valid, navigates to
    CT-SabicOutbound > UPDATE in CTS, searches `reference` (the
    OrderTracking business reference, e.g. a SABIC dispatch shipment
    number — the same value already shown in the tracking panel's
    Reference column), opens the latest ("All results") matching thread,
    forwards it to OUTLOOK_FORWARD_TO. Before attaching anything, replaces
    Outlook's auto-filled "Fw: ..." subject with "SABIC OUTBOUND Please
    arrange dispatch for shipment: {reference}[/ {itos_number}]" (see
    _set_subject), then pastes each of `screenshot_paths` inline (in the
    given order) via the clipboard-write + Ctrl+V technique, with a fixed
    "Hi, / This order was completed {reference}." intro line placed above
    the images (see _insert_order_intro), and sends.

    `itos_number` is OrderTracking.itos_number for this row (may be None if
    not yet detected) — used only in the subject line.

    `email`/`password` are this user's own Outlook credentials (decrypted
    by the caller from User.outlook_password just before this call, never
    persisted here) — only used if the saved session under this user's
    profile dir has expired and a fresh login is needed; day-to-day runs
    with a still-valid session never touch them.

    Raises EmailAutomationError (or its LoginRequiredError subtype) with a
    human-readable reason on any failure — never silently no-ops. Safe to
    run concurrently for DIFFERENT user_ids (separate profile dirs, no
    shared lock); helpers/email_worker.py still processes jobs one at a
    time regardless, since a handful of users doesn't need real
    concurrency here.

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
    if not email:
        raise EmailAutomationError(
            "This user has no Outlook account connected — set an Outlook username/password "
            "for them in Admin > Users before approving a send."
        )

    profile_dir = profile_dir_for_user(user_id)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            profile_dir,
            headless=False,
            executable_path=CHROME_PATH,
            viewport={"width": 1366, "height": 768},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(OUTLOOK_URL, wait_until="domcontentloaded", timeout=60000)

            if _needs_login(page):
                _perform_login(page, email, password)
                _wait_for_outlook_or_raise(page)

            _open_update_in_cts_folder(page)
            _search_and_open_latest(page, reference)
            _click_forward(page)
            _fill_recipients(page, FORWARD_TO)
            _set_subject(page, reference, itos_number)
            _paste_screenshots_inline(page, screenshot_paths)
            _insert_order_intro(page, reference)

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
