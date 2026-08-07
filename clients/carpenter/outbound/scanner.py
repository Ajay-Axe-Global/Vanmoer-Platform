"""
scanner.py — Gemini-vision extraction for the Carpenter Outbound order table
screenshot (PNG/JPG).

The source is a screenshot of a shipping-system grid (columns like 16 REF,
ORDER#, INCO, VOY, VESSEL, LOAD, OH, PCS, NET KG, GROSS K, CONSIGNEE,
NEW ITEM, TCS REF — colored/merged header cells, no fixed layout), so plain
OCR isn't reliable. This sends the whole image to Gemini and asks for just
the two fields outbound.py needs per row: INCO and the reference (TCS REF,
falling back to CONSIGNEE when a sheet has no TCS REF column).
"""

from helpers.gemini_client import call_gemini

_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _mime_type_for(path: str) -> str:
    for ext, mime in _MIME_TYPES.items():
        if path.lower().endswith(ext):
            return mime
    return "image/png"


def _build_prompt() -> str:
    return """\
You are a data-extraction specialist reading a screenshot of a shipping/order
table grid. It has a header row followed by one data row per shipment order.
Column headers may include: 16 REF, ORDER#, INCO, VOY, VESSEL, LOAD, OH, PCS,
NET KG, GROSS K, CONSIGNEE, NEW ITEM, TCS REF (exact header wording, casing,
column order and presence can vary between screenshots).

─── TASK ───
For every DATA row (skip the header row), extract:
  • inco       — the value from the INCO column (e.g. "DAP", "DDP"). If there
                 is no INCO column, return "".
  • tcs_ref    — the value from the "TCS REF" column (may also be labeled
                 "TCS Ref", "TCS Reference", etc.). If there is no such
                 column, or the cell is blank, return "".
  • consignee  — the value from the "CONSIGNEE" column. If there is no such
                 column, return "".

Some cells span multiple lines (e.g. an ORDER# cell listing "10181585" and
"10181583" stacked) — that does not affect inco/tcs_ref/consignee, which are
each single values per row.

─── OUTPUT FORMAT ───
Return a JSON array, one element per data row, in top-to-bottom order:

[
  {"inco": "DAP", "tcs_ref": "H600070860", "consignee": "WERK SCHWEINFURT"},
  {"inco": "DAP", "tcs_ref": "H600070860", "consignee": "WERK SCHWEINFURT"}
]

─── RULES ───
1. Extract EVERY data row — do not skip or summarize any.
2. Do not invent values. If a column is missing or a cell is blank, use "".
3. Return ONLY the JSON array — no markdown fences, no commentary, no extra text.\
"""


def scan_table_image(image_path: str) -> list[dict]:
    """
    Send the table screenshot to Gemini and return one dict per data row:
    {"inco": str, "tcs_ref": str, "consignee": str}.
    """
    raw = call_gemini(
        _build_prompt(),
        pdf_path=image_path,
        mime_type=_mime_type_for(image_path),
        max_output_tokens=4096,
    )
    if isinstance(raw, dict):
        raw = [raw]

    rows = []
    for item in raw:
        rows.append({
            "inco": str(item.get("inco", "")).strip(),
            "tcs_ref": str(item.get("tcs_ref", "")).strip(),
            "consignee": str(item.get("consignee", "")).strip(),
        })
    return rows
