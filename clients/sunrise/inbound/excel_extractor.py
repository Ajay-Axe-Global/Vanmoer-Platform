"""
Sunrise Inbound — Excel Packing List extraction.

Sunrise's Packing List is ALWAYS an Excel workbook (never PDF) and is
ALWAYS scoped to exactly ONE container per file — confirmed with the user:
a shipment with several containers is uploaded as several separate Excel
files, one per container, not several blocks/sheets inside one file. So
this extractor reads exactly one "Intake reference:" block per call.

Like Emvia's Excel Packing List path (clients/emvia/inbound/excel_extractor.py),
this sheet is already machine-readable structured data — read directly with
pandas/openpyxl, no LLM involved, none of the digit-misread risk a vision
model has on a scanned PDF.

Sheet layout (see the reference sample):
  - Somewhere in the sheet's leading rows, a label cell containing "Intake
    reference" (case-insensitive, colon optional) with the container number
    in a following cell on the SAME row — this is the container number
    (SVRI's own term for it), matched against the MBL's container list in
    extractor.build_rows().
  - A header row further down with columns "PO Reference #", "Description",
    "Bag quantity", "SVRI code", "Batch / lot #", "Weight kg", "Notes /
    Requirements" — followed by one line-item row per bag lot until a blank
    row or a footer block ("Form completed by...").

Per client instruction: SVRI code is the Product, "Batch / lot #" is the
Lot — and when several rows share the SAME lot number within this
container, they collapse into ONE output row, with Bag quantity and Weight
kg SUMMED across every row sharing that lot (a different lot number always
starts a new row). That grouping/summing happens here, not in build_rows(),
so build_rows() gets one row per (container, lot) already.

Gross Weight is not stated separately anywhere on this sheet — only "Weight
kg" is given, so Net Weight and Gross Weight both mirror that same summed
figure (same convention as Emvia's Excel path and VMR's Packing List path
when a source document states only one weight figure).
"""

import re

import pandas as pd

from helpers.doc_common import fix_container_id, num, s

# ═══════════════════════════════════════════════════════════════════════════
# HEADER ALIASES
# ═══════════════════════════════════════════════════════════════════════════

def _normalize_header(header) -> str:
    text = str(header or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# canonical field -> every known header spelling seen so far, pre-normalized
# with _normalize_header(). Add a new variant here (not in the row-building
# code) whenever a sender's sheet uses different wording.
HEADER_ALIASES: dict[str, list[str]] = {
    "bag_quantity": ["bag quantity", "bags quantity", "bag qty", "bags qty"],
    "svri_code":    ["svri code"],
    "batch_lot":    ["batch lot", "batch lot no", "lot no", "lot number"],
    "weight_kg":    ["weight kg", "weight kgs", "net weight kg"],
}

REQUIRED_FIELDS = ("bag_quantity", "svri_code", "batch_lot", "weight_kg")

# How many of the sheet's leading rows to scan for the real header row — a
# sender's sheet has a title/address/"Intake reference" block above the
# actual column headers, so row 0 can't be assumed to be it.
HEADER_SCAN_ROWS = 40

_INTAKE_REF_RE = re.compile(r"intake\s*reference", re.IGNORECASE)


def _match_field_columns(row_values) -> dict[str, str]:
    field_to_column: dict[str, str] = {}
    for cell in row_values:
        normalized = _normalize_header(cell)
        if not normalized:
            continue
        for field, aliases in HEADER_ALIASES.items():
            if field in field_to_column:
                continue
            if normalized in aliases:
                field_to_column[field] = cell
    return field_to_column


def _find_header_row(raw: pd.DataFrame) -> tuple[int, dict[str, str]]:
    """Scans the sheet's leading rows for the one that matches every
    REQUIRED_FIELDS alias — raises ValueError (naming what was found and
    the rows scanned) if none does, a loud failure rather than silently
    reading the wrong row as data."""
    best_row_idx = -1
    best_match: dict[str, str] = {}

    for i in range(min(HEADER_SCAN_ROWS, len(raw))):
        match = _match_field_columns(raw.iloc[i].tolist())
        if len(match) > len(best_match):
            best_row_idx, best_match = i, match
        if len(match) == len(REQUIRED_FIELDS):
            break

    missing = [f for f in REQUIRED_FIELDS if f not in best_match]
    if missing:
        scanned_rows = [raw.iloc[i].tolist() for i in range(min(HEADER_SCAN_ROWS, len(raw)))]
        raise ValueError(
            f"Sunrise Packing List Excel — couldn't find a header row matching column(s): {', '.join(missing)}. "
            f"Best-matching row (row {best_row_idx + 1}): {list(raw.iloc[best_row_idx]) if best_row_idx >= 0 else 'none'}. "
            f"First {len(scanned_rows)} row(s) scanned: {scanned_rows}. "
            f"Add the new header spelling to HEADER_ALIASES in excel_extractor.py."
        )
    return best_row_idx, best_match


def _find_intake_reference(raw: pd.DataFrame) -> str:
    """Finds the "Intake reference:" label cell anywhere in the sheet's
    leading rows and returns the value in the next non-empty cell on that
    SAME row (the container number). Raises ValueError if not found — the
    container number drives which MBL container this Excel's rows are
    matched to, so a silent miss here would misattribute an entire
    container's cargo."""
    for i in range(min(HEADER_SCAN_ROWS, len(raw))):
        row_values = raw.iloc[i].tolist()
        for ci, cell in enumerate(row_values):
            if cell is not None and _INTAKE_REF_RE.search(str(cell)):
                for value in row_values[ci + 1:]:
                    # An empty cell in a dtype=str DataFrame comes back as
                    # float NaN, not None or "" — pd.isna() catches it
                    # before s(value) would otherwise stringify it to the
                    # literal (truthy) text "nan".
                    if pd.isna(value):
                        continue
                    text = s(value).strip()
                    if text:
                        return text
    raise ValueError(
        "Sunrise Packing List Excel — no \"Intake reference:\" cell found in the "
        f"first {min(HEADER_SCAN_ROWS, len(raw))} row(s). Every sheet must state its "
        "container number this way."
    )


# ═══════════════════════════════════════════════════════════════════════════
# WEIGHT PARSING — same comma/decimal + MT-vs-KG safety net as Emvia's Excel
# path (helpers/doc_common.num() only recognizes "." as a decimal marker).
# ═══════════════════════════════════════════════════════════════════════════

def _cell_str(value) -> str:
    """Safely stringify one raw pandas cell value from a dtype=str
    DataFrame. A blank cell doesn't come back as None or "" here — pandas
    gives back float NaN, which s() happily stringifies to the literal
    (non-empty, truthy) text "nan", and which num()'s own isinstance(float)
    fast-path passes straight through as an actual NaN value instead of
    defaulting to 0. Both silently corrupted the bags/weight sums for a
    genuinely blank cell (e.g. a footer/notes row) and produced a NaN in
    the final JSON response, which Python's json.dumps writes as the bare
    token `NaN` — not valid JSON, so the browser's JSON.parse rejected the
    whole response. Filtering the literal "nan" text out here, before it
    ever reaches s()/num() elsewhere in this module, is the single choke
    point that prevents that."""
    text = s(value).strip()
    return "" if text.lower() == "nan" else text


def _parse_weight_kg(raw) -> float:
    text = _cell_str(raw)
    if not text:
        return 0

    if "," in text:
        if "." in text:
            text = text.replace(".", "")
        text = text.replace(",", ".")

    return num(text, 0)


# ═══════════════════════════════════════════════════════════════════════════
# EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════

def extract_packing_list_excel(path: str) -> dict:
    raw = pd.read_excel(path, header=None, dtype=str)

    container_raw = _find_intake_reference(raw)
    cid, _ = fix_container_id(container_raw)

    header_row_idx, field_to_column = _find_header_row(raw)
    df = raw.iloc[header_row_idx + 1:].copy()
    df.columns = raw.iloc[header_row_idx]
    df = df.reset_index(drop=True)

    # Per-lot grouping/summing — several bag-lot rows can share the same
    # "Batch / lot #"; per client instruction, they collapse into ONE
    # output row with Bag quantity and Weight kg SUMMED. Order-preserving
    # so the output row order follows the sheet's own row order.
    lots: dict[str, dict] = {}
    order: list[str] = []
    for _, row in df.iterrows():
        lot_no = _cell_str(row.get(field_to_column["batch_lot"]))
        bag_qty = num(_cell_str(row.get(field_to_column["bag_quantity"])), 0)
        if not lot_no or not bag_qty:
            continue  # blank/subtotal/footer row

        if lot_no not in lots:
            lots[lot_no] = {
                "product":       _cell_str(row.get(field_to_column["svri_code"])),
                "lot_no":        lot_no,
                "bags_qty":      0,
                "net_weight_kg": 0,
            }
            order.append(lot_no)

        lots[lot_no]["bags_qty"] += bag_qty
        lots[lot_no]["net_weight_kg"] += _parse_weight_kg(row.get(field_to_column["weight_kg"]))

    return {
        "container_no": cid,
        "lots": [lots[lot_no] for lot_no in order],
    }
