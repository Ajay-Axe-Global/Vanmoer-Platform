"""
EDF Inbound (ED&F Man) — validation and row-building.

Source is the Excel Packing List ONLY (see excel_extractor.py) — no MBL, no
PDF, no Gemini. Reference, Shipping Line, Ship Name and ETA Date are all UI
inputs applied uniformly to every row. Shipping Line is picked from the same
display list every other client's MBL-derived Shipping Line column uses
(Swiss's CARRIER_DISPLAY_MAP values, plus "Other"), so the spelling is
identical across clients.

One output row per (container, lot): a container whose "Lot Nr. 2" holds
several "lot - N bags" entries produces several rows, each with its own lot
and bag count. Seal No is not on the Excel sheet: it comes from the optional
Arrival Notice PDF (see arrival_notice.py), matched by container number, and
is blank for any container the notice doesn't list.
"""

from clients.swiss.inbound.extractor import CARRIER_DISPLAY_MAP, carrier_display
from helpers.doc_common import normalize_container_type, s

# The UI dropdown shows the short alias only (MSC, HMM, HAPAG-LLOYD, ...); the
# output Excel's Shipping Line column gets the full display string for that
# alias via carrier_display() — identical to every other client's output.
SHIPPING_LINE_OPTIONS = list(CARRIER_DISPLAY_MAP.keys()) + ["Other"]

# Sanity tolerance for bags x bag-size vs "Quantity (MT) per FCL".
QUANTITY_TOLERANCE = 0.02


def validate(packing_list: dict) -> list[str]:
    containers = packing_list.get("containers", [])
    results = []

    if not containers:
        return ["[X]  LINE ITEMS — no containers found in the Packing List Excel"]

    n_lots = sum(len(c["lots"]) for c in containers)
    results.append(f"[OK] CONTAINERS — {len(containers)} container(s), {n_lots} lot row(s) from the Packing List")

    seen = set()
    for c in containers:
        cid = c["container_id"]
        if cid in seen:
            results.append(f"[!]  CONTAINER — {cid} appears on more than one sheet row")
        seen.add(cid)

        if len(cid) != 11 or not (cid[:4].isalpha() and cid[4:].isdigit()):
            results.append(f"[!]  CONTAINER — {cid!r} is not a 4-letter + 7-digit container number")

        if not c["bl_no"]:
            results.append(f"[!]  B/L — {cid}: blank B/L on the sheet")

        lot_sum = sum(b for _, b in c["lots"])
        if c["bags"] and lot_sum != c["bags"]:
            results.append(f"[!]  BAGS — {cid}: lot bags sum({lot_sum}) vs Number of bags({c['bags']})")
        elif c["bags"]:
            results.append(f"[OK] BAGS — {cid}: {len(c['lots'])} lot(s) = {c['bags']} bags")

        if c["quantity_mt"] and c["bag_size_kg"] and c["bags"]:
            expected_mt = c["bags"] * c["bag_size_kg"] / 1000
            if abs(expected_mt - c["quantity_mt"]) > c["quantity_mt"] * QUANTITY_TOLERANCE:
                results.append(f"[!]  QUANTITY — {cid}: {c['bags']} bags x {c['bag_size_kg']:g} kg = "
                                f"{expected_mt:g} MT vs sheet Quantity {c['quantity_mt']:g} MT")

    results.extend(packing_list.get("warnings", []))
    return results


def validate_seals(packing_list: dict, arrival_notice: dict | None) -> list[str]:
    """Seal cross-check between the Packing List's containers and the Arrival
    Notice. A container with no seal in the notice is a warning (blank Seal
    No), never a failure — the notice is optional and the Excel still
    generates."""
    if arrival_notice is None:
        return ["[!]  SEAL — no Arrival Notice uploaded, Seal No left blank"]

    seals = arrival_notice["seals"]
    results = list(arrival_notice.get("warnings", []))
    packing_ids = [c["container_id"] for c in packing_list.get("containers", [])]

    matched = [cid for cid in packing_ids if cid in seals]
    if matched:
        results.append(f"[OK] SEAL — {len(matched)}/{len(packing_ids)} container(s) matched to the Arrival Notice")
    for cid in packing_ids:
        if cid not in seals:
            results.append(f"[!]  SEAL — {cid}: not found in the Arrival Notice, Seal No left blank")
    for cid in seals:
        if cid not in packing_ids:
            results.append(f"[!]  SEAL — {cid}: in the Arrival Notice but not in the Packing List")
    return results


def build_rows(packing_list: dict, reference: str, shipping_line: str, ship_name: str,
               eta_date: str, seals: dict | None = None) -> list[dict]:
    reference = s(reference).strip()
    seals = seals or {}
    rows = []
    for c in packing_list.get("containers", []):
        cid = c["container_id"]
        container_type = normalize_container_type(c["container_type"]) if c["container_type"] else ""
        mbl_container = f"{c['bl_no']}/{cid}" if c["bl_no"] else cid

        for lot, bags in c["lots"]:
            rows.append({
                "reference":     reference,
                "container_no":  cid,
                "mbl_container": mbl_container,
                "seal_no":       seals.get(cid, ""),
                "container_type": container_type,
                "shipping_line": shipping_line,
                "ship_name":     ship_name,
                "product":       c["product"],
                "lot_no":        lot,
                "bags_qty":      bags,
                "eta_date":      eta_date,
            })
    return rows
