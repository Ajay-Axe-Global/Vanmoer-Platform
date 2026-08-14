"""
SABIC Inbound — Extraction, validation, and row-building.

Three Gemini prompts extract structured JSON from MBL, Packing List, and
Invoice PDFs. Python then cross-validates common fields and builds the
Outcome rows (one row per Product/Lot per Container).
"""

import json
import os

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


def _normalize_container_id(cid: str) -> str:
    """
    Normalize a container ID to the standard 4-letter + 7-digit format (11 chars).
    Handles:
      - Spaces:  "MEDU 4867820"       → "MEDU4867820"
      - Glued seal: "BEAU58480071072481" → "BEAU5848007" (seal discarded here)
    """
    cid = cid.replace(" ", "").strip().upper()
    # Standard container ID: 4 alpha + 7 digits = 11 chars
    if len(cid) > 11 and cid[:4].isalpha() and cid[4:11].isdigit():
        cid = cid[:11]
    return cid


def _split_container_and_seal(raw_id: str, existing_seal: str) -> tuple[str, str]:
    """
    If Gemini glued container_id + seal into one string, split them.
    Returns (container_id, seal).
    """
    raw = raw_id.replace(" ", "").strip().upper()
    if len(raw) > 11 and raw[:4].isalpha() and raw[4:11].isdigit():
        container = raw[:11]
        seal = raw[11:]  # leftover digits = the seal
        # Only use extracted seal if we don't already have one
        if not existing_seal and seal.isdigit():
            return container, seal
        return container, existing_seal
    return _normalize_container_id(raw), existing_seal


# ═══════════════════════════════════════════════════════════════════════════
# GEMINI PROMPTS
# ═══════════════════════════════════════════════════════════════════════════

MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields from this Master Bill of Lading (MBL / Sea Waybill) PDF and return ONLY a JSON object — no markdown, no explanation.

RULES:
- "ref_nos": Find ALL Sales Order Numbers. They appear in the cargo description area as "SALES ORDER NO.:XXXXXXX". If there are multiple, return all of them as separate array elements. Look across ALL rider pages.
- "delivery_no": The Delivery Number from the cargo description, appears as "DELIVERY NO.:XXXXXXX".
- "port_of_loading": The Port of Loading (e.g. "KING ABDULLAH PORT, SAUDI ARABIA").
- "port_of_discharge": The Port of Discharge.
- "vessel": The Vessel and Voyage number.
- "mbl_no": The Sea Waybill / MBL number (top right, e.g. "MEDUFF373189").
- "containers": An array of every container listed. Include containers from ALL rider pages (there may be multiple pages). For each container extract:
  - "id": Container number with ALL spaces removed (e.g. "MEDU 4867820" must become "MEDU4867820")
  - "type": Container type (e.g. "40' HIGH CUBE")
  - "seal": Seal number

IMPORTANT:
- The MBL may have RIDER PAGES (continuation pages). You MUST extract containers from ALL pages.
- Each container block shows: container ID, container type (e.g. 40' HIGH CUBE), then Seal Number on a separate line.
- If Sales Order or Delivery numbers appear only on the last rider page's summary section, still capture them.

Return this exact JSON structure:
{
  "mbl_no": "string",
  "ref_nos": ["string"],
  "delivery_no": "string",
  "port_of_loading": "string",
  "port_of_discharge": "string",
  "vessel": "string",
  "containers": [
    {"id": "string", "type": "string", "seal": "string"}
  ]
}"""


PKG_LIST_PROMPT = """You are a shipping-document data extractor. Extract all data from this SABIC Packing List PDF and return ONLY a JSON object — no markdown, no explanation.

══════════════════════════════════════════
HEADER FIELDS
══════════════════════════════════════════
- Order/STO number: appears as "Sharq Order/STO:" or "Petrokemya Order/STO:" etc.
- Delivery number: appears as "Sharq Delivery:" or "Petrokemya Delivery:" etc. This is the PRIMARY delivery_no.
- Sabic PO: appears as "Sabic PO:" — extract ONLY the base numeric part (e.g. from "4506618575 000010" → "4506618575").
- Sabic Delivery: appears as "Sabic Delivery:".

══════════════════════════════════════════
TABLE LAYOUT (CRITICAL — understand before extracting)
══════════════════════════════════════════
The table columns are: Container ID / Seal No. | Material | PKG | CODE | Batch | Unit | Bags | Gross Weight | Verified Gross Mass | Net Weight

The FIRST column contains TWO values stacked vertically:
  LINE 1 → Container ID  (format: 4 UPPERCASE letters + 7 digits = exactly 11 characters, e.g. BEAU5848007)
  LINE 2 → Seal No.      (format: 6-7 digits only, e.g. 1072481)

Example of what you see in the PDF:
  BEAU5848007     LLDPE 318BJ 149    ...
    1072481
  FFAU5740867     LLDPE 318BJ 149    ...
    1072445

From the above, you must extract:
  container_id = "BEAU5848007"   seal = "1072481"    (TWO SEPARATE FIELDS)
  container_id = "FFAU5740867"   seal = "1072445"    (TWO SEPARATE FIELDS)

⚠️  NEVER concatenate container_id and seal into one string.
    WRONG: "BEAU58480071072481"  ← This is WRONG
    RIGHT: container_id="BEAU5848007", seal="1072481"  ← This is CORRECT

══════════════════════════════════════════
FIELD RULES
══════════════════════════════════════════
- "container_id": EXACTLY 11 characters (4 letters + 7 digits). Remove any spaces. STOP at 11 characters. The number below it is the seal, NOT part of the container ID.
- "seal": The Seal Number on the line BELOW the container ID. It is a separate 6-7 digit number.
- "product": The Material name (e.g. "LLDPE 318BJ 149").
- "lot": The Batch number (e.g. "0061134205").
- "bags": The value from the BAGS column (number of bags, e.g. 360, 660). Do NOT confuse with Unit (which shows pallets like "6 PAL").
- "unit": The numeric part of the Unit column (e.g. from "6 PAL" → 6).
- "pkg_type": Usually "PAL".
- "pkg_code": The PKG code number (e.g. "148", "149").
- "gross_weight": Gross Weight in MT.
- "net_weight": Net Weight in MT.

- Container ID and Seal appear ONLY on the FIRST line of each container group. Subsequent lines for the same container inherit the same container_id and seal.
- Some containers have MULTIPLE lines (multiple lots/batches). Each lot is a SEPARATE entry in the lines array sharing the same container_id and seal.
- The PDF may span MULTIPLE PAGES. Extract ALL lines from ALL pages.

══════════════════════════════════════════
OUTPUT FORMAT
══════════════════════════════════════════
{
  "delivery_no": "string",
  "sto": "string",
  "sabic_po": "string (base number only)",
  "sabic_delivery": "string",
  "lines": [
    {
      "container_id": "string (EXACTLY 11 chars: 4 letters + 7 digits)",
      "seal": "string (separate 6-7 digit number)",
      "product": "string",
      "pkg_code": "string",
      "lot": "string",
      "unit": number,
      "pkg_type": "string",
      "bags": number,
      "gross_weight": number,
      "net_weight": number
    }
  ]
}"""


INVOICE_PROMPT = """You are a shipping-document data extractor. Extract the following fields from this Commercial Invoice PDF and return ONLY a JSON object — no markdown, no explanation.

RULES:
- "invoice_no": The Invoice Number.
- "sales_ref": The Sales Ref number. It may appear as "Sales Ref: 4506618575/0010". Extract ONLY the base number before any slash (e.g. "4506618575").
- "delivery_no": The Delivery Number. It may appear as "Delivery No: 809110681/0010". Extract ONLY the base number before any slash (e.g. "809110681").
- "product": The product description from the line items (e.g. "LLDPE 318BJ" or "G3220A 10000").
- "shipment_no": The Shipment Number.
- "qty": The total quantity as a number.
- "unit": The unit of measure (e.g. "MT").

Return this exact JSON structure:
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

def _dump_debug_json(pdf_path: str, suffix: str, data: dict):
    """Save raw extraction JSON next to the source PDF for debugging."""
    try:
        out_dir = os.path.dirname(pdf_path)
        out_file = os.path.join(out_dir, suffix)
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass  # debug dump is best-effort


def extract_mbl(pdf_path: str) -> dict:
    data = call_gemini(MBL_PROMPT, pdf_path=pdf_path, max_output_tokens=16384)
    # Normalize container IDs (PDFs sometimes have spaces like "MEDU 4867820")
    for c in data.get("containers", []):
        c["id"] = _normalize_container_id(c.get("id", ""))
    _dump_debug_json(pdf_path, "mbl.json", data)
    return data


def extract_packing_list(pdf_path: str) -> dict:
    data = call_gemini(PKG_LIST_PROMPT, pdf_path=pdf_path, max_output_tokens=16384)
    # Fix container IDs: split glued container+seal, remove spaces (safety net)
    for ln in data.get("lines", []):
        raw_cid = ln.get("container_id") or ""
        existing_seal = ln.get("seal") or ""
        cid, seal = _split_container_and_seal(raw_cid, existing_seal)
        ln["container_id"] = cid
        ln["seal"] = seal
    _dump_debug_json(pdf_path, "pkg_list.json", data)
    return data


def extract_invoice(pdf_path: str) -> dict:
    return call_gemini(INVOICE_PROMPT, pdf_path=pdf_path)


# ═══════════════════════════════════════════════════════════════════════════
# CROSS-DOCUMENT VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

def _build_ref(ref_nos: list) -> str:
    """Join multiple refs with +, stripping /XXXX suffixes."""
    cleaned = []
    for r in ref_nos:
        base = str(r).split("/")[0].strip()
        if base and base not in cleaned:
            cleaned.append(base)
    return "+".join(cleaned)


def validate(mbl: dict, pkl: dict, inv: dict) -> list[str]:
    """
    Cross-validate MBL, Packing List, and Invoice.
    Returns a list of status strings like:
      "[OK] REF — MBL(X) = Invoice(X)"
      "[!]  CONTAINER — MSMU5262036 only in MBL"
      "[X]  REF MISMATCH — MBL(X) vs Invoice(Y)"
    """
    results = []

    mbl_ref = _build_ref(mbl.get("ref_nos", []))
    inv_ref = _build_ref([inv.get("sales_ref", "")])
    pkl_ref = _build_ref([pkl.get("sabic_po", "")])

    # --- REF ---
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

    # --- DELIVERY ---
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

    # --- PRODUCT ---
    inv_product = str(inv.get("product", "")).upper()
    pkl_products = set()
    for line in pkl.get("lines", []):
        pkl_products.add(str(line.get("product", "")).upper())

    for p in pkl_products:
        if inv_product and (inv_product in p or p in inv_product):
            results.append(f"[OK] PRODUCT — Invoice({inv_product}) ~ Packing List({p})")
        elif inv_product:
            results.append(f"[!]  PRODUCT — Invoice({inv_product}) vs Packing List({p})")

    # --- CONTAINERS ---
    mbl_cids = {c["id"] for c in mbl.get("containers", [])}
    pkl_cids = {ln["container_id"] for ln in pkl.get("lines", [])}

    common = mbl_cids & pkl_cids
    only_mbl = mbl_cids - pkl_cids
    only_pkl = pkl_cids - mbl_cids

    if common:
        results.append(f"[OK] CONTAINERS — {len(common)} matched across MBL & Packing List")
    if only_mbl:
        for c in sorted(only_mbl):
            results.append(f"[!]  CONTAINER — {c} only in MBL (not in Packing List)")
    if only_pkl:
        for c in sorted(only_pkl):
            results.append(f"[!]  CONTAINER — {c} only in Packing List (not in MBL)")

    return results


# ═══════════════════════════════════════════════════════════════════════════
# ROW BUILDER
# ═══════════════════════════════════════════════════════════════════════════

def build_rows(mbl: dict, pkl: dict) -> list[dict]:
    """
    Build Outcome rows — one per Product/Lot per Container.
    """
    ref_no = _build_ref(mbl.get("ref_nos", []))

    # Delivery No: Packing List's Sharq/Petrokemya Delivery first, then STO
    delivery_no = str(pkl.get("delivery_no", "")).strip()
    if not delivery_no:
        delivery_no = str(pkl.get("sto", "")).strip()

    country_code = get_country_code(mbl.get("port_of_loading", ""))

    # MBL container lookup for type
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
            "container_ref":   f"{cid}+{ref_no}",
            "container_type":  ctype,
        })

    return rows