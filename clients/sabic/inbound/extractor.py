# """
# SABIC Inbound — Extraction, validation, and row-building.

# Three Gemini prompts extract structured JSON from MBL, Packing List, and
# Invoice PDFs. Python then cross-validates common fields and builds the
# Outcome rows (one row per Product/Lot per Container).
# """

# import json
# import os

# from helpers.gemini_client import call_gemini

# # ═══════════════════════════════════════════════════════════════════════════
# # COUNTRY CODE LOOKUP
# # ═══════════════════════════════════════════════════════════════════════════

# PORT_COUNTRY_MAP = {
#     "KING ABDULLAH": "SA", "JEDDAH": "SA", "JUBAIL": "SA", "YANBU": "SA",
#     "DAMMAM": "SA", "RABIGH": "SA", "SAUDI ARABIA": "SA", "RIYADH": "SA",
#     "PUSAN": "KR", "BUSAN": "KR", "INCHEON": "KR", "KOREA": "KR",
#     "HOUSTON": "US", "LOS ANGELES": "US", "NEW YORK": "US", "SAVANNAH": "US",
#     "CHARLESTON": "US", "LONG BEACH": "US", "UNITED STATES": "US",
#     "ANTWERP": "BE", "BELGIUM": "BE",
#     "ROTTERDAM": "NL", "NETHERLANDS": "NL",
#     "HAMBURG": "DE", "BREMERHAVEN": "DE", "GERMANY": "DE",
#     "SHANGHAI": "CN", "QINGDAO": "CN", "NINGBO": "CN", "CHINA": "CN",
#     "SINGAPORE": "SG",
#     "MUMBAI": "IN", "NHAVA SHEVA": "IN", "INDIA": "IN",
#     "TOKYO": "JP", "YOKOHAMA": "JP", "KOBE": "JP", "JAPAN": "JP",
#     "FELIXSTOWE": "GB", "SOUTHAMPTON": "GB", "UNITED KINGDOM": "GB",
#     "LE HAVRE": "FR", "MARSEILLE": "FR", "FRANCE": "FR",
#     "BARCELONA": "ES", "VALENCIA": "ES", "SPAIN": "ES",
#     "GENOA": "IT", "GIOIA TAURO": "IT", "ITALY": "IT",
#     "PIRAEUS": "GR", "GREECE": "GR",
#     "ISTANBUL": "TR", "MERSIN": "TR", "TURKEY": "TR",
#     "DURBAN": "ZA", "CAPE TOWN": "ZA", "SOUTH AFRICA": "ZA",
#     "JEBEL ALI": "AE", "DUBAI": "AE", "ABU DHABI": "AE",
#     "MUNDRA": "IN", "CHENNAI": "IN",
#     "LAEM CHABANG": "TH", "THAILAND": "TH",
#     "PORT KLANG": "MY", "MALAYSIA": "MY",
#     "JAKARTA": "ID", "INDONESIA": "ID",
#     "HO CHI MINH": "VN", "VIETNAM": "VN",
#     "KARACHI": "PK", "PAKISTAN": "PK",
#     "COLOMBO": "LK", "SRI LANKA": "LK",
# }


# def get_country_code(port_of_loading: str) -> str:
#     upper = port_of_loading.upper().strip()
#     for key, code in PORT_COUNTRY_MAP.items():
#         if key in upper:
#             return code
#     return "??"


# def _normalize_container_id(cid: str) -> str:
#     """
#     Normalize a container ID to the standard 4-letter + 7-digit format (11 chars).
#     Handles:
#       - Spaces:  "MEDU 4867820"       → "MEDU4867820"
#       - Glued seal: "BEAU58480071072481" → "BEAU5848007" (seal discarded here)
#     """
#     cid = cid.replace(" ", "").strip().upper()
#     # Standard container ID: 4 alpha + 7 digits = 11 chars
#     if len(cid) > 11 and cid[:4].isalpha() and cid[4:11].isdigit():
#         cid = cid[:11]
#     return cid


# def _split_container_and_seal(raw_id: str, existing_seal: str) -> tuple[str, str]:
#     """
#     If Gemini glued container_id + seal into one string, split them.
#     Returns (container_id, seal).
#     """
#     raw = raw_id.replace(" ", "").strip().upper()
#     if len(raw) > 11 and raw[:4].isalpha() and raw[4:11].isdigit():
#         container = raw[:11]
#         seal = raw[11:]  # leftover digits = the seal
#         # Only use extracted seal if we don't already have one
#         if not existing_seal and seal.isdigit():
#             return container, seal
#         return container, existing_seal
#     return _normalize_container_id(raw), existing_seal


# # ═══════════════════════════════════════════════════════════════════════════
# # GEMINI PROMPTS
# # ═══════════════════════════════════════════════════════════════════════════

# MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields from this Master Bill of Lading (MBL / Sea Waybill) PDF and return ONLY a JSON object — no markdown, no explanation.

# RULES:
# - "ref_nos": Find ALL Sales Order Numbers. They appear in the cargo description area as "SALES ORDER NO.:XXXXXXX". If there are multiple, return all of them as separate array elements. Look across ALL rider pages.
# - "delivery_no": The Delivery Number from the cargo description, appears as "DELIVERY NO.:XXXXXXX".
# - "port_of_loading": The Port of Loading (e.g. "KING ABDULLAH PORT, SAUDI ARABIA").
# - "port_of_discharge": The Port of Discharge.
# - "vessel": The Vessel and Voyage number.
# - "mbl_no": The Sea Waybill / MBL number (top right, e.g. "MEDUFF373189").
# - "containers": An array of every container listed. Include containers from ALL rider pages (there may be multiple pages). For each container extract:
#   - "id": Container number with ALL spaces removed (e.g. "MEDU 4867820" must become "MEDU4867820")
#   - "type": Container type (e.g. "40' HIGH CUBE")
#   - "seal": Seal number

# IMPORTANT:
# - The MBL may have RIDER PAGES (continuation pages). You MUST extract containers from ALL pages.
# - Each container block shows: container ID, container type (e.g. 40' HIGH CUBE), then Seal Number on a separate line.
# - If Sales Order or Delivery numbers appear only on the last rider page's summary section, still capture them.

# Return this exact JSON structure:
# {
#   "mbl_no": "string",
#   "ref_nos": ["string"],
#   "delivery_no": "string",
#   "port_of_loading": "string",
#   "port_of_discharge": "string",
#   "vessel": "string",
#   "containers": [
#     {"id": "string", "type": "string", "seal": "string"}
#   ]
# }"""


# PKG_LIST_PROMPT = """You are a shipping-document data extractor. Extract all data from this SABIC Packing List PDF and return ONLY a JSON object — no markdown, no explanation.

# ══════════════════════════════════════════
# HEADER FIELDS
# ══════════════════════════════════════════
# - Order/STO number: appears as "Sharq Order/STO:" or "Petrokemya Order/STO:" etc.
# - Delivery number: appears as "Sharq Delivery:" or "Petrokemya Delivery:" etc.
# - Sabic PO: appears as "Sabic PO:" — extract ONLY the base numeric part (e.g. from "4506618575 000010" → "4506618575").
# - Sabic Delivery: appears as "Sabic Delivery:". This is the PRIMARY delivery_no.

# ══════════════════════════════════════════
# TABLE LAYOUT (CRITICAL — understand before extracting)
# ══════════════════════════════════════════
# The table columns are: Container ID / Seal No. | Material | PKG | CODE | Batch | Unit | Bags | Gross Weight | Verified Gross Mass | Net Weight

# The FIRST column contains TWO values stacked vertically:
#   LINE 1 → Container ID  (format: 4 UPPERCASE letters + 7 digits = exactly 11 characters, e.g. BEAU5848007)
#   LINE 2 → Seal No.      (format: 6-7 digits only, e.g. 1072481)

# Example of what you see in the PDF:
#   BEAU5848007     LLDPE 318BJ 149    ...
#     1072481
#   FFAU5740867     LLDPE 318BJ 149    ...
#     1072445

# From the above, you must extract:
#   container_id = "BEAU5848007"   seal = "1072481"    (TWO SEPARATE FIELDS)
#   container_id = "FFAU5740867"   seal = "1072445"    (TWO SEPARATE FIELDS)

# ⚠️  NEVER concatenate container_id and seal into one string.
#     WRONG: "BEAU58480071072481"  ← This is WRONG
#     RIGHT: container_id="BEAU5848007", seal="1072481"  ← This is CORRECT

# ══════════════════════════════════════════
# FIELD RULES
# ══════════════════════════════════════════
# - "container_id": EXACTLY 11 characters (4 letters + 7 digits). Remove any spaces. STOP at 11 characters. The number below it is the seal, NOT part of the container ID.
# - "has_container_id_in_pdf": boolean. Set to true ONLY if the Container ID is explicitly printed on THIS specific line (or the line directly above its seal). Set to false if the line is blank in the Container ID column (e.g. continuation lots).
# - "seal": The Seal Number on the line BELOW the container ID. It is a separate 6-7 digit number.
# - "product": The Material name (e.g. "LLDPE 318BJ 149").
# - "lot": The Batch number (e.g. "0061134205").
# - "bags": The value from the BAGS column (number of bags, e.g. 360, 660). Do NOT confuse with Unit (which shows pallets like "6 PAL").
# - "unit": The numeric part of the Unit column (e.g. from "6 PAL" → 6).
# - "pkg_type": Usually "PAL".
# - "pkg_code": The PKG code number (e.g. "148", "149").
# - "gross_weight": Gross Weight in MT.
# - "net_weight": Net Weight in MT.

# - Container ID and Seal appear ONLY on the FIRST line of each container group. Subsequent lines for the same container inherit the same container_id and seal from the line ABOVE them.
# - ⚠️ CRITICAL: If a row does not have a Container ID, it belongs to the last seen Container ID ABOVE it. Do NOT assign it to the Container ID below it.
# - Some containers have MULTIPLE lines (multiple lots/batches). Each lot is a SEPARATE entry in the lines array sharing the same container_id and seal.
# - The PDF may span MULTIPLE PAGES. Extract ALL lines from ALL pages.

# ══════════════════════════════════════════
# PAGE BREAK EXAMPLE (VERY IMPORTANT)
# ══════════════════════════════════════════
# A container's lots can SPLIT across pages. Example:

#   --- END OF PAGE 2 ---
#   AAAA1111111     LLDPE 318BJ 149    149   BATCH-A      6 PAL   360   9.1740 MT   29.7550 MT   9.0000 MT
#     9999999

#   --- START OF PAGE 3 (table header repeats, then first data row has NO container ID) ---
#   Container ID | Material | PKG CODE | Batch | Unit | Bags | ...
#                 LLDPE 318BJ 149    149   BATCH-B      11 PAL  660   16.8190 MT              16.5000 MT
#   BBBB2222222     LLDPE 318BJ 149    149   BATCH-C      11 PAL  660   ...
#     8888888

# The row with batch BATCH-B has NO container ID → it is a CONTINUATION of AAAA1111111 from the previous page.
# Do NOT assign it to BBBB2222222. BBBB2222222 starts on its OWN row below.

# Result:
#   {"container_id": "AAAA1111111", "has_container_id_in_pdf": true, "seal": "9999999", "lot": "BATCH-A", "bags": 360, ...}
#   {"container_id": "AAAA1111111", "has_container_id_in_pdf": false, "seal": "9999999", "lot": "BATCH-B", "bags": 660, ...}  ← CORRECT
#   {"container_id": "BBBB2222222", "has_container_id_in_pdf": true, "seal": "8888888", "lot": "BATCH-C", "bags": 660, ...}

# ══════════════════════════════════════════
# OUTPUT FORMAT
# ══════════════════════════════════════════
# {
#   "delivery_no": "string",
#   "sto": "string",
#   "sabic_po": "string (base number only)",
#   "sabic_delivery": "string",
#   "lines": [
#     {
#       "container_id": "string (EXACTLY 11 chars: 4 letters + 7 digits)",
#       "has_container_id_in_pdf": boolean,
#       "seal": "string (separate 6-7 digit number)",
#       "product": "string",
#       "pkg_code": "string",
#       "lot": "string",
#       "unit": number,
#       "pkg_type": "string",
#       "bags": number,
#       "gross_weight": number,
#       "net_weight": number
#     }
#   ]
# }"""


# INVOICE_PROMPT = """You are a shipping-document data extractor. Extract the following fields from this Commercial Invoice PDF and return ONLY a JSON object — no markdown, no explanation.

# RULES:
# - "invoice_no": The Invoice Number.
# - "sales_ref": The Sales Ref number. It may appear as "Sales Ref: 4506618575/0010". Extract ONLY the base number before any slash (e.g. "4506618575").
# - "delivery_no": The Delivery Number. It may appear as "Delivery No: 809110681/0010". Extract ONLY the base number before any slash (e.g. "809110681").
# - "product": The product description from the line items (e.g. "LLDPE 318BJ" or "G3220A 10000").
# - "shipment_no": The Shipment Number.
# - "qty": The total quantity as a number.
# - "unit": The unit of measure (e.g. "MT").

# Return this exact JSON structure:
# {
#   "invoice_no": "string",
#   "sales_ref": "string",
#   "delivery_no": "string",
#   "product": "string",
#   "shipment_no": "string",
#   "qty": number,
#   "unit": "string"
# }"""


# # ═══════════════════════════════════════════════════════════════════════════
# # EXTRACTION FUNCTIONS
# # ═══════════════════════════════════════════════════════════════════════════

# def _dump_debug_json(pdf_path: str, suffix: str, data: dict):
#     """Save raw extraction JSON next to the source PDF for debugging."""
#     try:
#         out_dir = os.path.dirname(pdf_path)
#         out_file = os.path.join(out_dir, suffix)
#         with open(out_file, "w", encoding="utf-8") as f:
#             json.dump(data, f, indent=2, ensure_ascii=False)
#     except Exception:
#         pass  # debug dump is best-effort


# def extract_mbl(pdf_path: str) -> dict:
#     data = call_gemini(MBL_PROMPT, pdf_path=pdf_path, max_output_tokens=16384)
#     # Normalize container IDs (PDFs sometimes have spaces like "MEDU 4867820")
#     for c in data.get("containers", []):
#         c["id"] = _normalize_container_id(c.get("id", ""))
#     _dump_debug_json(pdf_path, "mbl.json", data)
#     return data


# def _fix_page_break_orphans(lines: list) -> list:
#     """
#     Fix page-break orphan lots using the LLM's `has_container_id_in_pdf` flag.
#     If a line lacks a container ID in the PDF, it MUST belong to the last
#     seen container that DID have an ID. If the LLM assigned it to a different
#     container (because of a page break), we overwrite it.
#     """
#     if not lines:
#         return lines

#     last_real_cid = ""
#     last_real_seal = ""

#     for ln in lines:
#         has_id = ln.get("has_container_id_in_pdf")
        
#         # In case the LLM returned a string or None, normalize to boolean
#         if isinstance(has_id, str):
#             has_id = has_id.lower() == "true"
#         elif has_id is None:
#             has_id = True  # assume True if missing to be safe

#         if has_id:
#             # This is a real container start line
#             last_real_cid = ln.get("container_id", "")
#             last_real_seal = ln.get("seal", "")
#         else:
#             # This is a continuation line
#             current_cid = ln.get("container_id", "")
            
#             # If the LLM assigned it to the wrong container (the one below it), fix it
#             if last_real_cid and current_cid != last_real_cid:
#                 print(f"  [POST-PROCESS] Reassigned orphan lot {ln.get('lot','')} "
#                       f"from {current_cid} → {last_real_cid} (page-break fix)")
#                 ln["container_id"] = last_real_cid
#                 ln["seal"] = last_real_seal

#     return lines


# def extract_packing_list(pdf_path: str) -> dict:
#     data = call_gemini(PKG_LIST_PROMPT, pdf_path=pdf_path, max_output_tokens=16384)
#     # Fix container IDs: split glued container+seal, remove spaces (safety net)
#     for ln in data.get("lines", []):
#         raw_cid = ln.get("container_id") or ""
#         existing_seal = ln.get("seal") or ""
#         cid, seal = _split_container_and_seal(raw_cid, existing_seal)
#         ln["container_id"] = cid
#         ln["seal"] = seal
#     # Fix page-break orphan lots
#     data["lines"] = _fix_page_break_orphans(data.get("lines", []))
#     _dump_debug_json(pdf_path, "pkg_list.json", data)
#     return data


# def extract_invoice(pdf_path: str) -> dict:
#     return call_gemini(INVOICE_PROMPT, pdf_path=pdf_path)


# # ═══════════════════════════════════════════════════════════════════════════
# # CROSS-DOCUMENT VALIDATION
# # ═══════════════════════════════════════════════════════════════════════════

# def _build_ref(ref_nos: list) -> str:
#     """Join multiple refs with +, stripping /XXXX suffixes."""
#     cleaned = []
#     for r in ref_nos:
#         base = str(r).split("/")[0].strip()
#         if base and base not in cleaned:
#             cleaned.append(base)
#     return "+".join(cleaned)


# def validate(mbl: dict, pkl: dict, inv: dict) -> list[str]:
#     """
#     Cross-validate MBL, Packing List, and Invoice.
#     Returns a list of status strings like:
#       "[OK] REF — MBL(X) = Invoice(X)"
#       "[!]  CONTAINER — MSMU5262036 only in MBL"
#       "[X]  REF MISMATCH — MBL(X) vs Invoice(Y)"
#     """
#     results = []

#     mbl_ref = _build_ref(mbl.get("ref_nos", []))
#     inv_ref = _build_ref([inv.get("sales_ref", "")])
#     pkl_ref = _build_ref([pkl.get("sabic_po", "")])

#     # --- REF ---
#     if mbl_ref and inv_ref:
#         if mbl_ref == inv_ref:
#             results.append(f"[OK] REF — MBL({mbl_ref}) = Invoice({inv_ref})")
#         else:
#             results.append(f"[X]  REF MISMATCH — MBL({mbl_ref}) vs Invoice({inv_ref})")

#     if mbl_ref and pkl_ref:
#         if mbl_ref == pkl_ref:
#             results.append(f"[OK] REF — MBL({mbl_ref}) = Packing List({pkl_ref})")
#         else:
#             results.append(f"[X]  REF MISMATCH — MBL({mbl_ref}) vs Packing List({pkl_ref})")

#     # --- DELIVERY ---
#     mbl_del = str(mbl.get("delivery_no", "")).split("/")[0]
#     inv_del = str(inv.get("delivery_no", "")).split("/")[0]
#     pkl_sabic_del = str(pkl.get("sabic_delivery", "")).split("/")[0]

#     if mbl_del and inv_del:
#         if mbl_del == inv_del:
#             results.append(f"[OK] DELIVERY — MBL({mbl_del}) = Invoice({inv_del})")
#         else:
#             results.append(f"[X]  DELIVERY MISMATCH — MBL({mbl_del}) vs Invoice({inv_del})")

#     if mbl_del and pkl_sabic_del:
#         if mbl_del == pkl_sabic_del:
#             results.append(f"[OK] DELIVERY — MBL({mbl_del}) = Packing List Sabic({pkl_sabic_del})")
#         else:
#             results.append(f"[!]  DELIVERY — MBL({mbl_del}) vs Packing List Sabic({pkl_sabic_del})")

#     # --- PRODUCT ---
#     inv_product = str(inv.get("product", "")).upper()
#     pkl_products = set()
#     for line in pkl.get("lines", []):
#         pkl_products.add(str(line.get("product", "")).upper())

#     for p in pkl_products:
#         if inv_product and (inv_product in p or p in inv_product):
#             results.append(f"[OK] PRODUCT — Invoice({inv_product}) ~ Packing List({p})")
#         elif inv_product:
#             results.append(f"[!]  PRODUCT — Invoice({inv_product}) vs Packing List({p})")

#     # --- CONTAINERS ---
#     mbl_cids = {c["id"] for c in mbl.get("containers", [])}
#     pkl_cids = {ln["container_id"] for ln in pkl.get("lines", [])}

#     common = mbl_cids & pkl_cids
#     only_mbl = mbl_cids - pkl_cids
#     only_pkl = pkl_cids - mbl_cids

#     if common:
#         results.append(f"[OK] CONTAINERS — {len(common)} matched across MBL & Packing List")
#     if only_mbl:
#         for c in sorted(only_mbl):
#             results.append(f"[!]  CONTAINER — {c} only in MBL (not in Packing List)")
#     if only_pkl:
#         for c in sorted(only_pkl):
#             results.append(f"[!]  CONTAINER — {c} only in Packing List (not in MBL)")

#     return results


# # ═══════════════════════════════════════════════════════════════════════════
# # ROW BUILDER
# # ═══════════════════════════════════════════════════════════════════════════

# def build_rows(mbl: dict, pkl: dict) -> list[dict]:
#     """
#     Build Outcome rows — one per Product/Lot per Container.
#     """
#     ref_no = _build_ref(mbl.get("ref_nos", []))

#     # Delivery No: Sabic Delivery first, then Packing List's Sharq/Petrokemya Delivery, then STO
#     delivery_no = str(pkl.get("sabic_delivery", "")).strip()
#     if not delivery_no:
#         delivery_no = str(pkl.get("delivery_no", "")).strip()
#     if not delivery_no:
#         delivery_no = str(pkl.get("sto", "")).strip()

#     country_code = get_country_code(mbl.get("port_of_loading", ""))

#     # MBL container lookup for type
#     mbl_map = {}
#     for c in mbl.get("containers", []):
#         mbl_map[c["id"]] = {"type": c.get("type", ""), "seal": c.get("seal", "")}

#     rows = []
#     for ln in pkl.get("lines", []):
#         cid = ln["container_id"]
#         seal = ln.get("seal", "")
#         ctype = ""

#         if cid in mbl_map:
#             ctype = mbl_map[cid].get("type", "")
#             if not seal:
#                 seal = mbl_map[cid].get("seal", "")

#         rows.append({
#             "ref_no":          ref_no,
#             "delivery_no":     delivery_no,
#             "container_no":    cid,
#             "product":         ln.get("product", ""),
#             "lot_no":          ln.get("lot", ""),
#             "country_code":    country_code,
#             "pkg_type":        ln.get("pkg_type", "PAL"),
#             "pkg_qty":         ln.get("bags", 0),
#             "net_weight":      ln.get("net_weight", 0),
#             "gross_weight":    ln.get("gross_weight", 0),
#             "seal_no":         seal,
#             "container_ref":   f"{cid}+{ref_no}",
#             "container_type":  ctype,
#         })

#     return rows
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


# PKG_LIST_PROMPT = """You are a shipping-document data extractor. Extract all data from this SABIC Packing List PDF and return ONLY a JSON object — no markdown, no explanation.

# ══════════════════════════════════════════
# HEADER FIELDS
# ══════════════════════════════════════════
# - Order/STO: appears as "Sharq Order/STO:" or "Petrokemya Order/STO:" etc.
# - Delivery: appears as "Sharq Delivery:" or "Petrokemya Delivery:" etc.
# - Sabic PO: "Sabic PO:" — base number only (e.g. "4506618575" not "4506618575 000010").
# - Sabic Delivery: "Sabic Delivery:".

# ══════════════════════════════════════════
# TABLE STRUCTURE
# ══════════════════════════════════════════
# Columns: Container ID / Seal No. | Material | PKG CODE | Batch | Unit | Bags | Gross Weight | Verified Gross Mass | Net Weight

# The first column has TWO values stacked:
#   Line 1 → Container ID (4 uppercase letters + 7 digits = 11 chars, e.g. BEAU5848007)
#   Line 2 → Seal No. (6-7 digits, e.g. 1072481)

# ⚠️ container_id and seal are ALWAYS separate fields. NEVER concatenate them.

# ══════════════════════════════════════════
# NESTED OUTPUT FORMAT (CRITICAL)
# ══════════════════════════════════════════
# Return containers as a NESTED structure — each container has an "items" array with its lots inside.

# A container may have MULTIPLE lots (batches). The Container ID and Seal appear only on the first row. Subsequent rows with NO Container ID are continuation lots belonging to the SAME container.

# ⚠️ CRITICAL PAGE-BREAK RULE (USING VERIFIED GROSS MASS):
# The DEFINITIVE way to know if a row is a new container or a continuation lot is the "Verified Gross Mass" column. 
# - The FIRST row of a container ALWAYS has a value in the "Verified Gross Mass" column.
# - Continuation lots (even if they start on a new page) DO NOT have a "Verified Gross Mass" value.
# When a new page starts and the first data row has NO Verified Gross Mass, that row is a CONTINUATION of the LAST container from the PREVIOUS page. Add it to the previous container's items array.

# Example:
#   END OF PAGE 1:
#     MSMU8152963   LLDPE 318BJ 149   0052220632   6 PAL    360    9.174 MT   29.755 MT  (Has VGM!)
#       1072489

#   START OF PAGE 2 (first row has NO Container ID and NO Verified Gross Mass):
#                   LLDPE 318BJ 149   0061144594   11 PAL   660    16.819 MT             (No VGM!) ← belongs to MSMU8152963!
#     MSMU5295868   LLDPE 318BJ 149   0052220632   11 PAL   660    16.819 MT  29.755 MT  (Has VGM!) ← new container
#       1072545

# Result:
#   {"container_id": "MSMU8152963", "seal": "1072489", "items": [
#       {"lot": "0052220632", "bags": 360, ...},
#       {"lot": "0061144594", "bags": 660, ...}    ← page 2 orphan goes HERE because it had no VGM
#   ]},
#   {"container_id": "MSMU5295868", "seal": "1072545", "items": [
#       {"lot": "0052220632", "bags": 660, ...}
#   ]}

# ══════════════════════════════════════════
# FIELD RULES
# ══════════════════════════════════════════
# - "container_id": exactly 11 chars (4 letters + 7 digits). Seal is NOT part of this.
# - "seal": separate 6-7 digit number below the container ID.
# - "product": Material name (e.g. "LLDPE 318BJ 149").
# - "lot": Batch number (10-digit, e.g. "0061134205").
# - "bags": from BAGS column (number of bags). NOT the Unit/pallet count.
# - "unit": numeric part of Unit column (e.g. 6 from "6 PAL").
# - "pkg_type": usually "PAL".
# - "pkg_code": PKG code number.
# - "gross_weight": Gross Weight in MT (number only).
# - "net_weight": Net Weight in MT (number only).

# Extract ALL containers and ALL items from ALL pages.

# ══════════════════════════════════════════
# OUTPUT FORMAT
# ══════════════════════════════════════════
# {
#   "delivery_no": "string",
#   "sto": "string",
#   "sabic_po": "string",
#   "sabic_delivery": "string",
#   "containers": [
#     {
#       "container_id": "string (11 chars: 4 letters + 7 digits)",
#       "seal": "string (separate number)",
#       "items": [
#         {
#           "product": "string",
#           "pkg_code": "string",
#           "lot": "string",
#           "unit": number,
#           "pkg_type": "string",
#           "bags": number,
#           "gross_weight": number,
#           "net_weight": number
#         }
#       ]
#     }
#   ]
# }"""

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
  This is the number of PALLETS — always much SMALLER than bags.
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

# PKG_LIST_PROMPT = """You are a shipping-document data extractor. Extract all data from this SABIC Packing List PDF and return ONLY a JSON object — no markdown, no explanation.

# ══════════════════════════════════════════
# HEADER FIELDS
# ══════════════════════════════════════════
# - Order/STO: appears as "Sharq Order/STO:" or "Petrokemya Order/STO:" etc.
# - Delivery: appears as "Sharq Delivery:" or "Petrokemya Delivery:" etc.
# - Sabic PO: "Sabic PO:" — base number only (e.g. "4506618575" not "4506618575 000010").
# - Sabic Delivery: "Sabic Delivery:".

# ══════════════════════════════════════════
# TABLE STRUCTURE
# ══════════════════════════════════════════
# Columns: Container ID / Seal No. | Material | PKG CODE | Batch | Unit | Bags | Gross Weight | Verified Gross Mass | Net Weight

# The first column has TWO values stacked:
#   Line 1 → Container ID (4 uppercase letters + 7 digits = 11 chars, e.g. BEAU5848007)
#   Line 2 → Seal No. (6-7 digits, e.g. 1072481)

# ⚠️ container_id and seal are ALWAYS separate fields. NEVER concatenate them.

# ══════════════════════════════════════════
# HOW TO GROUP ROWS INTO CONTAINERS (READ THIS AS AN ALGORITHM, NOT A GUIDELINE)
# ══════════════════════════════════════════
# Process the table ONE ROW AT A TIME, strictly top to bottom, in the exact
# physical order the rows are printed — ignore page breaks entirely, they are
# not relevant to this process. Keep a single variable "current container" in
# your head as you go:

#   FOR EACH row, in printed order:
#     IF this row has a value in the "Verified Gross Mass" column:
#         → This row STARTS A NEW container. Open a new container using the
#           Container ID + Seal printed on this row. This new container is now
#           "current container".
#     ELSE (this row has NO Verified Gross Mass value):
#         → This row is a CONTINUATION lot. It belongs to "current container"
#           — i.e. whatever container you opened most recently, from the row
#           immediately above. Append it to that container's items array.
#           It can NEVER belong to a container whose row you have not reached
#           yet.

# Do this as a single forward pass. Never look ahead at what container ID comes
# next in the table to decide where an orphan row belongs — the ONLY thing that
# determines an orphan's container is which container is "current" at the
# moment you reach that row, based on rows already processed above it.

# NOTE : This is the single most common extraction mistake: attaching a no-VGM row
# to the container printed BELOW it instead of the container that opened ABOVE
# it . You Must place the that Row to the Last container or Above container.
# ══════════════════════════════════════════
# WORKED EXAMPLE 1 — mid-page (the case most often gotten wrong)
# ══════════════════════════════════════════
#     TXGU4257926   LLDPE 318BJ 149   0052220632   4 PAL   240   6.116 MT   29.755 MT   6.0 MT
#       1072586
#                   LLDPE 318BJ 149   0061144594   13 PAL  780   19.877 MT              19.5 MT
#     MSMU8152963   LLDPE 318BJ 149   0052220632   6 PAL   360   9.174 MT   29.755 MT   9.0 MT
#       1072489

# Row-by-row:
#   Row 1: TXGU4257926, HAS VGM (29.755 MT) → open new container TXGU4257926. current = TXGU4257926.
#   Row 2: no container ID, NO VGM → continuation of current (TXGU4257926). Append here.
#   Row 3: MSMU8152963, HAS VGM (29.755 MT) → open new container MSMU8152963. current = MSMU8152963.

# Correct result:
#   {"container_id": "TXGU4257926", "seal": "1072586", "items": [
#       {"lot": "0052220632", "bags": 240, ...},
#       {"lot": "0061144594", "bags": 780, ...}   ← belongs here — it appeared BEFORE MSMU8152963's row
#   ]},
#   {"container_id": "MSMU8152963", "seal": "1072489", "items": [
#       {"lot": "0052220632", "bags": 360, ...}
#   ]}

# WRONG result (do not do this):
#   {"container_id": "TXGU4257926", "items": [{"lot": "0052220632", "bags": 240, ...}]},
#   {"container_id": "MSMU8152963", "items": [
#       {"lot": "0061144594", "bags": 780, ...},   ← WRONG, this row came before MSMU8152963 even appeared
#       {"lot": "0052220632", "bags": 360, ...}
#   ]}

# ══════════════════════════════════════════
# WORKED EXAMPLE 2 — across a page break (same algorithm, no special-casing needed)
# ══════════════════════════════════════════
# This is a real page-break sequence. Notice it repeats the SAME pattern three
# times in a row — a container with VGM, immediately followed by one orphan
# row with no VGM, then the next container with VGM. Apply the algorithm
# identically each time, regardless of which page a row happens to be printed on:

#   END OF PAGE 1:
#     MSMU8152963   LLDPE 318BJ 149   149   0052220632   6 PAL    360   9.1740 MT   29.7550 MT   9.0000 MT
#       1072489

#   START OF PAGE 2 (table header repeats, then data continues):
#                   LLDPE 318BJ 149   149   0061144594   11 PAL   660   16.8190 MT               16.5000 MT
#     MSMU5295868   LLDPE 318BJ 149   149   0052220632   11 PAL   660   16.8190 MT   29.7550 MT   16.5000 MT
#       1072545
#                   LLDPE 318BJ 149   149   0061134205   6 PAL    360   9.1740 MT                 9.0000 MT
#     FFAU3510588   LLDPE 318BJ 149   149   0047606253   2 PAL    120   3.0580 MT    29.7550 MT   3.0000 MT
#       1072587
#                   LLDPE 318BJ 149   149   0052220632   15 PAL   900   22.9350 MT                22.5000 MT
#     MEDU7411270   LLDPE 318BJ 149   149   0061134205   17 PAL   1020  25.9930 MT   29.7550 MT   25.5000 MT

# Row-by-row:
#   Row 1: MSMU8152963, HAS VGM (29.755) → open MSMU8152963. current = MSMU8152963.
#   Row 2 (page 2 starts here): no container ID, NO VGM → continuation of current
#          (MSMU8152963). The page break is irrelevant — this row still belongs
#          to the container opened on the row directly above it.
#   Row 3: MSMU5295868, HAS VGM (29.755) → open MSMU5295868. current = MSMU5295868.
#   Row 4: no container ID, NO VGM → continuation of current (MSMU5295868), NOT
#          of FFAU3510588 which hasn't appeared yet.
#   Row 5: FFAU3510588, HAS VGM (29.755) → open FFAU3510588. current = FFAU3510588.
#   Row 6: no container ID, NO VGM → continuation of current (FFAU3510588), NOT
#          of MEDU7411270 which hasn't appeared yet.
#   Row 7: MEDU7411270, HAS VGM (29.755) → open MEDU7411270. current = MEDU7411270.

# Correct result:
#   {"container_id": "MSMU8152963", "seal": "1072489", "items": [
#       {"lot": "0052220632", "bags": 360, ...},
#       {"lot": "0061144594", "bags": 660, ...}   ← orphan from top of page 2
#   ]},
#   {"container_id": "MSMU5295868", "seal": "1072545", "items": [
#       {"lot": "0052220632", "bags": 660, ...},
#       {"lot": "0061134205", "bags": 360, ...}   ← orphan — belongs HERE, not to FFAU3510588
#   ]},
#   {"container_id": "FFAU3510588", "seal": "1072587", "items": [
#       {"lot": "0047606253", "bags": 120, ...},
#       {"lot": "0052220632", "bags": 900, ...}   ← orphan — belongs HERE, not to MEDU7411270
#   ]},
#   {"container_id": "MEDU7411270", "seal": "", "items": [
#       {"lot": "0061134205", "bags": 1020, ...}
#   ]}

# Result:
#   {"container_id": "MSMU8152963", "seal": "1072489", "items": [
#       {"lot": "0052220632", "bags": 360, ...},
#       {"lot": "0061144594", "bags": 660, ...}   ← still the row above's container; page break changes nothing
#   ]},
#   {"container_id": "MSMU5295868", "seal": "1072545", "items": [
#       {"lot": "0052220632", "bags": 660, ...}
#   ]}

# ══════════════════════════════════════════
# FIELD RULES
# ══════════════════════════════════════════
# - "container_id": exactly 11 chars (4 letters + 7 digits). Seal is NOT part of this.
# - "seal": separate 6-7 digit number below the container ID.
# - "product": Material name (e.g. "LLDPE 318BJ 149").
# - "lot": Batch number (10-digit, e.g. "0061134205").
# - "bags": from BAGS column (number of bags). NOT the Unit/pallet count.
# - "unit": numeric part of Unit column (e.g. 6 from "6 PAL").
# - "pkg_type": usually "PAL".
# - "pkg_code": PKG code number.
# - "gross_weight": Gross Weight in MT (number only).
# - "net_weight": Net Weight in MT (number only).

# Extract ALL containers and ALL items from ALL pages.

# ══════════════════════════════════════════
# BEFORE YOU FINALIZE — SELF-CHECK
# ══════════════════════════════════════════
# For every item you placed in a container's "items" array, confirm: does this
# row appear, in printed reading order, AFTER that container's own row and
# BEFORE the next container's row? If an item would appear after the NEXT
# container already started, you have misassigned it — move it to the correct
# container.

# ══════════════════════════════════════════
# OUTPUT FORMAT
# ══════════════════════════════════════════
# {
#   "delivery_no": "string",
#   "sto": "string",
#   "sabic_po": "string",
#   "sabic_delivery": "string",
#   "containers": [
#     {
#       "container_id": "string (11 chars: 4 letters + 7 digits)",
#       "seal": "string (separate number)",
#       "items": [
#         {
#           "product": "string",
#           "pkg_code": "string",
#           "lot": "string",
#           "unit": number,
#           "pkg_type": "string",
#           "bags": number,
#           "gross_weight": number,
#           "net_weight": number
#         }
#       ]
#     }
#   ]
# }"""

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


# def extract_packing_list(pdf_path: str) -> dict:
#     """
#     Gemini returns nested format: containers[] → items[].
#     We flatten it to a flat lines[] format for downstream processing.
#     """
#     data = call_gemini(PKG_LIST_PROMPT, pdf_path=pdf_path, max_output_tokens=16384)
#     _dump_json(pdf_path, "pkg_list_raw.json", data)

#     # Flatten nested containers→items into flat lines[]
#     flat_lines = []
#     for container in data.get("containers", []):
#         raw_cid = container.get("container_id", "")
#         raw_seal = container.get("seal", "")
#         cid, seal = _fix_container_id(raw_cid, raw_seal)

#         items = container.get("items", [])
#         if not items:
#             # Container with no items — skip or add empty
#             continue

#         for item in items:
#             flat_lines.append({
#                 "container_id": cid,
#                 "seal":         seal,
#                 "product":      item.get("product", ""),
#                 "pkg_code":     item.get("pkg_code", ""),
#                 "lot":          item.get("lot", ""),
#                 "unit":         item.get("unit", 0),
#                 "pkg_type":     item.get("pkg_type", "PAL"),
#                 "bags":         item.get("bags", 0),
#                 "gross_weight": item.get("gross_weight", 0),
#                 "net_weight":   item.get("net_weight", 0),
#             })

#     # Replace nested structure with flat lines for downstream
#     result = {
#         "delivery_no":   data.get("delivery_no", ""),
#         "sto":           data.get("sto", ""),
#         "sabic_po":      data.get("sabic_po", ""),
#         "sabic_delivery": data.get("sabic_delivery", ""),
#         "lines":         flat_lines,
#     }

#     _dump_json(pdf_path, "pkg_list.json", result)
#     print(f"  [PKG LIST] Extracted {len(data.get('containers', []))} containers, "
#           f"{len(flat_lines)} total lines")
#     return result

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
            "unit":         row.get("unit", 0),
            "pkg_type":     row.get("pkg_type", "PAL"),
            "bags":         _num(row.get("bags"), 0),
            "gross_weight": _num(row.get("gross_weight"), 0),
            "net_weight":   _num(row.get("net_weight"), 0),
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

        rows.append({
            "ref_no":          ref_no,
            "delivery_no":     delivery_no,
            "container_no":    cid,
            "product":         ln.get("product", ""),
            "lot_no":          ln.get("lot", ""),
            "country_code":    country_code,
            "pkg_type":        ln.get("pkg_type", "PAL"),
            "pkg_qty":         ln.get("bags", 0),
            "net_weight":      ln.get("net_weight", 0),
            "gross_weight":    ln.get("gross_weight", 0),
            "seal_no":         seal,
            "container_ref":   f"{cid} {ref_no}",
            "container_type":  ctype,
        })

    return rows

