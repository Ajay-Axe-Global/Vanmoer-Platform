"""
One-off calibration tool for helpers/itos_automation.py.

itos_automation.py drives Windows App / Edge with hardcoded pixel
coordinates (ITOS_APPS_TAB_X/Y, ITOS_CARD_X/Y). Those are only valid for
one specific screen resolution/window layout — if the monitor, resolution,
or Windows App's own UI changes, they go stale silently (clicks land on
nothing, and it just looks like "it opens Windows App and stops there").

This script launches Windows App itself, maximizes it exactly the way
itos_automation.py does, then locates:
  - the sidebar's currently-selected nav item (Windows App draws a thin
    blue bar on its left edge — normally that's "Apps", since that's the
    tab it opens on) by scanning for that blue indicator bar,
  - the first app card in the workspace's app grid, by finding the bottom
    of the "<workspace> - N Workspace" heading (a dense band of the
    theme's accent-blue text) and then finding the app icon's colorful
    pixels just below it.

We tried doing this properly via UI Automation first (pywinauto), but
Windows App's content is a Chromium/WebView2 pane that doesn't build an
accessibility tree by default — descendants() only sees the outer window
chrome (title bar buttons, one big "Pane"), nothing inside it. Hence the
pixel-based approach below.

Screenshots on this machine capture very dark (an HDR/GDI capture quirk on
this VM) — this script auto-contrasts before analyzing, and saves an
annotated copy so you can visually confirm the points before trusting
them.

Usage (from the repo root):
    python -m helpers.coordinates
"""
import sys
from pathlib import Path

import pyautogui
from PIL import ImageDraw, ImageOps

from helpers.itos_automation import _launch_windows_app_maximized

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "debug_coordinates.png"

TITLEBAR_HEIGHT = 60   # skip the window's own title bar
TASKBAR_HEIGHT = 60    # skip the Windows taskbar at the bottom
SATURATION_THRESHOLD = 40


def _saturation(rgb):
    r, g, b = rgb
    return max(r, g, b) - min(r, g, b)


def _is_accent_blue(rgb):
    r, g, b = rgb
    return b > r + 20 and b > g + 10 and b > 100


def _find_selected_sidebar_item(im):
    """Find the blue selection bar Windows App draws on the left edge of
    the active sidebar nav item, and the horizontal center of that item's
    icon tile on the same row. Returns (x, y) or None."""
    w, h = im.size
    px = im.load()

    best_run = None
    best_x = None
    for x in range(4, 20):
        run_start = None
        for y in range(TITLEBAR_HEIGHT, h - TASKBAR_HEIGHT):
            blue = _is_accent_blue(px[x, y])
            if blue and run_start is None:
                run_start = y
            elif not blue and run_start is not None:
                length = y - run_start
                if best_run is None or length > (best_run[1] - best_run[0]):
                    best_run = (run_start, y - 1)
                    best_x = x
                run_start = None

    if best_run is None or (best_run[1] - best_run[0]) < 10:
        return None

    bar_x = best_x
    y_center = (best_run[0] + best_run[1]) // 2

    # The icon's white/light tile background stands out against the
    # sidebar's own background color on that row — find its horizontal
    # extent, starting just past the blue bar.
    sidebar_bg = px[2, y_center]
    tile_start = None
    tile_end = None
    for x in range(bar_x + 2, min(bar_x + 100, w)):
        differs = abs(px[x, y_center][0] - sidebar_bg[0]) > 8
        if differs and tile_start is None:
            tile_start = x
        elif not differs and tile_start is not None:
            tile_end = x - 1
            break
    if tile_start is None:
        return (bar_x + 20, y_center)  # fallback: rough guess past the bar
    if tile_end is None:
        tile_end = tile_start + 30

    return ((tile_start + tile_end) // 2, y_center)


def _find_first_app_card(im):
    """Find the heading row ("<workspace name> - N Workspace", rendered in
    the theme's bold accent-blue) and then the colorful app icon in the
    card grid just below it. Returns (x, y) or None."""
    w, h = im.size
    px = im.load()

    search_x = (100, min(900, w - 50))
    heading_search_y = (150, 500)

    row_counts = {}
    for y in range(*heading_search_y):
        row_counts[y] = sum(
            1 for x in range(*search_x) if _is_accent_blue(px[x, y])
        )

    heading_rows = [y for y, c in row_counts.items() if c > 30]
    if not heading_rows:
        return None
    heading_bottom = max(heading_rows)

    y0, y1 = heading_bottom + 10, min(heading_bottom + 250, h - TASKBAR_HEIGHT)
    minx = miny = None
    maxx = maxy = None
    for y in range(y0, y1):
        for x in range(*search_x):
            if _saturation(px[x, y]) > SATURATION_THRESHOLD:
                if minx is None or x < minx:
                    minx = x
                if maxx is None or x > maxx:
                    maxx = x
                if miny is None or y < miny:
                    miny = y
                if maxy is None or y > maxy:
                    maxy = y

    if minx is None:
        return None
    return ((minx + maxx) // 2, (miny + maxy) // 2)


def _save_annotated_screenshot(bright, apps_tab, card):
    annotated = bright.copy()
    draw = ImageDraw.Draw(annotated)
    for point, label, color in ((apps_tab, "APPS_TAB", "red"), (card, "CARD", "lime")):
        if point:
            x, y = point
            draw.ellipse([x - 12, y - 12, x + 12, y + 12], outline=color, width=3)
            draw.text((x + 16, y - 8), label, fill=color)
    annotated.save(OUTPUT_PATH)
    print(f"\nAnnotated (auto-contrasted) screenshot saved to: {OUTPUT_PATH}")


def main():
    if sys.platform != "win32":
        print("This tool only runs on Windows.")
        return 1

    print("Launching Windows App and maximizing it...")
    window = _launch_windows_app_maximized()
    if window is None:
        print("Could not launch/connect to Windows App — nothing to calibrate.")
        return 1

    print("\nAnalyzing screenshot...")
    shot = pyautogui.screenshot()
    bright = ImageOps.autocontrast(shot.convert("RGB"), cutoff=0)

    apps_tab = _find_selected_sidebar_item(bright)
    card = _find_first_app_card(bright)

    print("\n" + "=" * 60)
    if apps_tab:
        print(f"Detected sidebar's selected tab at: {apps_tab}")
        print("(should be 'Apps' — that's the tab Windows App opens on;")
        print(" check the annotated screenshot to confirm)")
    else:
        print("Could NOT auto-detect the sidebar tab. Open the annotated "
              "screenshot below and read the 'Apps' icon's coordinates off "
              "it directly (e.g. open it in Paint and hover the cursor).")

    if card:
        print(f"\nDetected first app card at: {card}")
    else:
        print("\nCould NOT auto-detect the app card. Open the annotated "
              "screenshot below and read the card's coordinates off it "
              "directly.")

    _save_annotated_screenshot(bright, apps_tab, card)
    print("\nOpen that file and confirm the red/green circles actually "
          "land on the sidebar tab and the app card before trusting these "
          "values — this is a heuristic, not a guarantee.")

    if apps_tab and card:
        print("\nIf confirmed, paste into .env:")
        print(f"ITOS_APPS_TAB_X={apps_tab[0]}")
        print(f"ITOS_APPS_TAB_Y={apps_tab[1]}")
        print(f"ITOS_CARD_X={card[0]}")
        print(f"ITOS_CARD_Y={card[1]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
