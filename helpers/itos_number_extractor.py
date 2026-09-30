"""
Reads the "VMR<digits>" iTOS order number off the order-overview screenshot
(the "*_01_order.png" shot from helpers/itos_automation.py) via Gemini
Vision, so helpers/screenshot_worker.py can pre-fill OrderTracking.itos_number
without an admin having to type it in manually.

Deliberately best-effort: itos_number is (and stays) an optional field with
no automated field ever reading FROM it (see database/models.py), so nothing
here should ever fail a screenshot job. Every failure path — bad image read,
no Gemini credits, no match found — logs and returns None; callers just
leave itos_number unset in that case.

Sends the FULL screenshot to Gemini rather than a fixed pixel crop of the
top-left corner (an earlier version did that, tuned against one 1920x1080
sample). That crop turned out fragile in prod: a "same resolution" remote
desktop on a physically bigger monitor can still run a different OS/browser
zoom level, shifting exactly where the label renders in pixel space even at
an identical reported width/height — the crop silently missed the label and
this always returned None, with no screenshot-job failure to signal it (by
design). Sending the whole image removes that dependency entirely, at the
cost of a slightly larger Gemini request.
"""

import logging
import re

from helpers.gemini_client import call_gemini

logger = logging.getLogger("itos_number_extractor")

ITOS_NUMBER_RE = re.compile(r"^VMR\d+$")

PROMPT = (
    "This is a screenshot of an order screen in a logistics system. Find "
    "the order number label formatted like \"VMR\" followed by digits "
    "(e.g. VMR717000) — it's normally near the top-left of the page, just "
    "under the toolbar. Reply with ONLY a JSON object: "
    "{\"itos_number\": \"VMR717000\"} if you find one, or "
    "{\"itos_number\": null} if you don't. No other text."
)


def extract_itos_number(shot1_path) -> str | None:
    """Best-effort extraction of the ITOS number from `shot1_path` (the
    order-overview screenshot). Never raises — returns None on any failure
    or if no valid VMR<digits> value is found."""
    try:
        with open(shot1_path, "rb") as f:
            image_bytes = f.read()
    except Exception as e:
        logger.warning("ITOS number extraction: could not read %s: %s", shot1_path, e)
        return None

    try:
        result = call_gemini(
            PROMPT,
            pdf_bytes=image_bytes,
            mime_type="image/png",
            call_label="itos_number",
        )
    except Exception as e:
        logger.warning("ITOS number extraction: Gemini call failed for %s: %s", shot1_path, e)
        return None

    value = result.get("itos_number") if isinstance(result, dict) else None
    if not value or not isinstance(value, str):
        logger.info("ITOS number extraction: no value found in %s", shot1_path)
        return None

    value = value.strip().upper()
    if not ITOS_NUMBER_RE.match(value):
        logger.warning(
            "ITOS number extraction: Gemini returned %r for %s, doesn't match VMR<digits> — discarding",
            value, shot1_path,
        )
        return None

    logger.info("ITOS number extraction: found %s in %s", value, shot1_path)
    return value
