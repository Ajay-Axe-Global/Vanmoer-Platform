"""
Emvia Inbound (Warehouse 1147) — Excel Packing List extraction.

Unlike the PDF packing list (extractor.py, Gemini-based), this sheet is
already machine-readable structured data — pandas/openpyxl reads exact cell
values with none of the digit-misread risk a vision model has on a scanned
PDF, so this path deliberately does NOT call Gemini. The only real
uncertainty on an Excel source is that a sender can reword a column header
between shipments (e.g. "Size x Thickness" vs "Size & Thickness") — that's a
header-matching problem, not a data-reading problem, solved below with a
small alias map (same pattern as helpers/doc_common.py's PORT_COUNTRY_MAP /
CUSTOMER_NAME_ALIASES, applied to column headers instead of free text). If a
required column can't be matched against ANY known alias, this fails loudly
(ValueError naming the missing field and the sheet's actual headers) rather
than silently guessing a wrong column — safer for numbers that drive weight
totals.
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
    "ref":               ["ref", "reference"],
    "receiver":          ["receiver", "consignee"],
    "container":         ["container", "container no", "container number", "container nr"],
    "specification":     ["specification", "spec"],
    "size_thickness":    ["size x thickness", "size thickness", "size and thickness"],
    "total_bndls":       ["total bndls", "tot bndls", "total bundles", "tot bundles"],
    "pcs_per_bdl":       ["pcs bdl", "pcs per bdl", "pieces bdl", "pieces per bundle"],
    "tot_gross_weight":  ["tot gross weight", "total gross weight", "tot gross wt", "total gross wt"],
}

REQUIRED_FIELDS = (
    "ref", "receiver", "container", "specification", "size_thickness",
    "total_bndls", "pcs_per_bdl", "tot_gross_weight",
)

# How many of the sheet's leading rows to scan for the real header row — a
# sender's sheet commonly has a title/logo/blank row (or two) above the
# actual column headers, so row 0 can't be assumed to be it.
HEADER_SCAN_ROWS = 20


def _match_field_columns(row_values) -> dict[str, str]:
    """Given one raw row's cell values (a candidate header row), returns
    {canonical_field: actual_cell_text} for every REQUIRED_FIELDS alias it
    matches — used both to score candidate header rows and, once the real
    one is picked, to build the final field->column mapping."""
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
    REQUIRED_FIELDS alias — that's the real header row, which isn't
    necessarily row 0 (title/logo/blank rows commonly precede it). Raises
    ValueError (naming what was found on the best-matching row, and the
    other rows scanned) if no row within HEADER_SCAN_ROWS matches all of
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
# EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════

def extract_packing_list_excel(path: str) -> dict:
    raw = pd.read_excel(path, header=None, dtype=str)
    header_row_idx, field_to_column = _find_header_row(raw)

    df = raw.iloc[header_row_idx + 1:].copy()
    df.columns = raw.iloc[header_row_idx]
    df = df.reset_index(drop=True)

    # A sender's sheet can merge cells vertically for a repeated value
    # (Ref/Receiver/Container/Specification identical across several bundle
    # rows) — pandas reads a merged cell's value only on its first row and
    # NaN on the rows beneath it, so those columns are forward-filled to
    # recover the intended per-row value.
    for field in ("ref", "receiver", "container", "specification"):
        col = field_to_column[field]
        df[col] = df[col].ffill()

    containers: dict[str, list[dict]] = {}
    for _, row in df.iterrows():
        container_raw = s(row.get(field_to_column["container"])).strip()
        if not container_raw:
            continue  # blank/subtotal row

        pieces = num(row.get(field_to_column["pcs_per_bdl"]), 0)
        if not pieces:
            continue  # not a real bundle row

        cid, _ = fix_container_id(container_raw)
        # This sheet gives only one weight column ("TOT GROSS weight") — no
        # separate net figure exists anywhere on it, so Net Weight mirrors
        # Gross Weight (per client instruction) rather than being left at 0.
        gross_weight_kg = num(row.get(field_to_column["tot_gross_weight"]), 0)
        containers.setdefault(cid, []).append({
            "product":          s(row.get(field_to_column["specification"])).strip(),
            "batch_no":         s(row.get(field_to_column["size_thickness"])).strip(),
            "pieces":           pieces,
            "pallet_count":     num(row.get(field_to_column["total_bndls"]), 0),
            "net_weight_kg":    gross_weight_kg,
            "gross_weight_kg":  gross_weight_kg,
            "reference":        s(row.get(field_to_column["ref"])).strip(),
            "receiver":         s(row.get(field_to_column["receiver"])).strip(),
        })

    return {
        "destination_country": "",
        "containers": [
            {"container_no": cid, "bundles": bundles}
            for cid, bundles in containers.items()
        ],
    }
