"""
EDF Inbound (ED&F Man) — Excel Packing List extraction.

The only source document is the "INBOUND CONTAINER TRANSPORT & WAREHOUSE
ORDER" Excel sheet — already machine-readable, so (like Sunrise/Emvia's Excel
paths) it is read directly with pandas, no LLM involved.

Only the columns this task consumes are looked up, by an alias map over the
sheet's header text (the real header row sits several rows down, under the
company/address block, and its cells contain line breaks, e.g. "Number\\nof
bags" — headers are normalized before matching). Columns are addressed by
POSITION, not name, because this sheet repeats/blanks header cells.

  B/L                 -> bl_no
  CNT number          -> container
  CNT type            -> container_type
  Article number      -> product (primary)
  Product             -> product (fallback when no Article number column/value)
  Number of bags      -> bags
  Lot Nr. 2           -> lot   (ONLY this lot column — "Lot Nr. 1" is ignored)
  Quantity (MT) per FCL / Sugar product (25kg-50kg-BB)   [optional, only
      used for a bags x bag-size = quantity sanity check]

"Lot Nr. 2" can hold several lots for one container, each with its own bag
count, e.g. "L094-2026 - 22 bags, L425-2026 - 77 bags, L231-2026 - 289
bags". Each lot becomes its own row (same container, own lot + own bags).
A value that isn't in that "lot - N bags" shape (a single plain lot, or a
date like 30/12/2025) is kept verbatim as ONE row carrying the container's
full bag count.
"""

import re

import pandas as pd

from helpers.doc_common import fix_container_id, num, s


def _normalize_header(header) -> str:
    text = str(header or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# canonical field -> accepted normalized header spellings. Add a new variant
# here (not in the row code) when the sheet's wording changes.
HEADER_ALIASES: dict[str, list[str]] = {
    "bl_no":          ["b l", "bl", "b l no", "bl no", "b l number", "bl number"],
    "container":      ["cnt number", "cnt no", "container no", "container number", "container"],
    "container_type": ["cnt type", "container type"],
    "article":        ["article number", "article no", "article nr", "article"],
    "product":        ["product"],
    "bags":           ["number of bags", "no of bags", "bags", "bags qty"],
    "lot":            ["lot nr 2", "lot no 2", "lot number 2", "lot 2"],
    "quantity_mt":    ["quantity mt per fcl", "quantity mt", "quantity"],
    "bag_size":       ["sugar product 25kg 50 kg bb", "sugar product"],
}

# Product is taken from "Article number" first; the "Product" column is only
# the fallback (see extract_packing_list_excel), so neither is individually
# required — at least ONE of the two must exist (checked below).
REQUIRED_FIELDS = ("bl_no", "container", "container_type", "bags", "lot")

HEADER_SCAN_ROWS = 40


def _find_header_row(raw: pd.DataFrame) -> tuple[int, dict[str, int]]:
    """Returns (header_row_index, {field: column_position}). Raises
    ValueError naming the missing fields if no scanned row matches every
    REQUIRED_FIELDS alias."""
    best_idx, best = -1, {}
    for i in range(min(HEADER_SCAN_ROWS, len(raw))):
        match: dict[str, int] = {}
        for pos, cell in enumerate(raw.iloc[i].tolist()):
            norm = _normalize_header(cell)
            if not norm:
                continue
            for field, aliases in HEADER_ALIASES.items():
                if field not in match and norm in aliases:
                    match[field] = pos
        if len(match) > len(best):
            best_idx, best = i, match
        if all(f in match for f in REQUIRED_FIELDS) and ("article" in match or "product" in match):
            return i, match

    missing = [f for f in REQUIRED_FIELDS if f not in best]
    if "article" not in best and "product" not in best:
        missing.append("article/product")
    raise ValueError(
        f"Packing List Excel — couldn't find a header row with column(s): {', '.join(missing)}. "
        f"Best-matching row (row {best_idx + 1}): {list(raw.iloc[best_idx]) if best_idx >= 0 else 'none'}. "
        f"Add the new header spelling to HEADER_ALIASES in edf/inbound/excel_extractor.py."
    )


# ═══════════════════════════════════════════════════════════════════════════
# LOT PARSING
# ═══════════════════════════════════════════════════════════════════════════

# "<lot> - <n> bags" — lot ids contain hyphens themselves ("L425-2026"), so
# the separator before the count is the LAST "-" that is followed by a number
# and the word "bag(s)". Lots are separated by commas/semicolons/newlines.
_LOT_BAGS_RE = re.compile(
    r"(?P<lot>[A-Za-z0-9][^,;\s]*?)\s*[-–:]\s*(?P<bags>\d+(?:,\d{3})*)\s*bags?\b",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T]00:00:00(?:\.0+)?)?$")


def _clean_lot_text(raw) -> str:
    text = s(raw).strip()
    m = _ISO_DATE_RE.match(text)
    if m:  # Excel date cell read back as text -> dd/mm/yyyy (as shown in the sheet)
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return text


def parse_lots(raw, total_bags: int) -> tuple[list[tuple[str, int]], str]:
    """Returns ([(lot, bags), ...], warning). A value with no "lot - N bags"
    pattern becomes ONE (verbatim text, total_bags) entry."""
    text = _clean_lot_text(raw)
    if not text:
        return [("", total_bags)], "Lot Nr. 2 is blank"

    matches = list(_LOT_BAGS_RE.finditer(text))
    if not matches:
        return [(text, total_bags)], ""

    lots = [(m.group("lot").strip(), int(m.group("bags").replace(",", ""))) for m in matches]
    leftover = _LOT_BAGS_RE.sub("", text)
    leftover = re.sub(r"[\s,;/]+", "", leftover)
    warning = f"unparsed text left in Lot Nr. 2 ({text!r})" if leftover else ""
    return lots, warning


# ═══════════════════════════════════════════════════════════════════════════
# EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════

def _parse_number(raw) -> float:
    text = s(raw).strip().replace(",", "")
    return num(text, 0) if text else 0


def _bag_size_kg(raw) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*kg", s(raw), re.IGNORECASE)
    return float(m.group(1)) if m else 0


def extract_packing_list_excel(path: str) -> dict:
    """Returns {"containers": [one entry per sheet row], "warnings": [...]}.
    Each container entry: bl_no, container_id, container_type, product,
    bags (total), lots [(lot, bags), ...], quantity_mt, bag_size_kg."""
    raw = pd.read_excel(path, header=None, dtype=str)
    raw = raw.where(raw.notna(), "")
    header_idx, cols = _find_header_row(raw)

    def cell(row, field):
        pos = cols.get(field)
        return row.iloc[pos] if pos is not None else ""

    containers, warnings = [], []
    for i in range(header_idx + 1, len(raw)):
        row = raw.iloc[i]
        container_raw = s(cell(row, "container")).strip()
        if not container_raw:
            continue  # blank / spacer / footer row

        cid, _ = fix_container_id(container_raw)
        total_bags = int(_parse_number(cell(row, "bags")))
        lots, lot_warning = parse_lots(cell(row, "lot"), total_bags)
        if lot_warning:
            warnings.append(f"[!]  LOT — {cid}: {lot_warning}")

        containers.append({
            "bl_no":          s(cell(row, "bl_no")).strip(),
            "container_id":   cid,
            "container_type": s(cell(row, "container_type")).strip(),
            # Article number is the primary value; Product column only when
            # there is no Article number column (or this row's cell is blank).
            "product":        s(cell(row, "article")).strip() or s(cell(row, "product")).strip(),
            "bags":           total_bags,
            "lots":           lots,
            "quantity_mt":    _parse_number(cell(row, "quantity_mt")),
            "bag_size_kg":    _bag_size_kg(cell(row, "bag_size")),
        })

    return {"containers": containers, "warnings": warnings}
