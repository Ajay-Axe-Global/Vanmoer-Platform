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


def _match_field_columns(row_values, aliases_map=None) -> dict[str, str]:
    """Given one raw row's cell values (a candidate header row), returns
    {canonical_field: actual_cell_text} for every REQUIRED_FIELDS alias it
    matches — used both to score candidate header rows and, once the real
    one is picked, to build the final field->column mapping."""
    aliases_map = aliases_map or HEADER_ALIASES
    field_to_column: dict[str, str] = {}
    for cell in row_values:
        normalized = _normalize_header(cell)
        if not normalized:
            continue
        for field, aliases in aliases_map.items():
            if field in field_to_column:
                continue
            if normalized in aliases:
                field_to_column[field] = cell
    return field_to_column


def _find_header_row(raw: pd.DataFrame, aliases_map=None,
                     required=REQUIRED_FIELDS) -> tuple[int, dict[str, str]]:
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
        match = _match_field_columns(raw.iloc[i].tolist(), aliases_map)
        if len(match) > len(best_match):
            best_row_idx, best_match = i, match
        if all(f in match for f in required):
            break  # found a row matching everything — no need to scan further

    missing = [f for f in required if f not in best_match]
    if missing:
        scanned_rows = [raw.iloc[i].tolist() for i in range(min(HEADER_SCAN_ROWS, len(raw)))]
        raise ValueError(
            f"Excel Packing List — couldn't find a header row matching column(s): {', '.join(missing)}. "
            f"Best-matching row (row {best_row_idx + 1}): {list(raw.iloc[best_row_idx]) if best_row_idx >= 0 else 'none'}. "
            f"First {len(scanned_rows)} row(s) scanned: {scanned_rows}. "
            f"Add the new header spelling to the alias map in excel_extractor.py."
        )
    return best_row_idx, best_match


# ═══════════════════════════════════════════════════════════════════════════
# WEIGHT PARSING
# ═══════════════════════════════════════════════════════════════════════════

def _parse_weight_kg(raw) -> float:
    """Parses a weight cell into KG, handling two real issues seen on this
    sheet:

    1. A cell can be stored as literal TEXT with a European decimal comma
       (e.g. "0,782" typed directly, not a real Excel number) — doc_common's
       num() only recognizes "." as a decimal marker, so a raw comma value
       fell through its int() fallback, raised, and silently returned 0
       (looked like the weight vanished entirely). Fixed here by
       normalizing comma-as-decimal to a period BEFORE calling num() — but
       only when there's no "." already present (a value with both, e.g.
       "1.234,56", is thousands-dot + decimal-comma and the dot is stripped
       instead).

    2. Once parsed, a value under 1 (e.g. 0.782) is a metric-ton figure on
       this sheet, not KG — a bundle can never genuinely weigh under 1 KG —
       so it's converted to KG by ×1000, the same "small decimal = MT"
       convention already used for the MBL and NNRC Packing List. A value
       >= 1 (e.g. 7502) is trusted as already being KG, unchanged — this
       matches the confirmed-correct earlier samples on this same column
       and is NOT touched by this conversion.
    """
    text = s(raw).strip()
    if not text:
        return 0

    if "," in text:
        if "." in text:
            text = text.replace(".", "")  # thousands-dot, e.g. "1.234,56"
        text = text.replace(",", ".")

    value = num(text, 0)
    if 0 < value < 1:
        value = value * 1000
    return value


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
        gross_weight_kg = _parse_weight_kg(row.get(field_to_column["tot_gross_weight"]))
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


# ═══════════════════════════════════════════════════════════════════════════
# MKK TYPE (warehouse 1147) — MKK Metal Sections Excel Packing List
# ═══════════════════════════════════════════════════════════════════════════
# Columns: Ref | Receiver | Date in | Container | Description | Specification
# | Size x thickness | Length | TOTAL (bundles) | Pcs/bdl | unit | TOT (weight)
# One row per lot (size x thickness) — a container holding 6 sizes gives 6
# output rows, each with its own qty and weight. Qty = TOTAL bundles x Pcs/bdl
# (e.g. 1 x 34 = 34, 3 x 3 = 9).

MKK_HEADER_ALIASES: dict[str, list[str]] = {
    "ref":            ["ref", "reference"],
    "receiver":       ["receiver", "consignee"],
    "container":      ["container", "contain", "container no", "container number", "container nr"],
    "description":    ["description", "descript", "desc"],
    "specification":  ["specification", "specific", "spec"],
    "size_thickness": ["size x thickness", "size x thick", "size x", "size thickness",
                       "size and thickness", "size"],
    "total_bndls":    ["total", "total bndls", "tot bndls", "total bundles", "tot bundles", "bundles"],
    "pcs_per_bdl":    ["pcs bdl", "pcs per bdl", "pieces bdl", "pieces per bundle"],
    "tot_weight":     ["tot", "tot weight", "total weight", "tot gross weight", "total gross weight",
                       "tot net weight", "total net weight", "tot gross wt", "total gross wt"],
}

MKK_REQUIRED_FIELDS = (
    "ref", "receiver", "container", "specification", "size_thickness",
    "total_bndls", "pcs_per_bdl", "tot_weight",
)


def _clean_cell(raw) -> str:
    """Collapses in-cell line breaks/extra spaces ("PL10030\nLOT 1\nBIS 4"
    -> "PL10030 LOT 1 BIS 4"); NaN -> ""."""
    text = s(raw).strip()
    if text.lower() == "nan":
        return ""
    return re.sub(r"\s+", " ", text)


def _parse_number(raw) -> float:
    """Number cell that may be text with a European decimal comma."""
    text = _clean_cell(raw)
    if not text:
        return 0
    if "," in text:
        if "." in text:
            text = text.replace(".", "")
        text = text.replace(",", ".")
    return num(text, 0)


def extract_packing_list_mkk(path: str) -> dict:
    raw = pd.read_excel(path, header=None, dtype=str)
    header_row_idx, cols = _find_header_row(raw, MKK_HEADER_ALIASES, MKK_REQUIRED_FIELDS)

    df = raw.iloc[header_row_idx + 1:].copy()
    df.columns = raw.iloc[header_row_idx]
    df = df.reset_index(drop=True)

    parsed = []
    sheet_total_weight = 0.0
    sheet_total_bundles = 0.0
    last = {"ref": "", "receiver": "", "container": "", "description": "", "specification": ""}
    for _, row in df.iterrows():
        get = lambda f: _clean_cell(row.get(cols[f])) if f in cols else ""
        bundles = _parse_number(row.get(cols["total_bndls"]))
        pcs_per_bdl = _parse_number(row.get(cols["pcs_per_bdl"]))
        weight = _parse_number(row.get(cols["tot_weight"]))
        # Lot No — a wrapped cell ("260X140" / "X10") must not keep the break.
        size = re.sub(r"\s+", "", get("size_thickness")).upper()
        container_raw = get("container")

        if not pcs_per_bdl and not size:
            # Grand-total line (TOTAL bundles / TOT weight, no lot data).
            if weight and not container_raw:
                sheet_total_weight = weight
                sheet_total_bundles = bundles
            continue

        # Merged cells — a value printed once for several rows reads as
        # blank on the rows beneath it; carry the last one down.
        for f in last:
            v = get(f)
            if v:
                last[f] = v
        if not last["container"]:
            continue

        parsed.append({
            "container": last["container"],
            "ref": last["ref"], "receiver": last["receiver"],
            "description": last["description"], "specification": last["specification"],
            "size": size, "bundles": bundles, "pcs_per_bdl": pcs_per_bdl, "weight": weight,
        })

    # TOT unit: the sheet shows e.g. "2,000" / "26,440" — that is either
    # tonnes with a decimal comma (2.000 MT) or KG with a thousands comma
    # (2000 KG), depending on how the cell is stored. A container's lot
    # weights summing under 100 can only be tonnes (a 40' container of steel
    # is ~26 t), so scale the whole sheet x1000 in that case — one decision
    # for the sheet, never per row.
    total_raw = sum(p["weight"] for p in parsed)
    factor = 1000 if 0 < total_raw < 100 else 1
    weight_unit = "MT" if factor == 1000 else "KG"

    containers: dict[str, list[dict]] = {}
    for p in parsed:
        cid, _ = fix_container_id(p["container"].replace(" ", ""))
        weight_kg = round(p["weight"] * factor, 3)
        if weight_kg == int(weight_kg):
            weight_kg = int(weight_kg)
        pieces = int(round(p["bundles"] * p["pcs_per_bdl"]))
        containers.setdefault(cid, []).append({
            "product":         p["specification"],
            "description":     p["description"],
            # Lot No = Size x thickness (e.g. "50X30X5", "260X140X10")
            "batch_no":        p["size"],
            "pieces":          pieces,
            "pallet_count":    int(round(p["bundles"])),
            # Only one weight column on this sheet — Net and Gross both use it.
            "net_weight_kg":   weight_kg,
            "gross_weight_kg": weight_kg,
            "net_weight_mt":   weight_kg / 1000,
            "reference":       p["ref"],
            "receiver":        p["receiver"],
        })

    return {
        "destination_country": "",
        "pl_type": "MKK",
        "mkk_weight_unit": weight_unit,
        # Sheet's own total row — checked against the lot rows in validate().
        "grand_total_net_mt": round(sheet_total_weight * factor / 1000, 3) if sheet_total_weight else 0,
        "sheet_total_bundles": int(round(sheet_total_bundles)),
        "containers": [
            {"container_no": cid, "bundles": bundles}
            for cid, bundles in containers.items()
        ],
    }
