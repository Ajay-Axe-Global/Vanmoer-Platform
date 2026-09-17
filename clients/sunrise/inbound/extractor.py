"""
Sunrise Inbound — MBL extraction, cross-document validation, and
row-building. Packing List extraction (Excel-only) lives in
excel_extractor.py — see that module's docstring for the sheet layout and
the per-lot grouping/summing rule.

MBL extraction reuses Swiss Inbound's already-proven carrier-identification
pipeline and per-carrier prompt library WHOLESALE (imported, never
duplicated/forked) — same convention as clients/vmr/inbound/extractor.py.
Swiss's extract_mbl() already returns exactly the shape this client needs
(mbl_no, port_of_loading, containers[{id, seal, type}]) with no
Sunrise-specific fields to add on top (unlike VMR, which needed an extra
"product" field on the MBL — Sunrise's Product/Lot/Bags/Weight all come from
the Excel Packing List instead, never the MBL), so it's called directly
rather than re-exported through a local wrapper.
"""

from clients.swiss.inbound.extractor import (
    CARRIER_MBL_PROMPTS,
    extract_mbl,
)
from helpers.doc_common import (
    get_country_code,
    normalize_container_type,
    num,
    s,
    spaced_container_type,
)

__all__ = ["extract_mbl", "CARRIER_MBL_PROMPTS", "validate", "build_rows"]


# ═══════════════════════════════════════════════════════════════════════════
# FUZZY CONTAINER-ID MATCHING — same OCR-tolerant matching used by Swiss/VMR
# Inbound, copied rather than imported since it's a small, self-contained
# utility (see clients/vmr/inbound/extractor.py's own copy for the same
# rationale: importing it would create a confusing cross-client dependency
# for something this tiny).
# ═══════════════════════════════════════════════════════════════════════════

def _fuzzy_match_container(cid: str, reference_cids: set[str], threshold: int = 2) -> str:
    """If cid isn't in reference_cids, find the closest match within
    `threshold` character differences (covers OCR/typing confusions like
    U/Y, O/C/0, H/U, 1/6/l, B/8, S/5, Z/2 between the MBL and the Excel
    Packing List's "Intake reference" readings of the same container).
    Returns the matched id, or the original cid if no close match is found."""
    if cid in reference_cids or not cid:
        return cid

    best_match = None
    best_dist = threshold + 1
    for ref_cid in reference_cids:
        if len(ref_cid) != len(cid):
            continue
        dist = sum(1 for a, b in zip(cid, ref_cid) if a != b)
        if dist < best_dist:
            best_dist = dist
            best_match = ref_cid

    return best_match if best_match and best_dist <= threshold else cid


# ═══════════════════════════════════════════════════════════════════════════
# CROSS-DOCUMENT VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

def validate(mbl: dict, packing_lists: list[dict]) -> list[str]:
    results = []

    carrier = s(mbl.get("carrier")).strip()
    if carrier:
        if carrier in CARRIER_MBL_PROMPTS:
            results.append(f"[OK] CARRIER — {carrier} (carrier-specific MBL prompt)")
        else:
            results.append(f"[!]  CARRIER — {carrier} (generic fallback MBL prompt — not yet carrier-tuned, "
                            f"verify every MBL field manually)")

    if s(mbl.get("mbl_no")).strip():
        results.append(f"[OK] MBL No — {s(mbl.get('mbl_no')).strip()}")
    else:
        results.append("[X]  MBL No — not found on the MBL")

    mbl_cids = {c["id"] for c in mbl.get("containers", []) if c.get("id")}
    if not mbl_cids:
        results.append("[X]  CONTAINERS — no containers extracted from the MBL")
    else:
        results.append(f"[OK] CONTAINERS — {len(mbl_cids)} found on the MBL")

    if not packing_lists:
        results.append("[X]  PACKING LIST — no Excel Packing List file(s) uploaded")
        return results

    pkl_cids = {pl["container_no"] for pl in packing_lists if pl.get("container_no")}
    common = mbl_cids & pkl_cids
    only_mbl = mbl_cids - pkl_cids
    only_pkl = pkl_cids - mbl_cids

    if common:
        results.append(f"[OK] CONTAINERS — {len(common)} matched across MBL & Packing List(s)")
    for c in sorted(only_mbl):
        results.append(f"[!]  CONTAINER — {c} only in MBL (not in any Packing List upload)")
    for c in sorted(only_pkl):
        results.append(f"[!]  CONTAINER — {c} only in a Packing List upload (not in MBL)")

    for pl in packing_lists:
        if not pl.get("lots"):
            results.append(f"[X]  PACKING LIST — container {pl.get('container_no', '?')}'s Excel file has "
                            f"no lot rows (check the \"Bag quantity\"/\"Batch / lot #\" columns)")

    total_lots = sum(len(pl.get("lots", [])) for pl in packing_lists)
    total_bags = sum(lot["bags_qty"] for pl in packing_lists for lot in pl.get("lots", []))
    results.append(f"[OK] LOTS — {total_lots} lot row(s), {total_bags} total bags, across "
                    f"{len(packing_lists)} container(s)")

    return results


# ═══════════════════════════════════════════════════════════════════════════
# ROW BUILDER
# ═══════════════════════════════════════════════════════════════════════════

def build_rows(mbl: dict, packing_lists: list[dict], reference: str = "", eta_date: str = "") -> list[dict]:
    reference = s(reference).strip()

    # Origin (Port of Loading) -> country code, same convention as every
    # other client (Sabic/Vinmar/Emvia Inbound) — NOT destination.
    country_code = get_country_code(s(mbl.get("port_of_loading")).strip())

    mbl_map = {c["id"]: c for c in mbl.get("containers", []) if c.get("id")}
    mbl_cids = set(mbl_map.keys())

    rows = []
    for pl in packing_lists:
        cid = s(pl.get("container_no")).strip()
        matched_cid = _fuzzy_match_container(cid, mbl_cids)
        mbl_entry = mbl_map.get(matched_cid, {})

        # Fall back to the shipment-wide "container_type" (some carrier
        # prompts state the size once for the whole shipment and leave each
        # container's own "type" blank — see swiss/inbound/extractor.py's
        # RETURN_SCHEMA / GENERIC_MBL_PROMPT for why).
        raw_type = s(mbl_entry.get("type")).strip() or s(mbl.get("container_type")).strip()
        container_type = normalize_container_type(raw_type) if raw_type else ""
        container_type2 = spaced_container_type(container_type) if container_type else ""
        seal_no = s(mbl_entry.get("seal")).strip()

        for lot in pl.get("lots", []):
            net_weight = num(lot.get("net_weight_kg"), 0)
            rows.append({
                "reference":       reference,
                "container_no":    matched_cid,
                "container_ref":   f"{matched_cid}/{reference}",
                "container_type":  container_type,
                "container_type2": container_type2,
                "seal_no":         seal_no,
                "country_code":    country_code,
                "product":         lot.get("product", ""),
                "lot_no":          lot.get("lot_no", ""),
                "bags_qty":        num(lot.get("bags_qty"), 0),
                # No source document states this — always 0, per client
                # instruction.
                "pallet_count":    0,
                "net_weight":      net_weight,
                # Excel Packing List states only ONE weight figure ("Weight
                # kg") — Gross Weight mirrors Net Weight, per client
                # instruction (same convention as Emvia's Excel path).
                "gross_weight":    net_weight,
                "eta_date":        eta_date,
            })

    return rows
