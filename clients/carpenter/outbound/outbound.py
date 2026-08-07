"""
outbound.py — Carpenter Outbound business logic (pure, no I/O).

Turns the per-row extraction from scanner.py into the final output rows:
    Reference  <- TCS REF (fallback: CONSIGNEE), distinct values "/"-joined
    Doc Type   <- INCO: contains "DDP" -> IMAH, contains "DAP" -> T1, else ""

Rows are grouped by Doc Type because a single Excel row can only hold one
Doc Type value. When every source row shares the same Doc Type (the normal
case), this collapses to exactly one output row. If a screenshot genuinely
mixes DAP and DDP rows, one output row is produced per Doc Type instead of
silently dropping one.
"""


def _doc_type(inco: str) -> str:
    upper = inco.strip().upper()
    if "DDP" in upper:
        return "IMAH"
    if "DAP" in upper:
        return "T1"
    return ""


def _reference(row: dict) -> str:
    return row.get("tcs_ref") or row.get("consignee") or ""


def build_rows(raw_rows: list[dict]) -> list[dict]:
    """
    raw_rows: [{"inco": str, "tcs_ref": str, "consignee": str}, ...]
    Returns:  [{"reference": str, "doc_type": str}, ...] one row per distinct
              Doc Type found, with distinct references "/"-joined.
    """
    groups: dict[str, list[str]] = {}
    order: list[str] = []

    for row in raw_rows:
        doc_type = _doc_type(row.get("inco", ""))
        ref = _reference(row).strip()
        if not ref:
            continue
        if doc_type not in groups:
            groups[doc_type] = []
            order.append(doc_type)
        if ref not in groups[doc_type]:
            groups[doc_type].append(ref)

    return [
        {"reference": "/".join(groups[doc_type]), "doc_type": doc_type}
        for doc_type in order
    ]
