"""
iTOS order screenshot automation — ported from Screenshot/prototype.py.

This drives an actual ON-SCREEN Microsoft Edge window inside a Windows 365
remote-desktop session via pywinauto/pyautogui/pyperclip: real mouse clicks,
keyboard shortcuts, and clipboard pastes against the physical desktop — NOT
a headless browser. That means only one call into this module can ever be
in flight on a given machine at a time (two concurrent runs would fight over
the same mouse cursor, keyboard focus, and clipboard). helpers/screenshot_
worker.py enforces that with a single background worker thread; this module
itself has no concurrency logic and assumes it's already safe to take over
the screen when called.

Deliberately has no Flask/SQLAlchemy imports — pure automation, independently
reusable and testable (e.g. from a standalone debug script) without needing
the web app running.
"""

import ctypes

import sys
# MUST be before any other imports that touch the display
if sys.platform == "win32":
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
import logging
import os
import re
import subprocess

import time
from pathlib import Path

from pywinauto import Application, Desktop
import pyautogui
import pyperclip

TARGET_URL = os.getenv("ITOS_TARGET_URL", "https://itos.vanmoer.com")
INDEX_URL = os.getenv("ITOS_INDEX_URL", "https://itos.vanmoer.com/Stevedoring/Order/Index")
APP_ID = os.getenv("ITOS_APP_ID", "MicrosoftCorporationII.Windows365_8wekyb3d8bbwe!Windows365")
# Screen coordinates are inherently machine/resolution-specific — configurable
# via .env rather than hardcoded so moving this to another PC or monitor
# layout never requires a code change.
APPS_TAB_X = int(os.getenv("ITOS_APPS_TAB_X", "301"))
APPS_TAB_Y = int(os.getenv("ITOS_APPS_TAB_Y", "347"))
CARD_X = int(os.getenv("ITOS_CARD_X", "410"))
CARD_Y = int(os.getenv("ITOS_CARD_Y", "457"))
MAX_EDGE_LAUNCH_RETRIES = int(os.getenv("ITOS_MAX_LAUNCH_RETRIES", "2"))
# Seconds to wait after navigating before touching the page. The date-filter
# script polls for jQuery itself, so this only needs to cover the bare load.
PAGE_LOAD_WAIT = float(os.getenv("ITOS_PAGE_LOAD_WAIT", "3.5"))
EDGE_SCAN_ATTEMPTS = int(os.getenv("ITOS_EDGE_SCAN_ATTEMPTS", "4"))

logger = logging.getLogger("itos_automation")
DATE_FROM_YEARS_BACK = int(os.getenv("ITOS_DATE_FROM_YEARS_BACK", "5"))


# def _declare_dpi_awareness():
#     if sys.platform != "win32":
#         return
#     try:
#         ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
#         print("DPI: per-monitor-v2 ✓")
#         return
#     except Exception as e:
#         print(f"DPI v2 failed: {e}")
#     try:
#         ctypes.windll.shcore.SetProcessDpiAwareness(2)
#         print("DPI: per-monitor ✓")
#         return
#     except Exception as e:
#         print(f"DPI per-monitor failed: {e}")
#     try:
#         ctypes.windll.user32.SetProcessDPIAware()
#         print("DPI: system-aware ✓")
#     except Exception as e:
#         print(f"DPI all methods failed: {e}")





# Per-client iTOS tab clicks. shot 1: tab opened on the order screen before
# the overview shot; shot 2: tab opened after Order Items > first item tile.
# Both are matched by visible tab text. Shot 3 (Stock Info > Transport) is
# the same for every client. Sabic: Attachments / Addresses; Vinmar:
# Addresses / References.
CLIENT_TABS = {
    "sabic": ("Attachments", "Addresses"),
    "vinmar": ("Addresses", "References"),
}


class ScreenshotAutomationError(Exception):
    """Raised on any automation failure, with a human-readable reason so the
    worker can log a real last_error and decide whether to retry."""


# ═══════════════════════════════════════════════════════════
# EDGE WINDOW MANAGEMENT
# ═══════════════════════════════════════════════════════════

def _is_edge_title(title):
    return (
        title
        and "Microsoft" in title
        and "Edge" in title
        and "Van Moer Production - 1 Workspace" != title
    )


def _find_edge_wrapper():
    """UIA calls against a remote/RDP-rendered window (which is what the
    real Edge window is here, inside Windows App) can throw a transient COM
    error on ANY window in the enumeration, not just Edge's own. The
    try/except used to wrap the whole loop, so one flaky window hit before
    Edge in enumeration order silently aborted the entire scan and reported
    "no Edge" even when a perfectly valid Edge window was sitting right
    there untouched — a false negative that then sent the caller down the
    launch-fresh path (and, on a subsequent failed retry, force-killed the
    already-working session — see _ensure_edge_ready()). Catching per-window
    instead means one bad .window_text() call is skipped, not fatal to the
    whole search."""
    # Retried (not one-shot): a single empty/failed UIA enumeration used to be
    # read as "no Edge open" and sent the caller down the launch-fresh path,
    # opening a second Edge via Windows App. Every miss is now logged with the
    # exception or the titles actually seen, so a false negative is diagnosable
    # from the log alone.
    last_titles = []
    for attempt in range(1, EDGE_SCAN_ATTEMPTS + 1):
        last_titles = []
        try:
            windows = Desktop(backend="uia").windows()
        except Exception as e:
            logger.warning("Edge scan %d/%d: Desktop().windows() failed: %r",
                           attempt, EDGE_SCAN_ATTEMPTS, e)
            time.sleep(1)
            continue
        for w in windows:
            try:
                title = w.window_text()
                last_titles.append(title)
                if _is_edge_title(title):
                    return w
            except Exception:
                continue
        time.sleep(1)
    logger.warning("Edge not found after %d scans. Window titles seen: %s",
                   EDGE_SCAN_ATTEMPTS, last_titles)
    return None


def _check_edge_alive():
    return _find_edge_wrapper() is not None


def _focus_edge(w=None):
    if w is None:
        w = _find_edge_wrapper()
    if w is None:
        return False

    try:
        w.set_focus()
        time.sleep(0.5)
        return True
    except Exception:
        pass

    try:
        title = w.window_text()
        app = Application(backend="uia").connect(title=title, timeout=5)
        app.window(title=title).set_focus()
        time.sleep(0.5)
        return True
    except Exception:
        pass

    try:
        pyautogui.hotkey('alt', 'tab')
        time.sleep(1)
    except Exception:
        pass

    return True  # Edge exists even if focus imperfect


def _ensure_edge_focused():
    """MUST call before any pyautogui keystrokes. Returns True if Edge is alive."""
    w = _find_edge_wrapper()
    if w is None:
        return False
    try:
        w.set_focus()
    except Exception:
        try:
            pyautogui.hotkey('alt', 'tab')
        except Exception:
            pass
    time.sleep(0.5)
    return True


# ═══════════════════════════════════════════════════════════
# CORE ACTIONS
# ═══════════════════════════════════════════════════════════

def _slow_hotkey(*keys):
    """pyautogui.hotkey presses and releases modifiers with no delay, which
    a RemoteApp/RDP session can drop (the modifier is missed, so Ctrl+A
    never selects anything). Hold each key explicitly with a short gap."""
    for k in keys:
        pyautogui.keyDown(k)
        time.sleep(0.08)
    time.sleep(0.08)
    for k in reversed(keys):
        pyautogui.keyUp(k)
        time.sleep(0.08)


def _navigate_via_addressbar(url):
    """Open `url` in a FRESH tab, then close the old one.

    Replacing text in the existing address bar is unreliable inside the
    RemoteApp session: select-all (Alt+D / Ctrl+L / Ctrl+A / Shift+Home)
    keeps failing to register, so the paste APPENDED to the old URL and
    stacked up into a 404. A new tab's address bar is always empty, so
    there is nothing to select or clear. A new tab also starts with the
    dev console closed, which keeps the Ctrl+Shift+J toggle state known."""
    if not _ensure_edge_focused():
        return False
    pyautogui.press('escape')
    time.sleep(0.2)
    _slow_hotkey('ctrl', 't')
    time.sleep(1)
    pyperclip.copy(url)
    _slow_hotkey('ctrl', 'v')
    time.sleep(0.4)
    pyautogui.press('enter')
    # Let the page load before the caller opens the console and pastes JS —
    # otherwise the keystrokes hit a still-loading page / the address bar.
    time.sleep(PAGE_LOAD_WAIT)
    # New tabs open to the right of the current one, so the previous tab is
    # the old (stale/broken) one — close it, leaving only the fresh tab.
    _slow_hotkey('ctrl', 'shift', 'tab')
    time.sleep(0.5)
    _slow_hotkey('ctrl', 'w')
    time.sleep(1)
    return True


def _open_console_and_enable_paste():
    """Focus Edge, open dev console, enable pasting. Leaves console OPEN."""
    if not _ensure_edge_focused():
        return False

    pyautogui.hotkey('ctrl', 'shift', 'j')
    time.sleep(1)

    pyperclip.copy("console.log('test')")
    pyautogui.hotkey('ctrl', 'v')
    time.sleep(0.5)

    pyautogui.hotkey('ctrl', 'a')
    time.sleep(0.2)
    pyautogui.press('delete')
    time.sleep(0.2)

    pyautogui.typewrite('allow pasting', interval=0.05)
    time.sleep(0.2)
    pyautogui.press('enter')
    time.sleep(0.5)
    return True


def _console_run(js_code, wait=0.5):
    pyperclip.copy(js_code)
    pyautogui.hotkey('ctrl', 'v')
    time.sleep(0.3)
    pyautogui.press('enter')
    time.sleep(wait)


# How the page talks back to Python. There are two channels, both written by
# reply(): the Edge WINDOW TITLE (document.title — Python reads it over UIA,
# the same way it already finds the Edge window; works even with the console
# closed and doesn't depend on RDP clipboard redirection, which is often
# one-way, local -> remote, on Windows 365), and the clipboard via the
# console-only copy() utility. `copy` isn't in scope inside timers, so it's
# captured into a local up front. The title is put back after keepMs so the
# tab doesn't show the token in the evidence screenshots.
_REPLY_JS_PRELUDE = """
    var _copy = copy;
    if (!/^ITOS_/.test(document.title)) window.__itosT0 = document.title;
    function reply(msg, keepMs) {
        document.title = msg;
        try { _copy(msg); } catch (e) {}
        setTimeout(function() {
            if (document.title === msg) document.title = window.__itosT0 || '';
        }, keepMs);
    }
"""

# Runs in the dev console: polls until the page is actually idle (document
# loaded, no jQuery ajax in flight, no visible Kendo mask / "In Progress ..."
# overlay) for ~0.6s straight, then replies "<token>:ok". Gives up (and
# replies "<token>:timeout") after PAGE_IDLE_MAX_MS so a page that never
# settles can't hang.
_WAIT_IDLE_JS = """
(function() {{
    {prelude}
    var stable = 0, t0 = Date.now();
    function visible(el) {{
        return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    }}
    function ready() {{
        try {{ return !!({ready_js}); }} catch (e) {{ return false; }}
    }}
    function busy() {{
        if (!ready()) return true;
        if (document.readyState !== 'complete') return true;
        if (window.jQuery && jQuery.active > 0) return true;
        var masks = document.querySelectorAll('.k-loading-mask, .k-loading-image, .k-loading, .blockUI');
        for (var i = 0; i < masks.length; i++) {{ if (visible(masks[i])) return true; }}
        var leaves = document.querySelectorAll('div, span, p');
        for (var j = 0; j < leaves.length; j++) {{
            var el = leaves[j];
            if (!el.children.length && /^In Progress/i.test(el.textContent.trim()) && visible(el)) return true;
        }}
        return false;
    }}
    var iv = setInterval(function() {{
        stable = busy() ? 0 : stable + 1;
        if (stable >= 3 || Date.now() - t0 > {max_ms}) {{
            clearInterval(iv);
            reply('{token}:' + (ready() ? 'ok' : 'timeout'), 2500);
        }}
    }}, 200);
}})();
"""
PAGE_IDLE_MAX_MS = int(os.getenv("ITOS_PAGE_IDLE_MAX_MS", "30000"))


def _edge_titles() -> list[str]:
    titles = []
    try:
        windows = Desktop(backend="uia").windows()
    except Exception:
        return titles
    for w in windows:
        try:
            t = w.window_text()
        except Exception:
            continue
        if t and "Edge" in t:
            titles.append(t)
    return titles


def _poll_reply(token: str, use_clipboard: bool = True) -> str | None:
    """One look at both reply channels (see _REPLY_JS_PRELUDE): the payload
    after "<token>:", or None if neither carries it yet."""
    pat = re.compile(re.escape(token) + r":(\w+)")
    if use_clipboard:
        try:
            m = pat.match(pyperclip.paste())
            if m:
                return m.group(1)
        except Exception:
            pass
    for title in _edge_titles():
        m = pat.search(title)
        if m:
            return m.group(1)
    return None


def _wait_reply(token: str, timeout: float) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = _poll_reply(token)
        if got is not None:
            return got
        time.sleep(0.15)
    return None


def _wait_until_page_idle(label="", ready_js="true", required=False):
    """Blocks until iTOS has finished loading/rendering what the last action
    triggered. Console must be OPEN.

    `ready_js` is a JS expression that must be truthy before the page counts
    as ready — i.e. the element the NEXT step needs actually exists. This is
    what stops a step firing against the previous page: the idle checks alone
    pass on a fully-rendered search-results page that simply hasn't been
    navigated away from yet.

    required=True raises ScreenshotAutomationError if ready_js never became
    true (so the worker retries instead of screenshotting/clicking the wrong
    page). required=False just logs and carries on. A reply that never
    comes back is never fatal on its own: the caller's fixed sleeps still
    apply, same as before this wait existed."""
    token = f"ITOS_IDLE_{int(time.time() * 1000)}"
    pyperclip.copy("")
    _console_run(
        _WAIT_IDLE_JS.format(prelude=_REPLY_JS_PRELUDE, max_ms=PAGE_IDLE_MAX_MS, token=token, ready_js=ready_js),
        0.2,
    )
    result = _wait_reply(token, PAGE_IDLE_MAX_MS / 1000 + 3)

    if result == "ok":
        time.sleep(0.4)  # let the final frame paint
        return True
    if result == "timeout":
        msg = f"Page never became ready ({label}) within {PAGE_IDLE_MAX_MS // 1000}s"
        if required:
            raise ScreenshotAutomationError(msg)
        logger.warning(msg)
    else:
        logger.warning("Page-idle wait got no reply from the page (%s)", label)
    return False


def _console_query(js_expr: str, timeout: float = 6.0) -> str | None:
    """Evaluates `js_expr` in the (open) dev console and returns its string
    value (a single word: digits/letters), handed back through the reply
    channels. None if no reply came back — callers must treat that as
    "unknown", never as a negative answer."""
    token = f"ITOS_Q_{int(time.time() * 1000)}"
    pyperclip.copy("")
    _console_run(
        "(function() {" + _REPLY_JS_PRELUDE +
        f" var r; try {{ r = String({js_expr}); }} catch (e) {{ r = 'error'; }} "
        f"reply('{token}:' + r, 2500); }})();",
        0.2,
    )
    return _wait_reply(token, timeout)


# Runs in the console right before it is closed for a screenshot: reports
# whether DevTools is still docked (the page's inner size shrinks by the
# panel's size) through the window title only — the clipboard can't be used
# here, copy() needs the console. Keeps reporting until it has seen "closed"
# for ~2s (so Python has time to read it), then puts the real title back.
_CONSOLE_STATE_JS = """
(function() {{
    {prelude}
    var t0 = Date.now(), closedTicks = 0;
    var iv = setInterval(function() {{
        var docked = (window.outerWidth - window.innerWidth) > 100 || (window.outerHeight - window.innerHeight) > 220;
        document.title = '{token}:' + (docked ? 'open' : 'closed');
        closedTicks = docked ? 0 : closedTicks + 1;
        if (closedTicks >= 7 || Date.now() - t0 > 15000) {{
            clearInterval(iv);
            document.title = window.__itosT0 || '';
        }}
    }}, 300);
}})();
"""


def _close_console_verified() -> bool:
    """Closes the dev console and CONFIRMS it's gone before returning, so a
    screenshot can never catch it open. Ctrl+Shift+J is a toggle, and it's
    easy for it to land on an unfocused DevTools (e.g. right after the page
    navigated) and not close anything — so the state is read back and the
    key pressed again if needed. If the page can't report back at all, falls
    back to the old blind 1s wait."""
    token = f"ITOS_DT_{int(time.time() * 1000)}"
    _console_run(_CONSOLE_STATE_JS.format(prelude=_REPLY_JS_PRELUDE, token=token), 0.1)

    state = None
    for _press in range(3):
        pyautogui.hotkey('ctrl', 'shift', 'j')
        time.sleep(0.8)
        deadline = time.time() + 2.5
        state = None
        while time.time() < deadline:
            state = _poll_reply(token, use_clipboard=False)
            if state == "closed":
                break
            time.sleep(0.15)
        if state in ("closed", None):
            break

    if state is None:
        logger.warning("Could not read the DevTools state back; assuming it closed")
        time.sleep(1)
        return False
    if state != "closed":
        logger.warning("DevTools still open after 3 close attempts")
        return False

    # The token is in the tab title until the page restores it (~2s) — don't
    # screenshot before that or the tab strip shows it.
    deadline = time.time() + 4
    while time.time() < deadline and any(token in t for t in _edge_titles()):
        time.sleep(0.2)
    return True


# Counts the visible, non-placeholder rows of the Addresses grid.
#
# The grid is found by its own "Address Type" column header
# (th[data-field="AddressTypeName"]) — identical on the order page and the
# order-item page, so one script serves both address shots. Kendo splits a
# scrollable grid into a header table and a content table, so rows are
# counted in the whole .k-grid container, skipping thead. A row only counts
# when one of its first 7 cells has text (placeholder/filler rows don't).
# Returns a number, or 'nogrid' when no visible Addresses grid exists (i.e.
# we can't tell).
_ADDRESS_ROW_COUNT_JS = """(function() {
    function visible(el) { return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length); }
    var header = [...document.querySelectorAll('th[data-field="AddressTypeName"]')].find(visible);
    if (!header) return 'nogrid';
    var grid = header.closest('.k-grid');
    if (!grid) return 'nogrid';
    var n = 0;
    grid.querySelectorAll('tbody tr').forEach(function(row) {
        if (row.closest('thead') || row.classList.contains('k-no-data')) return;
        var cells = [...row.querySelectorAll('td')];
        if (!cells.length) return;
        if (cells.slice(0, 7).some(function(c) { return c.textContent.trim() !== ''; })) n++;
    });
    return n;
})()"""


def _check_addresses(where: str, warnings: list):
    """Console must be OPEN with the Addresses tab showing. Appends a
    warning when the grid has zero address rows; an unreadable result
    (no clipboard reply / grid not found) adds nothing — never guess."""
    res = _console_query(_ADDRESS_ROW_COUNT_JS)
    logger.info("Address rows on the %s: %r", where, res)
    if res == "0":
        warnings.append(f"No address on the {where}")
    elif res is None or not res.isdigit():
        logger.warning("Could not read the address rows on the %s (got %r)", where, res)


def _take_screenshot(path: Path, label=""):
    """Wait for the page to finish loading, close console, wait for a clean
    frame, screenshot, reopen console. Console is OPEN both before and after
    this call."""
    _wait_until_page_idle(label)
    _close_console_verified()
    time.sleep(0.3)
    pyautogui.screenshot(str(path))
    pyautogui.hotkey('ctrl', 'shift', 'j')
    time.sleep(0.5)


def _get_all_window_titles():
    titles = []
    try:
        for w in Desktop(backend="uia").windows():
            t = w.window_text()
            if t:
                titles.append(t)
    except Exception:
        pass
    return titles


def _kill_windows_app():
    try:
        subprocess.run('taskkill /f /im "Windows App.exe" 2>nul', shell=True, timeout=5)
    except Exception:
        pass
    try:
        subprocess.run('taskkill /f /im "msrdc.exe" 2>nul', shell=True, timeout=5)
    except Exception:
        pass
    time.sleep(2)


# ═══════════════════════════════════════════════════════════
# PHASE 1 — ENSURE EDGE IS OPEN
# ═══════════════════════════════════════════════════════════

def _check_existing_edge():
    w = _find_edge_wrapper()
    if not w:
        return False
    _focus_edge(w)
    _navigate_via_addressbar(INDEX_URL)
    time.sleep(0.5)
    return True

def _launch_edge_fresh():
    subprocess.Popen(f'explorer.exe shell:appsFolder\\{APP_ID}', shell=True)
    time.sleep(8)

    try:
        app = Application(backend="uia").connect(title_re=".*Windows App.*", timeout=20)
    except Exception:
        print("Could not connect to Windows App")
        return False

    window = app.window(title_re=".*Windows App.*")

    # Force the window visible
    try:
        # import ctypes
        hwnd = window.handle
        print(f"Window handle: {hwnd}")
        print(f"Window rect before: {window.rectangle()}")

        ctypes.windll.user32.ShowWindow(hwnd, 9)        # SW_RESTORE
        time.sleep(0.5)
        ctypes.windll.user32.ShowWindow(hwnd, 3)        # SW_MAXIMIZE
        time.sleep(0.5)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        time.sleep(1)

        print(f"Window visible: {window.is_visible()}")
        print(f"Window rect after: {window.rectangle()}")
    except Exception as e:
        print(f"ctypes focus failed: {e}")

    # Debug screenshot to verify Windows App is on screen

    pyautogui.click(APPS_TAB_X, APPS_TAB_Y)
    time.sleep(2)

    pyautogui.doubleClick(CARD_X, CARD_Y)
    time.sleep(1)

    for _ in range(10):
        time.sleep(1)
        if "Van Moer Production - 1 Workspace" in _get_all_window_titles():
            break

    for _ in range(30):
        time.sleep(1)
        w = _find_edge_wrapper()
        if w:
            _focus_edge(w)
            _navigate_via_addressbar(TARGET_URL)
            time.sleep(0.5)
            return True

    return False

def _ensure_edge_ready() -> bool:
    if _check_existing_edge():
        return True

    for attempt in range(1, MAX_EDGE_LAUNCH_RETRIES + 1):
        if attempt > 1:
            _kill_windows_app()
            time.sleep(3)
        if _launch_edge_fresh():
            return True
        _kill_windows_app()

    return False


def _reconnect_if_needed() -> bool:
    # Always route through _ensure_edge_ready() — specifically its first
    # step, _check_existing_edge() — which is what actually navigates to
    # the iTOS index page. The old shortcut here (_check_edge_alive():
    # true the instant ANY Edge window exists, on ANY page) skipped that
    # navigation entirely, so a freshly-opened Edge window sitting on its
    # default Bing/new-tab page — or any other leftover page from before —
    # got treated as "ready," and the date-filter/search script ran against
    # whatever page it actually had loaded, only reaching iTOS afterward
    # (at the end of capture_order_screenshots(), which navigates back to
    # the index page to reset state for the *next* job — too late for the
    # one that had just run against the wrong page). Every request now
    # explicitly (re)navigates to the iTOS index page before anything else
    # happens, whether Edge was already open or not.
    return _ensure_edge_ready()


# ═══════════════════════════════════════════════════════════
# PHASE 2 — PROCESS ONE ORDER
# ═══════════════════════════════════════════════════════════

def _prepare_index_page(wait_for_load=False):
    """Get the index page ready for searching. Console is OPEN when this returns."""
    if wait_for_load:
        time.sleep(8)

    if not _open_console_and_enable_paste():
        return False

    # Computed from TODAY's real date every time, never from whatever the
    # picker currently shows — this runs once per order in a batch, so
    # reading-and-shifting the picker's own (already-shifted) value here
    # compounded further back on every subsequent order in the same run
    # (2026 -> 2020 -> 2014 -> 2008 ...). Setting an absolute target date is
    # idempotent no matter how many times this gets called.
    _console_run(f"""
        (function() {{
            var attempts = 0;
            var check = setInterval(function() {{
                attempts++;
                if (typeof $ !== "undefined" && $("#dateFrom").length > 0) {{
                    clearInterval(check);
                    var picker = $("#dateFrom").data("kendoDatePicker");
                    if (picker) {{
                        var d = new Date();
                        d.setFullYear(d.getFullYear() - {DATE_FROM_YEARS_BACK});
                        picker.value(d);
                        picker.trigger("change");
                    }}
                }}
                if (attempts > 15) {{ clearInterval(check); }}
            }}, 500);
        }})();
    """, 3)
    return True


def _click_tab_js(tab_text: str) -> str:
    return f"""
        (function() {{
            var tab = [...document.querySelectorAll('a.header.ui-tabs-anchor')]
                .find(el => el.textContent.trim() === '{tab_text}');
            if (tab) {{ tab.click(); }}
        }})();
    """


def _process_single_order(
    order_number: str, order_folder: Path, search_value: str, on_shot1_captured=None,
    client_slug: str = "sabic", warnings: list | None = None,
) -> list[Path]:
    """3-screenshot flow for one order (4 for Sabic — see below). Non-fatal
    findings (e.g. an empty Addresses tab) are appended to `warnings`. Console must be open with paste
    enabled when called; console is CLOSED when this returns.

    `order_number` names the output files; `search_value` is what actually
    gets typed into iTOS's External ID box — the two are allowed to differ
    (e.g. searching by a business reference while still filing screenshots
    under the ITOS number's folder/prefix).

    `on_shot1_captured`, if given, is called with `shot1`'s path the instant
    it's saved — before shots 2/3 are taken — so a caller can kick off
    slow, unrelated work (e.g. an OCR/vision API call) on a background
    thread that overlaps with the remaining ~10s of on-screen automation
    instead of adding its own latency after this function returns. Any
    exception it raises is swallowed (it must never break the screenshot
    flow); this module still has no idea what the callback does."""
    if warnings is None:
        warnings = []
    shot1_tab, shot2_tab = CLIENT_TABS.get(client_slug, CLIENT_TABS["sabic"])
    shot1 = order_folder / f"{order_number}_01_order.png"
    shot1b = order_folder / f"{order_number}_01b_order_addresses.png"
    shot2 = order_folder / f"{order_number}_02_addresses.png"
    shot3 = order_folder / f"{order_number}_03_transport.png"

    # ── SCREENSHOT 1 — Search → main tile ────────────
    # A leftover value in the separate External ID field (left over from
    # browsing a previous order, or a previous search) makes iTOS filter by
    # BOTH fields at once — clear it first, only if it actually has a value,
    # so the Number search below is the sole filter in effect.
    _console_run(f"""
    (function() {{
        var orderNum = $("#orderNumber");
        if (orderNum.length && orderNum.val()) {{
            orderNum.val("").trigger("input").trigger("change").trigger("keyup");
        }}

        // Step 2: Enter the value in External ID field
        $("#externalId").val("{search_value}").trigger("input").trigger("change").trigger("keyup");

        // Step 3: Click Search, then click the first result tile
        setTimeout(function() {{
            $(".menu-item[title='Search']").trigger("click");
            var check = setInterval(function() {{
                var tile = $(".tile.order").first();
                if (tile.length) {{
                    clearInterval(check);
                    tile.trigger("click");
                }}
            }}, 200);
            setTimeout(function() {{ clearInterval(check); }}, 8000);
        }}, 300);
    }})();
    """, 4)

    # The search + tile click above only *starts* navigation to the order
    # page; wait until it has really loaded (its tabs exist) before clicking
    # anything, otherwise the tab click lands on the still-showing results page.
    _wait_until_page_idle(
        "order page after search",
        ready_js="document.getElementById('orderItemsTab') && document.querySelector('a.header.ui-tabs-anchor')",
        required=True,
    )

    # Click the client's tab before capturing the order overview shot.
    _console_run(_click_tab_js(shot1_tab), 1.5)
    _take_screenshot(shot1, "Order overview")
    if on_shot1_captured is not None:
        try:
            on_shot1_captured(shot1)
        except Exception:
            pass

    # Sabic only: same order page, Addresses tab, captured right after the
    # Attachments shot. "_01b_" sorts between _01_order and _02_addresses, so
    # list_screenshot_files() / the email paste order stay in capture order.
    if client_slug == "sabic":
        _console_run("""
            [...document.querySelectorAll('a.ui-tabs-anchor')]
              .find(tab => tab.textContent.trim() === 'Addresses')
              ?.click();
        """, 1.5)
        _take_screenshot(shot1b, "Order addresses")
        _check_addresses("order", warnings)

    # ── SCREENSHOT 2 — Order Items → tile → Addresses ─
    _console_run("""document.getElementById("orderItemsTab").click();""", 1.5)
    _wait_until_page_idle(
        "order item tile", ready_js="document.querySelector('#orderItemsView .tile.orderItem')", required=True,
    )
    _console_run("""
        (function() {
            var tile = document.querySelector("#orderItemsView .tile.orderItem");
            if (tile) { tile.click(); }
        })();
    """, 1.5)
    _wait_until_page_idle("item detail tabs", ready_js="document.querySelector('#detailTabs')", required=True)
    if shot2_tab == "Addresses":
        _console_run("""
            (function() {
                var tab = document.querySelector('#detailTabs a[href="#addressesTab"]');
                if (tab) { tab.click(); }
            })();
        """, 1.5)
    else:
        _console_run(_click_tab_js(shot2_tab), 1.5)
    _take_screenshot(shot2, shot2_tab)
    if client_slug == "sabic" and shot2_tab == "Addresses":
        _check_addresses("order item", warnings)

    # ── SCREENSHOT 3 — Stock Info → Transport ─────────
    _console_run("""
        (function() {
            var tab = document.querySelector('#detailTabs a[href="#stockInfoTab"]');
            if (tab) { tab.click(); }
        })();
    """, 1.5)
    # The transport link is only rendered once the stock info tab has loaded.
    # Not required: the click below silently skips if it never shows up, as
    # it always did.
    _wait_until_page_idle(
        "stock info transport link",
        ready_js="""(function() {
            var i = document.querySelector('input[data-bind*="ExternalId"]');
            var v = i && i.value.trim();
            return !!v && [...document.querySelectorAll('a')].some(function(a) { return a.textContent.includes(v); });
        })()""",
    )
    _console_run("""
        (function() {
            var externalId = (document.querySelector('input[data-bind*="ExternalId"]') || {}).value;
            if (externalId) {
                externalId = externalId.trim();
                var links = document.querySelectorAll('a');
                for (var i = 0; i < links.length; i++) {
                    if (links[i].textContent.includes(externalId)) {
                        links[i].click();
                        break;
                    }
                }
            }
        })();
    """, 3)
    _take_screenshot(shot3, "Transport")

    # Close console — clean state for the next order in this batch.
    pyautogui.hotkey('ctrl', 'shift', 'j')
    time.sleep(0.3)

    paths = [shot1, shot1b, shot2, shot3] if client_slug == "sabic" else [shot1, shot2, shot3]
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise ScreenshotAutomationError(
            f"Screenshot(s) not written for order {order_number}: {[str(p) for p in missing]}"
        )
    return paths


# ═══════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ═══════════════════════════════════════════════════════════

def capture_order_screenshots(
    order_number: str, output_dir: Path, search_value: str | None = None, on_shot1_captured=None,
    client_slug: str = "sabic", warnings: list | None = None,
) -> list[Path]:
    """
    Ensures Edge/iTOS is ready (reusing an existing session if one's already
    open, otherwise launching one), searches `search_value` on the iTOS
    index page, and saves 3 PNGs into `output_dir` named after
    `order_number`. `search_value` defaults to `order_number` when not
    given, but a caller can pass a different business reference to search
    by while still filing the screenshots under the ITOS number's
    folder/prefix.

    `on_shot1_captured` is passed straight through to `_process_single_order`
    — see its docstring.

    Raises ScreenshotAutomationError with a human-readable reason on any
    failure instead of silently returning False, so the caller (the
    background worker) can record a real last_error and decide whether to
    retry. Never called concurrently with itself — see this module's
    docstring for why that would corrupt the run.
    """
    if sys.platform != "win32":
        raise ScreenshotAutomationError("iTOS screenshot automation only runs on Windows.")

    if search_value is None:
        search_value = order_number

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not _reconnect_if_needed():
        raise ScreenshotAutomationError("Could not open or reconnect to Edge/Windows App.")

    if not _prepare_index_page(wait_for_load=False):
        raise ScreenshotAutomationError("Could not prepare the iTOS index page (console/date filter).")

    try:
        paths = _process_single_order(
            order_number, output_dir, search_value, on_shot1_captured, client_slug, warnings,
        )
    except ScreenshotAutomationError:
        raise
    except Exception as e:
        raise ScreenshotAutomationError(f"Automation failed for order {order_number}: {e}") from e

    # Navigate back to index so the next queued job starts from a known state.
    # (_navigate_via_addressbar already waits for the page to load.)
    _navigate_via_addressbar(INDEX_URL)

    return paths
