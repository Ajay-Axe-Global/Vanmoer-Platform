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
import os
import subprocess
import sys
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
DATE_FROM_YEARS_BACK = int(os.getenv("ITOS_DATE_FROM_YEARS_BACK", "5"))


def _declare_dpi_awareness():
    """
    Without this, Windows treats this process as "DPI-unaware" and secretly
    renders/reports everything to it at a scaled-DOWN virtual resolution
    (e.g. a real 1920x1080 24" monitor at 125% scaling appears to an unaware
    process as 1536x864) instead of true native pixels. pyautogui.screenshot()
    then captures at that reduced virtual resolution and Windows stretches it
    back up to fill the real screen — which is exactly what "low quality on
    a 24-inch monitor" looks like: it's not a compression/format issue, the
    captured bitmap itself has fewer real pixels than the display.

    Must be called once, as early as possible in the process — before any
    window/graphics APIs are touched — which is why this runs at import time
    of this module rather than lazily inside capture_order_screenshots().
    Tries the modern per-monitor-v2 API first, falling back for older
    Windows versions; safe to no-op on failure (screenshots just stay at
    whatever awareness the process already had).

    NOTE: this changes the process's coordinate space from virtual/scaled
    pixels to TRUE physical pixels. Any ITOS_APPS_TAB_X/Y and ITOS_CARD_X/Y
    values measured before this fix was in place were measured in the OLD
    (scaled) coordinate space and must be re-measured now that this is on,
    or clicks will land in the wrong spot.
    """
    if sys.platform != "win32":
        return
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 — Windows 10 1703+.
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE — Windows 8.1+.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        # System DPI aware — Vista+, universal fallback.
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_declare_dpi_awareness()


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
    try:
        for w in Desktop(backend="uia").windows():
            if _is_edge_title(w.window_text()):
                return w
    except Exception:
        pass
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

def _navigate_via_addressbar(url):
    if not _ensure_edge_focused():
        return False
    pyautogui.hotkey('alt', 'd')
    time.sleep(0.3)
    pyperclip.copy(url)
    pyautogui.hotkey('ctrl', 'v')
    time.sleep(0.2)
    pyautogui.press('enter')
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


def _take_screenshot(path: Path, label=""):
    """Close console, wait for a clean frame, screenshot, reopen console.
    Console is OPEN both before and after this call."""
    pyautogui.hotkey('ctrl', 'shift', 'j')
    time.sleep(2)
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
        import ctypes
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
    if _check_edge_alive():
        return True
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


def _process_single_order(order_number: str, order_folder: Path) -> list[Path]:
    """3-screenshot flow for one order. Console must be open with paste
    enabled when called; console is CLOSED when this returns."""
    shot1 = order_folder / f"{order_number}_01_order.png"
    shot2 = order_folder / f"{order_number}_02_addresses.png"
    shot3 = order_folder / f"{order_number}_03_transport.png"

    # ── SCREENSHOT 1 — Search → main tile ────────────
    # A leftover value in the separate External ID field (left over from
    # browsing a previous order, or a previous search) makes iTOS filter by
    # BOTH fields at once — clear it first, only if it actually has a value,
    # so the Number search below is the sole filter in effect.
    _console_run(f"""
    (function() {{
        var extId = $("#externalId");
        if (extId.length && extId.val()) {{
            extId.val("").trigger("input").trigger("change").trigger("keyup");
        }}
        $("#orderNumber").val("{order_number}").trigger("input").trigger("change").trigger("keyup");
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
    _take_screenshot(shot1, "Order overview")

    # ── SCREENSHOT 2 — Order Items → tile → Addresses ─
    _console_run("""document.getElementById("orderItemsTab").click();""", 2)
    _console_run("""
        (function() {
            var tile = document.querySelector("#orderItemsView .tile.orderItem");
            if (tile) { tile.click(); }
        })();
    """, 2)
    _console_run("""
        (function() {
            var tab = document.querySelector('#detailTabs a[href="#addressesTab"]');
            if (tab) { tab.click(); }
        })();
    """, 2)
    _take_screenshot(shot2, "Addresses")

    # ── SCREENSHOT 3 — Stock Info → Transport ─────────
    _console_run("""
        (function() {
            var tab = document.querySelector('#detailTabs a[href="#stockInfoTab"]');
            if (tab) { tab.click(); }
        })();
    """, 2)
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

    paths = [shot1, shot2, shot3]
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise ScreenshotAutomationError(
            f"Screenshot(s) not written for order {order_number}: {[str(p) for p in missing]}"
        )
    return paths


# ═══════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ═══════════════════════════════════════════════════════════

def capture_order_screenshots(order_number: str, output_dir: Path) -> list[Path]:
    """
    Ensures Edge/iTOS is ready (reusing an existing session if one's already
    open, otherwise launching one), searches `order_number` on the iTOS
    index page (this is exactly the ITOS number saved on an OrderTracking
    row — nothing else is searched on), and saves 3 PNGs into `output_dir`.

    Raises ScreenshotAutomationError with a human-readable reason on any
    failure instead of silently returning False, so the caller (the
    background worker) can record a real last_error and decide whether to
    retry. Never called concurrently with itself — see this module's
    docstring for why that would corrupt the run.
    """
    if sys.platform != "win32":
        raise ScreenshotAutomationError("iTOS screenshot automation only runs on Windows.")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not _reconnect_if_needed():
        raise ScreenshotAutomationError("Could not open or reconnect to Edge/Windows App.")

    if not _prepare_index_page(wait_for_load=False):
        raise ScreenshotAutomationError("Could not prepare the iTOS index page (console/date filter).")

    try:
        paths = _process_single_order(order_number, output_dir)
    except ScreenshotAutomationError:
        raise
    except Exception as e:
        raise ScreenshotAutomationError(f"Automation failed for order {order_number}: {e}") from e

    # Navigate back to index so the next queued job starts from a known state.
    _navigate_via_addressbar(INDEX_URL)
    time.sleep(6)

    return paths
