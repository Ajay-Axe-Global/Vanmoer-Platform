"""
Swiss Inbound — Excel Packing List extraction (e.g. "PLATO W Shipping
Summary" reports).

Same rationale as Emvia Inbound (Warehouse 1147)'s excel_extractor.py: this
sheet is already machine-readable structured data — pandas/openpyxl reads
exact cell values, none of the digit-misread risk a vision model has on a
PDF — so this path deliberately does NOT call Gemini. The only real
uncertainty is that different shippers/forwarders reword column headers
between shipments (e.g. "Container" vs "Container No" vs "Container No."),
and the sheet carries many MORE columns than this task needs (Release No.,
Client reference, Booking#, FFWD, ...) — solved with a small alias map
(same pattern as helpers/doc_common.py's PORT_COUNTRY_MAP, applied to
column headers instead of free text) that only looks for the 8 columns this
task actually consumes: Container, Seal No., Pallets, Bags, Net Weight
(kg), Gross Weight (kg), Grade, Lot No. Every other column on the sheet is
ignored outright. If a required column can't be matched against ANY known
alias, this fails loudly (ValueError naming the missing field and the
sheet's actual headers) rather than silently guessing a wrong column —
safer for numbers that drive weight totals.

Unlike the PDF Packing List layouts (which state weights in MT), this
sheet's own headers say "(kg)" — its weight columns are trusted as already
being KG, no MT conversion applied here.
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
# code) whenever a shipper's/forwarder's sheet uses different wording.
HEADER_ALIASES: dict[str, list[str]] = {
    "container":     ["container", "container no", "container no ", "container number",
                       "container nr", "container number ", "cntr no", "cntr no "],
    "seal_no":       ["seal no", "seal no ", "seal number", "seal", "seal nos"],
    "pallets":       ["pallets", "pallet", "pallet qty", "pallet count", "no of pallets",
                       "number of pallets", "pallets no"],
    "bags":          ["bags", "bag", "bags qty", "bags count", "no of bags",
                       "number of bags", "bags no"],
    "net_weight_kg": ["net weight kg", "net weight", "net wt kg", "net wt", "netweight kg",
                       "net weight kgs", "net wt kgs"],
    "gross_weight_kg": ["gross weight kg", "gross weight", "gross wt kg", "gross wt",
                         "grossweight kg", "gross weight kgs", "gross wt kgs"],
    "grade":         ["grade", "product", "product grade"],
    "lot_no":        ["lot no", "lot no ", "lot number", "lot", "batch no", "batch number"],
}

REQUIRED_FIELDS = (
    "container", "seal_no", "pallets", "bags", "net_weight_kg", "gross_weight_kg",
    "grade", "lot_no",
)

# How many of the sheet's leading rows to scan for the real header row — a
# sender's sheet commonly has a title/logo/report-number block (and several
# blank rows) above the actual column headers, so row 0 can't be assumed to
# be it (see the "PLATO W Shipping Summary" sample: the header row is
# several rows down, after Release No./Vessel Name/Customer/Site blocks).
HEADER_SCAN_ROWS = 30

# "Net Weight (kg)" and "Gross Weight (kg)" are each immediately followed on
# this sheet by their own unlabeled/duplicate "UOM" column — those UOM
# columns are not one of REQUIRED_FIELDS and are simply never matched or
# read; the "(kg)" in the header itself is trusted instead of parsing UOM.


def _match_field_columns(row_values) -> dict[str, str]:
    """Given one raw row's cell values (a candidate header row), returns
    {canonical_field: actual_cell_text} for every REQUIRED_FIELDS alias it
    matches — used both to score candidate header rows and, once the real
    one is picked, to build the final field->column mapping. Matches the
    FIRST cell that aliases to a given field (left-to-right), so a repeated
    ambiguous header (e.g. a later unrelated "Grade"-like column) never
    overrides the first, most likely genuine one."""
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
    REQUIRED_FIELDS alias — that's the real header row. Raises ValueError
    (naming what was found on the best-matching row, and the actual headers
    of every row scanned) if no row within HEADER_SCAN_ROWS matches all of
    them — a loud, obvious failure rather than silently reading the wrong
    row as data."""
    best_row_idx = -1
    best_match: dict[str, str] = {}

    for i in range(min(HEADER_SCAN_ROWS, len(raw))):
        match = _match_field_columns(raw.iloc[i].tolist())
        if len(match) > len(best_match):
            best_row_idx, best_match = i, match
        if len(match) == len(REQUIRED_FIELDS):
            break  # found a row matching everything — no need to scan further

    missing = [f for f in REQUIRED_FIELDS if f not in best_match]
    if missing:
        scanned_rows = [raw.iloc[i].tolist() for i in range(min(HEADER_SCAN_ROWS, len(raw)))]
        raise ValueError(
            f"Excel Packing List — couldn't find a header row matching column(s): {', '.join(missing)}. "
            f"Best-matching row (row {best_row_idx + 1}): {list(raw.iloc[best_row_idx]) if best_row_idx >= 0 else 'none'}. "
            f"First {len(scanned_rows)} row(s) scanned: {scanned_rows}. "
            f"Add the new header spelling to HEADER_ALIASES in excel_extractor.py."
        )
    return best_row_idx, best_match


# ═══════════════════════════════════════════════════════════════════════════
# NUMBER PARSING
# ═══════════════════════════════════════════════════════════════════════════

def _parse_number(raw) -> float:
    """Parses a numeric cell (weight, bag count, pallet count), stripping a
    thousands separator (e.g. "22,000" -> 22000) the same way
    doc_common.num() alone can't — that helper only recognizes "." as a
    decimal marker and would otherwise silently truncate at the comma.
    Unlike Emvia's MT-scale steel sheet (where a sub-1 decimal figure like
    "0,782" is plausible and genuinely ambiguous), this sheet's weight
    columns are already KG-scale (hundreds/thousands) per their own "(kg)"
    header — a comma here is always a thousands separator, never a decimal
    marker, so no European-decimal-comma heuristic is needed."""
    text = s(raw).strip()
    if not text:
        return 0
    return num(text.replace(",", ""), 0)


# ═══════════════════════════════════════════════════════════════════════════
# EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════

def extract_packing_list_excel(path: str) -> dict:
    """Returns the same common shape as extractor.extract_packing_list_pdf():
    a flat "containers" list of line items, each already carrying net/gross
    weight in KG — so build_rows() has one source-agnostic row-building
    path regardless of whether the Packing List arrived as Excel or PDF."""
    raw = pd.read_excel(path, header=None, dtype=str)
    # A blank cell survives dtype=str as an actual NaN (float), not "" — so
    # str(cell) would read "nan" (a truthy, non-empty string) rather than
    # blank. Replaced with "" up front so every blank cell — most
    # importantly a totals/subtotal row's blank Container cell, the signal
    # used below to skip that row — is recognized as genuinely empty.
    raw = raw.where(raw.notna(), "")
    header_row_idx, field_to_column = _find_header_row(raw)

    df = raw.iloc[header_row_idx + 1:].copy()
    df.columns = raw.iloc[header_row_idx]
    df = df.reset_index(drop=True)

    containers = []
    for _, row in df.iterrows():
        container_raw = s(row.get(field_to_column["container"])).strip()
        if not container_raw:
            continue  # blank/subtotal row (this sheet's own totals row at
            # the bottom, e.g. "62.7 | 3451 | 86,275 | ... 1,90,202", has no
            # Container value and is skipped here for free)

        cid, _ = fix_container_id(container_raw)
        containers.append({
            "container_id":     cid,
            "seal_no":          s(row.get(field_to_column["seal_no"])).strip(),
            "product":          s(row.get(field_to_column["grade"])).strip(),
            "lot_no":           s(row.get(field_to_column["lot_no"])).strip(),
            # Already KG on this sheet (header says "(kg)") — no MT
            # conversion, unlike the PDF layouts.
            "net_weight_kg":    _parse_number(row.get(field_to_column["net_weight_kg"])),
            "gross_weight_kg":  _parse_number(row.get(field_to_column["gross_weight_kg"])),
            "bags":             int(_parse_number(row.get(field_to_column["bags"]))),
            "pallets":          int(_parse_number(row.get(field_to_column["pallets"]))),
        })

    return {
        "packing_list_source": "excel",
        # No document-wide printed total on this sheet's own header block to
        # cross-check row sums against (unlike the PDF layouts' GRAND
        # TOTAL/TOTALS row) — left at 0 so validate()'s weight/bags/pallets
        # cross-checks are skipped gracefully rather than comparing against
        # a fabricated total.
        "total_bags": 0,
        "total_net_weight_mt": 0,
        "total_gross_weight_mt": 0,
        "total_pallets": 0,
        "containers": containers,
    }
