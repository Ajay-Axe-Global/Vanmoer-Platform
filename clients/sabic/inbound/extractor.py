
"""
SABIC Inbound — Extraction, validation, and row-building.

Gemini returns packing list as nested containers→items structure.
Python flattens it to rows, cross-checks with MBL, validates, and builds output.
"""

import json
import os
import re

from helpers.gemini_client import call_gemini

# ═══════════════════════════════════════════════════════════════════════════
# COUNTRY CODE LOOKUP
# ═══════════════════════════════════════════════════════════════════════════

PORT_COUNTRY_MAP = {
    "KING ABDULLAH": "SA", "JEDDAH": "SA", "JUBAIL": "SA", "YANBU": "SA",
    "DAMMAM": "SA", "RABIGH": "SA", "SAUDI ARABIA": "SA", "RIYADH": "SA",
    "PUSAN": "KR", "BUSAN": "KR", "INCHEON": "KR", "KOREA": "KR",
    "HOUSTON": "US", "LOS ANGELES": "US", "NEW YORK": "US", "SAVANNAH": "US",
    "CHARLESTON": "US", "LONG BEACH": "US", "UNITED STATES": "US",
    "ANTWERP": "BE", "BELGIUM": "BE",
    "ROTTERDAM": "NL", "NETHERLANDS": "NL",
    "HAMBURG": "DE", "BREMERHAVEN": "DE", "GERMANY": "DE",
    "SHANGHAI": "CN", "QINGDAO": "CN", "NINGBO": "CN", "CHINA": "CN",
    "SINGAPORE": "SG",
    "MUMBAI": "IN", "NHAVA SHEVA": "IN", "INDIA": "IN",
    "TOKYO": "JP", "YOKOHAMA": "JP", "KOBE": "JP", "JAPAN": "JP",
    "FELIXSTOWE": "GB", "SOUTHAMPTON": "GB", "UNITED KINGDOM": "GB",
    "LE HAVRE": "FR", "MARSEILLE": "FR", "FRANCE": "FR",
    "BARCELONA": "ES", "VALENCIA": "ES", "SPAIN": "ES",
    "GENOA": "IT", "GIOIA TAURO": "IT", "ITALY": "IT",
    "PIRAEUS": "GR", "GREECE": "GR",
    "ISTANBUL": "TR", "MERSIN": "TR", "TURKEY": "TR",
    "DURBAN": "ZA", "CAPE TOWN": "ZA", "SOUTH AFRICA": "ZA",
    "JEBEL ALI": "AE", "DUBAI": "AE", "ABU DHABI": "AE",
    "MUNDRA": "IN", "CHENNAI": "IN",
    "LAEM CHABANG": "TH", "THAILAND": "TH",
    "PORT KLANG": "MY", "MALAYSIA": "MY",
    "JAKARTA": "ID", "INDONESIA": "ID",
    "HO CHI MINH": "VN", "VIETNAM": "VN",
    "KARACHI": "PK", "PAKISTAN": "PK",
    "COLOMBO": "LK", "SRI LANKA": "LK",
}


def get_country_code(port_of_loading: str) -> str:
    upper = port_of_loading.upper().strip()
    for key, code in PORT_COUNTRY_MAP.items():
        if key in upper:
            return code
    return "??"


# ═══════════════════════════════════════════════════════════════════════════
# CONTAINER ID NORMALIZATION
# ═══════════════════════════════════════════════════════════════════════════

_CONTAINER_RE = re.compile(r'^([A-Z]{4})(\d{7})')


def _fix_container_id(raw: str, existing_seal: str = "") -> tuple[str, str]:
    cleaned = raw.replace(" ", "").strip().upper()
    m = _CONTAINER_RE.match(cleaned)
    if not m:
        return cleaned, existing_seal
    container_id = m.group(1) + m.group(2)
    leftover = cleaned[len(container_id):]
    if leftover and leftover.isdigit() and not existing_seal:
        return container_id, leftover
    return container_id, existing_seal


# ═══════════════════════════════════════════════════════════════════════════
# DEBUG JSON DUMP
# ═══════════════════════════════════════════════════════════════════════════

def _dump_json(pdf_path: str, suffix: str, data):
    try:
        out = os.path.join(os.path.dirname(pdf_path), suffix)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# GEMINI PROMPTS
# ═══════════════════════════════════════════════════════════════════════════

MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields from this Master Bill of Lading (MBL / Sea Waybill) PDF and return ONLY a JSON object — no markdown, no explanation.

RULES:
- "ref_nos": Find ALL Sales Order / STO numbers. They appear with ONLY these labels:
    "SALES ORDER NO.:XXXXXXX" or "STO NO : XXXXXXX" or "STO NO:XXXXXXX"
  ⚠️ ONLY extract values that follow these exact labels. Do NOT extract:
    - "OBD#" values (that is a delivery reference, NOT a sales order)
    - "DELIVERY NO." values
    - Any other unlabeled numbers
  Sales order numbers always start with "450..." (10 digits). If a number does not
  start with "450", it is NOT a sales order — do not include it.
  Look across ALL rider pages. Return as array.
- "delivery_no": The Delivery Number ("DELIVERY NO.:XXXXXXX").
- "port_of_loading": Port of Loading.
- "port_of_discharge": Port of Discharge.
- "vessel": Vessel and Voyage number.
- "mbl_no": The Sea Waybill / MBL number (top right).
- "containers": Array of EVERY container from ALL rider pages. For each:
  - "id": Container number — exactly 4 letters + 7 digits = 11 characters. Remove spaces. NEVER include seal.
  - "type": Container type (e.g. "40' HIGH CUBE")
  - "seal": Seal number (separate field)
  - "bags": Number of bags from the description (e.g. from "1020 BAG(S) LLDPE 318BJ 149" extract 1020)
  - "net_weight_mt": Net weight in MT from description (e.g. from "NET WEIGHT:25.500 MT" extract 25.5)

Return:
{
  "mbl_no": "string",
  "ref_nos": ["string"],
  "delivery_no": "string",
  "port_of_loading": "string",
  "port_of_discharge": "string",
  "vessel": "string",
  "containers": [
    {"id": "string", "type": "string", "seal": "string", "bags": number, "net_weight_mt": number}
  ]
}"""


PKG_LIST_PROMPT = """You are a shipping-document data extractor. Extract all data from this SABIC Packing List PDF and return ONLY a JSON object — no markdown, no explanation.

══════════════════════════════════════════
HEADER FIELDS
══════════════════════════════════════════
- Order/STO: appears as "Sharq Order/STO:" or "Petrokemya Order/STO:" etc.
- Delivery: appears as "Sharq Delivery:" or "Petrokemya Delivery:" etc But Need to take only Sabic Delivery According to Different Layout.
- Sabic PO: "Sabic PO:" — base number only (e.g. "4506618575" not "4506618575 000010").
- Sabic Delivery: "Sabic Delivery:" - this is the PRIMARY delivery number.

══════════════════════════════════════════
TABLE STRUCTURE
══════════════════════════════════════════
Columns: Container ID / Seal No. | Material | PKG CODE | Batch | Unit | Bags | Gross Weight | Verified Gross Mass | Net Weight

The first column has TWO values stacked:
  Line 1 → Container ID (4 uppercase letters + 7 digits = 11 chars, e.g. BEAU5848007)
  Line 2 → Seal No. (6-7 digits, e.g. 1072481)

Return a FLAT array of rows, ONE entry per table row, in EXACT top-to-bottom
physical order as printed — across all pages, ignoring page breaks entirely.
Do NOT group rows into containers. Do NOT decide which container an orphan
row belongs to. Just transcribe each row exactly as printed, in order.

For each row:
- "container_id": the Container ID printed on THIS row. Empty string "" if
  no Container ID is printed on this row (do not copy one down from above).
- "seal": the Seal No. printed on THIS row. Empty string "" if not printed here.
- "has_vgm": true if this row has a value in the Verified Gross Mass column,
  false if that column is blank on this row.
- "product": Material name (e.g. "LLDPE 318BJ 149")
- "pkg_code": PKG CODE column value (e.g. "149")
- "lot": Batch column value (e.g. "0061861590")
- "pallet_qty": The NUMERIC part of the Unit column (e.g. "17 PAL" → 17, "1 PAL" → 1).
  This is the number of PALLETS. It can be smaller than bags (e.g. 17 pallets, 1020 bags)
  or equal to bags when each pallet holds one big bag (e.g. 36 pallets, 36 bags).
  If the Unit column shows only "MT" (no number + PAL), set pallet_qty to 0.
- "bags": The BAGS column — total number of bags (e.g. 1020, 360, 980).
  ⚠️ bags is ALWAYS LARGER than pallet_qty. If bags < pallet_qty, you swapped them — fix it.
- "gross_weight": Gross Weight number only (e.g. 25.993)
- "net_weight": Net Weight number only (e.g. 25.5)
- "weight_unit": The unit shown in the weight columns. "MT" if weights say "25.9930 MT",
  "KG" if weights say "25993 KG". Default to "MT" if unclear.

NOTE : This is the single most common extraction mistake: attaching a no-VGM row
to the container printed BELOW it instead of the container that opened ABOVE
it . You Must place the that Row to the Last container or Above container.
══════════════════════════════════════════
ALTERNATIVE TABLE LAYOUTS
══════════════════════════════════════════
Some packing lists (e.g. from KNC / Korea Nexlene) have a DIFFERENT column layout:
  CONTAINER ID | SEAL NO. | MATERIAL | Grade | BATCH | UNIT | BAGS | GROSS WEIGHT | NET WEIGHT

Key differences from the standard layout:
- Container ID and Seal No. are in SEPARATE columns (not stacked vertically).
- There is a "Grade" column (e.g. "OS") — map this to "pkg_code", NOT to "lot".
- The "BATCH" column (e.g. "C902Q7C501") — this is the LOT number. Map to "lot".
- There is NO "Verified Gross Mass" column — set has_vgm = true for ALL rows.
- "UNIT" column shows "MT" (unit of measure) — this is NOT the pkg_type.
  Set pkg_type = "PAL" and unit = 0 when UNIT shows "MT".

How to detect: if the column headers include "Grade" and "BATCH" as separate columns,
OR if the seal numbers start with "FJ" or "M" followed by digits, use this mapping.

══════════════════════════════════════════
WORKED EXAMPLE 1 — mid-page (the case most often gotten wrong)
══════════════════════════════════════════
    TXGU4257926   LLDPE 318BJ 149   0052220632   4 PAL   240   6.116 MT   29.755 MT   6.0 MT
      1072586
                  LLDPE 318BJ 149   0061144594   13 PAL  780   19.877 MT              19.5 MT
    MSMU8152963   LLDPE 318BJ 149   0052220632   6 PAL   360   9.174 MT   29.755 MT   9.0 MT
      1072489

Row-by-row:
  Row 1: TXGU4257926, HAS VGM (29.755 MT) → open new container TXGU4257926. current = TXGU4257926.
  Row 2: no container ID, NO VGM → continuation of current (TXGU4257926). Append here.
  Row 3: MSMU8152963, HAS VGM (29.755 MT) → open new container MSMU8152963. current = MSMU8152963.

══════════════════════════════════════════
OUTPUT FORMAT
══════════════════════════════════════════
{
  "delivery_no": "string",
  "sto": "string",
  "sabic_po": "string",
  "sabic_delivery": "string",
  "rows": [
    {
      "container_id": "string or empty",
      "seal": "string or empty",
      "has_vgm": true,
      "product": "string",
      "pkg_code": "string",
      "lot": "string",
      "pallet_qty": 17,
      "bags": 1020,
      "gross_weight": 25.993,
      "net_weight": 25.5,
      "weight_unit": "MT"
    }
  ]
}"""

INVOICE_PROMPT = """You are a shipping-document data extractor. Extract fields from this Commercial Invoice PDF. Return ONLY JSON — no markdown.

- "invoice_no": Invoice Number.
- "sales_ref": Sales Ref base number before slash (e.g. "4506618575" from "4506618575/0010").
- "delivery_no": Delivery No base number before slash.
- "product": Product description from line items.
- "shipment_no": Shipment Number.
- "qty": Total quantity (number).
- "unit": Unit of measure (e.g. "MT").

{
  "invoice_no": "string",
  "sales_ref": "string",
  "delivery_no": "string",
  "product": "string",
  "shipment_no": "string",
  "qty": number,
  "unit": "string"
}"""


# ═══════════════════════════════════════════════════════════════════════════
# EXTRACTION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════
def _to_kg(value, unit="MT"):
    """Convert weight to KG. MT × 1000, KG passthrough."""
    val = _num(value, 0)
    if not val:
        return 0
    if unit.upper().strip() == "MT":
        return round(val * 1000, 3)
    return round(val, 3)


def _determine_pkg_type(net_weight_kg, bags):
    """net_weight_kg / bags > 50 → 'Big Bags', else 'Bags'."""
    if not bags or bags == 0:
        return "Bags"
    per_bag = net_weight_kg / bags
    return "Big Bags" if per_bag > 50 else "Bags"


def extract_mbl(pdf_path: str) -> dict:
    data = call_gemini(MBL_PROMPT, pdf_path=pdf_path, max_output_tokens=16384)
    _dump_json(pdf_path, "mbl_raw.json", data)
    for c in data.get("containers", []):
        cid, seal = _fix_container_id(c.get("id", ""), c.get("seal", ""))
        c["id"] = cid
        c["seal"] = seal
    _dump_json(pdf_path, "mbl.json", data)
    return data


def _num(value, default=0):
    """
    Coerce a Gemini-extracted numeric field to a number, safely handling
    None, missing keys, empty strings, and stray non-numeric junk.
    Use this everywhere a number comes straight from Gemini's JSON —
    dict.get(key, default) does NOT protect against an explicit `null`.
    """
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value
    try:
        s = str(value).strip()
        if not s:
            return default
        return float(s) if "." in s else int(s)
    except (TypeError, ValueError):
        return default

def extract_packing_list(pdf_path: str) -> dict:
    """
    Gemini returns FLAT, order-preserved rows[] (one row = one table line,
    transcribed as-is, no grouping decisions). Python groups them into
    containers deterministically using has_vgm.
    """
    data = call_gemini(PKG_LIST_PROMPT, pdf_path=pdf_path, max_output_tokens=16384)
    _dump_json(pdf_path, "pkg_list_raw.json", data)
 
    flat_lines = []
    current_cid, current_seal = None, None
 
    for row in data.get("rows", []):
        has_vgm = row.get("has_vgm")
        if isinstance(has_vgm, str):
            has_vgm = has_vgm.strip().lower() == "true"
 
        raw_cid = (row.get("container_id") or "").strip()
 
        if has_vgm and raw_cid:
            cid, seal = _fix_container_id(raw_cid, row.get("seal", ""))
            current_cid, current_seal = cid, seal
 
        if current_cid is None:
            continue
 
        # Skip total/summary rows
        product = (row.get("product") or "").strip()
        if product.upper().replace(":", "").strip() in ("TOTAL", "SUB TOTAL", "SUBTOTAL", "GRAND TOTAL"):
            continue
 
        flat_lines.append({
            "container_id": current_cid,
            "seal":         current_seal,
            "product":      product,
            "pkg_code":     row.get("pkg_code", ""),
            "lot":          row.get("lot", ""),
            "pallet_qty":   _num(row.get("pallet_qty"), 0),
            "bags":         _num(row.get("bags"), 0),
            "gross_weight": _num(row.get("gross_weight"), 0),
            "net_weight":   _num(row.get("net_weight"), 0),
            "weight_unit":  (row.get("weight_unit") or "MT").strip().upper(),
        })
 
    result = {
        "delivery_no":    data.get("delivery_no", ""),
        "sto":            data.get("sto", ""),
        "sabic_po":       data.get("sabic_po", ""),
        "sabic_delivery": data.get("sabic_delivery", ""),
        "lines":          flat_lines,
    }
 
    _dump_json(pdf_path, "pkg_list.json", result)
    n_containers = len({ln["container_id"] for ln in flat_lines})
    print(f"  [PKG LIST] Grouped {len(flat_lines)} rows into {n_containers} containers")
    return result


def extract_invoice(pdf_path: str) -> dict:
    data = call_gemini(INVOICE_PROMPT, pdf_path=pdf_path)
    _dump_json(pdf_path, "invoice.json", data)
    return data


# ═══════════════════════════════════════════════════════════════════════════
# MBL CROSS-CHECK — SAFETY NET FOR PAGE-BREAK ORPHANS
# ═══════════════════════════════════════════════════════════════════════════

def cross_check_containers(mbl: dict, pkl: dict) -> dict:
    """
    Use MBL per-container bag counts to detect and fix page-break orphan lots.
    Even with nested format, Gemini can still group a lot under the wrong container.
    This catches it mathematically.
    """
    from collections import OrderedDict

    # Build expected bags per container from MBL
    mbl_expected = {}
    for c in mbl.get("containers", []):
        bags = c.get("bags") or 0
        if bags:
            mbl_expected[c["id"]] = int(bags)

    # Fallback: calculate from totals if MBL doesn't have per-container bags
    if not mbl_expected:
        mbl_containers = [c["id"] for c in mbl.get("containers", [])]
        if mbl_containers:
            total_bags = sum(ln.get("bags", 0) for ln in pkl.get("lines", []))
            num = len(mbl_containers)
            if total_bags and num and total_bags % num == 0:
                per = total_bags // num
                print(f"  [CROSS-CHECK] No per-container bags in MBL. "
                      f"Fallback: {total_bags} ÷ {num} = {per} each")
                for cid in mbl_containers:
                    mbl_expected[cid] = per

    if not mbl_expected:
        print("  [CROSS-CHECK] Cannot determine expected bags — skipping")
        return pkl

    lines = pkl.get("lines", [])
    if not lines:
        return pkl

    # Build container order and line indices
    container_order = []
    container_lines = OrderedDict()
    for i, ln in enumerate(lines):
        cid = ln["container_id"]
        if cid not in container_lines:
            container_order.append(cid)
            container_lines[cid] = []
        container_lines[cid].append(i)

    # Check consecutive pairs — iterative to handle cascades across 3+ containers.
    # A cascade happens when lot A stolen from container 1 → container 2,
    # and lot B stolen from container 2 → container 3, etc.
    MAX_ROUNDS = 5
    total_fixes = 0

    for round_num in range(MAX_ROUNDS):
        # Rebuild indices each round (after mutations from prior round)
        container_order = []
        container_lines_map = OrderedDict()
        for i, ln in enumerate(lines):
            cid = ln["container_id"]
            if cid not in container_lines_map:
                container_order.append(cid)
                container_lines_map[cid] = []
            container_lines_map[cid].append(i)

        fixes_this_round = 0
        for idx in range(len(container_order) - 1):
            under_cid = container_order[idx]
            over_cid = container_order[idx + 1]
            u_exp = mbl_expected.get(under_cid, 0)
            if not u_exp:
                continue

            u_act = sum(lines[i]["bags"] for i in container_lines_map[under_cid])
            o_act = sum(lines[i]["bags"] for i in container_lines_map[over_cid])

            # Under-container is short AND over-container has excess
            if u_act < u_exp and o_act > u_act and container_lines_map[over_cid]:
                fi = container_lines_map[over_cid][0]
                stolen = lines[fi]
                # Only require the under-container to become correct
                if u_act + stolen["bags"] == u_exp:
                    print(f"  [CROSS-CHECK FIX] Lot {stolen['lot']} ({stolen['bags']} bags): "
                          f"{over_cid} → {under_cid}")
                    stolen["container_id"] = under_cid
                    stolen["seal"] = lines[container_lines_map[under_cid][0]]["seal"]
                    container_lines_map[under_cid].append(fi)
                    container_lines_map[over_cid].remove(fi)
                    fixes_this_round += 1

        total_fixes += fixes_this_round
        if fixes_this_round == 0:
            break

    if total_fixes:
        print(f"  [CROSS-CHECK] Fixed {total_fixes} page-break orphan(s)")
    else:
        print(f"  [CROSS-CHECK] All containers match — no fixes needed")

    pkl["lines"] = lines
    return pkl


# ═══════════════════════════════════════════════════════════════════════════
# CROSS-DOCUMENT VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

def _build_ref(ref_nos: list) -> str:
    cleaned = []
    for r in ref_nos:
        base = str(r).split("/")[0].strip()
        if base and base not in cleaned:
            cleaned.append(base)
    return " ".join(cleaned)


def validate(mbl: dict, pkl: dict, inv: dict) -> list[str]:
    results = []

    mbl_ref = _build_ref(mbl.get("ref_nos", []))
    inv_ref = _build_ref([inv.get("sales_ref", "")])
    pkl_ref = _build_ref([pkl.get("sabic_po", "")]) or _build_ref([pkl.get("sto", "")])

    if mbl_ref and inv_ref:
        if mbl_ref == inv_ref:
            results.append(f"[OK] REF — MBL({mbl_ref}) = Invoice({inv_ref})")
        else:
            results.append(f"[X]  REF MISMATCH — MBL({mbl_ref}) vs Invoice({inv_ref})")

    if mbl_ref and pkl_ref:
        if mbl_ref == pkl_ref:
            results.append(f"[OK] REF — MBL({mbl_ref}) = Packing List({pkl_ref})")
        else:
            results.append(f"[X]  REF MISMATCH — MBL({mbl_ref}) vs Packing List({pkl_ref})")

    mbl_del = str(mbl.get("delivery_no", "")).split("/")[0]
    inv_del = str(inv.get("delivery_no", "")).split("/")[0]
    pkl_sabic_del = str(pkl.get("sabic_delivery", "")).split("/")[0]

    if mbl_del and inv_del:
        if mbl_del == inv_del:
            results.append(f"[OK] DELIVERY — MBL({mbl_del}) = Invoice({inv_del})")
        else:
            results.append(f"[X]  DELIVERY MISMATCH — MBL({mbl_del}) vs Invoice({inv_del})")

    if mbl_del and pkl_sabic_del:
        if mbl_del == pkl_sabic_del:
            results.append(f"[OK] DELIVERY — MBL({mbl_del}) = Packing List Sabic({pkl_sabic_del})")
        else:
            results.append(f"[!]  DELIVERY — MBL({mbl_del}) vs Packing List Sabic({pkl_sabic_del})")
    
        # Fallback: check delivery_no from PKG list when sabic_delivery is empty
    pkl_del = str(pkl.get("delivery_no", "")).split("/")[0]
    if mbl_del and pkl_del and not pkl_sabic_del:
        if mbl_del == pkl_del:
            results.append(f"[OK] DELIVERY — MBL({mbl_del}) = Packing List({pkl_del})")
        else:
            results.append(f"[!]  DELIVERY — MBL({mbl_del}) vs Packing List({pkl_del})")

    inv_product = str(inv.get("product", "")).upper()
    pkl_products = set()
    for line in pkl.get("lines", []):
        pkl_products.add(str(line.get("product", "")).upper())
    for p in pkl_products:
        if inv_product and (inv_product in p or p in inv_product):
            results.append(f"[OK] PRODUCT — Invoice({inv_product}) ~ Packing List({p})")
        elif inv_product:
            results.append(f"[!]  PRODUCT — Invoice({inv_product}) vs Packing List({p})")

    mbl_cids = {c["id"] for c in mbl.get("containers", [])}
    pkl_cids = {ln["container_id"] for ln in pkl.get("lines", [])}
    common = mbl_cids & pkl_cids
    only_mbl = mbl_cids - pkl_cids
    only_pkl = pkl_cids - mbl_cids

    if common:
        results.append(f"[OK] CONTAINERS — {len(common)} matched across MBL & Packing List")
    for c in sorted(only_mbl):
        results.append(f"[!]  CONTAINER — {c} only in MBL (not in Packing List)")
    for c in sorted(only_pkl):
        results.append(f"[!]  CONTAINER — {c} only in Packing List (not in MBL)")

    return results


# ═══════════════════════════════════════════════════════════════════════════
# ROW BUILDER
# ═══════════════════════════════════════════════════════════════════════════

def build_rows(mbl: dict, pkl: dict) -> list[dict]:
    ref_no = _build_ref(mbl.get("ref_nos", []))

    delivery_no = (
        pkl.get("sabic_delivery")
        or pkl.get("delivery_no")
        or pkl.get("sto")
        or ""
    )
    delivery_no = str(delivery_no).strip()
    if delivery_no == "None":
        delivery_no = ""
    if not delivery_no:
        delivery_no = str(pkl.get("sto", "")).strip()

    country_code = get_country_code(mbl.get("port_of_loading", ""))

    mbl_map = {}
    for c in mbl.get("containers", []):
        mbl_map[c["id"]] = {"type": c.get("type", ""), "seal": c.get("seal", "")}

    rows = []
    for ln in pkl.get("lines", []):
        cid = ln["container_id"]
        seal = ln.get("seal", "")
        ctype = ""

        if cid in mbl_map:
            ctype = mbl_map[cid].get("type", "")
            if not seal:
                seal = mbl_map[cid].get("seal", "")

        # Convert to KG
        weight_unit = ln.get("weight_unit", "MT")
        net_wt_kg = _to_kg(ln.get("net_weight", 0), weight_unit)
        gross_wt_kg = _to_kg(ln.get("gross_weight", 0), weight_unit)
        bags = _num(ln.get("bags", 0), 0)

        # Bags vs Big Bags
        pkg_type = _determine_pkg_type(net_wt_kg, bags)

        # Pallet qty from extractor
        pallet_qty = _num(ln.get("pallet_qty", 0), 0)

        rows.append({
            "ref_no":          ref_no,
            "delivery_no":     delivery_no,
            "container_no":    cid,
            "product":         ln.get("product", ""),
            "lot_no":          ln.get("lot", ""),
            "country_code":    country_code,
            "pkg_type":        pkg_type,
            "pkg_qty":         bags,
            "net_weight":      net_wt_kg,
            "gross_weight":    gross_wt_kg,
            "seal_no":         seal,
            "container_ref":   f"{cid} {ref_no}",
            "container_type":  ctype,
            "pallet_qty":      pallet_qty,
        })

    return rows
