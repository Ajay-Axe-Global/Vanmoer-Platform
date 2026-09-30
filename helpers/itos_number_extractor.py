"""
Reads the "VMR<digits>" iTOS order number off the order-overview screenshot
(the "*_01_order.png" shot from helpers/itos_automation.py) via Gemini
Vision, so helpers/screenshot_worker.py can pre-fill OrderTracking.itos_number
without an admin having to type it in manually.

Deliberately best-effort: itos_number is (and stays) an optional field with
no automated field ever reading FROM it (see database/models.py), so nothing
here should ever fail a screenshot job. Every failure path — bad crop, no
Gemini credits, no match in the image — logs and returns None; callers just
leave itos_number unset in that case.

The crop box below was tuned against real 1920x1080 screenshots saved under
screenshots/sabic/*/*_01_order.png (the iTOS order number label always sits
top-left, just under the toolbar). If the remote desktop's resolution or the
iTOS page layout ever changes, re-tune CROP_BOX against a fresh screenshot
rather than trusting this blindly.
"""

import logging
import re

from PIL import Image

from helpers.gemini_client import call_gemini

logger = logging.getLogger("itos_number_extractor")

CROP_BOX = (0, 260, 260, 330)  # (left, top, right, bottom) in source pixels
ITOS_NUMBER_RE = re.compile(r"^VMR\d+$")

PROMPT = (
    "This image is a cropped corner of an order screen. It may contain a "
    "label formatted like \"VMR\" followed by digits (e.g. VMR717000). "
    "Reply with ONLY a JSON object: {\"itos_number\": \"VMR717000\"} if you "
    "find one, or {\"itos_number\": null} if you don't. No other text."
)


def extract_itos_number(shot1_path) -> str | None:
    """Best-effort extraction of the ITOS number from `shot1_path` (the
    order-overview screenshot). Never raises — returns None on any failure
    or if no valid VMR<digits> value is found."""
    try:
        with Image.open(shot1_path) as im:
            cropped = im.crop(CROP_BOX)
            import io
            buf = io.BytesIO()
            cropped.save(buf, format="PNG")
            image_bytes = buf.getvalue()
    except Exception as e:
        logger.warning("ITOS number extraction: could not read/crop %s: %s", shot1_path, e)
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
