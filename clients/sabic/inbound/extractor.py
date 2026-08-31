
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


# A truncated container id: 4 letters + FEWER than 7 digits. A correctly
# read container id is always exactly 4 letters + 7 digits (ISO 6346), so
# this shape only ever shows up when a page break cut the number in half.
_SHORT_ID_RE = re.compile(r'^[A-Z]{4}\d{1,6}$')
# A bare digit fragment with no letters at all. No real container id is
# ever digits-only, so this shape is never a legitimate independent row —
# it's always the missing tail of the previous row's truncated id.
_DIGIT_FRAGMENT_RE = re.compile(r'^\d{1,6}$')


def _repair_split_container_ids(rows: list) -> list:
    """
    Safety net for a page break that splits a container ID itself, not just
    the row it's on — the first few characters end one page's table, the
    remaining digits open the next page's table as if they were their own
    row (seen on the SABIC-direct/US export layout in PKG_LIST_PROMPT).
    Telling Gemini to join these in the prompt alone isn't reliable — the
    prompt also tells it to transcribe every row exactly as printed, and
    that instruction tends to win. This deterministically joins a truncated
    id with the very next row's digit-only fragment whenever the join
    produces a valid 11-character id, and drops the now-redundant fragment
    row (it never carries any weight/bag data of its own — the real row's
    data already landed on the truncated-id row).
    """
    repaired = []
    skip_next = False
    for i, row in enumerate(rows):
        if skip_next:
            skip_next = False
            continue

        cid = (row.get("container_id") or "").strip().upper().replace(" ", "")
        if _SHORT_ID_RE.match(cid) and i + 1 < len(rows):
            next_cid = (rows[i + 1].get("container_id") or "").strip().upper().replace(" ", "")
            if _DIGIT_FRAGMENT_RE.match(next_cid):
                joined = cid + next_cid
                if len(joined) == 11 and _CONTAINER_RE.match(joined):
                    row = dict(row)
                    row["container_id"] = joined
                    skip_next = True

        repaired.append(row)
    return repaired


# ═══════════════════════════════════════════════════════════════════════════
# CONTAINER TYPE NORMALIZATION
# ═══════════════════════════════════════════════════════════════════════════

# Checked in order — HC markers first, since a container can be labeled with
# both a cargo-type word AND a high-cube marker (e.g. "40' HC DRY VAN"),
# and high-cube must win in that case.
_HC_KEYWORDS = ("HIGH CUBE", "HIGHCUBE", "HQ", "HC", "9 6", "9X6", "9 X 6", "96")
_FT_KEYWORDS = ("DRY VAN", "DV", "ST", "GP", "GENERAL", "DRY", "FT")


def normalize_container_type(raw: str) -> str:
    """
    Collapse the free-text MBL container type into exactly one of
    "40HC" / "40FT" / "20FT" — the only three values the OP accepts.

    Rules (confirmed with the client):
      - 20' containers are ALWAYS "20FT" regardless of sub-type
        (DRY/FT/DV/ST/DRY VAN/GEN all mean the same thing here).
      - 40' containers are "40HC" if any high-cube marker is present
        (HC/HQ/HIGH CUBE/9'6" height notation), else "40FT"
        (FT/DV/ST/DRY VAN/GP/GENERAL/bare DRY).
      - An unrecognized 40' string defaults to "40HC"; an unrecognized
        20' string defaults to "20FT" (every 20' sub-type already maps
        there, so this is really just "no length token matched inside
        a 20' family string").
    """
    cleaned = re.sub(r'[^A-Z0-9 ]', ' ', (raw or "").upper())
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()

    is_40 = "40" in cleaned
    is_20 = "20" in cleaned

    if is_20 and not is_40:
        return "20FT"

    if is_40:
        for kw in _HC_KEYWORDS:
            if kw in cleaned:
                return "40HC"
        for kw in _FT_KEYWORDS:
            if kw in cleaned:
                return "40FT"
        # Unmatched 40' — default per business rule.
        return "40HC"

    # No length token detected at all — nothing to normalize against.
    return cleaned or "??"


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
This document uses ONE of four header layouts. Identify which one FIRST, then
apply ONLY that layout's rules below — the layouts use overlapping words
("Delivery") for different things, so do not mix rules across layouts.

LAYOUT A — Sharq / Petrokemya style: labels are prefixed with a plant name
("Sharq Order/STO:", "Petrokemya Delivery:", etc.) AND a separate "Sabic PO:"
/ "Sabic Delivery:" label also appears.
  - "Sabic PO:" → sabic_po — base number only (e.g. "4506618575" not
    "4506618575 000010").
  - "Sabic Delivery:" → sabic_delivery — this is the PRIMARY delivery number
    for this layout. A separate plant-prefixed "Sharq Delivery:" /
    "Petrokemya Delivery:" line, if present, is NOT the delivery number —
    ignore it; only "Sabic Delivery:" counts.
  - Order/STO label ("Sharq Order/STO:" etc.) → sto.
  - Leave delivery_no empty for this layout (sabic_delivery is what's used).

LAYOUT B — KNC / Korea Nexlene style: bare, unprefixed labels — "SO/STO:",
"Delivery:", "Shipment:", "KNC SO:" — usually with a "KNC" or "Korea Nexlene"
logo on the page. This layout has NO "Sabic PO:" or "Sabic Delivery:" label
anywhere — leave sabic_po and sabic_delivery empty, do not guess a value.
  - "SO/STO:" → sto
  - "Delivery:" → delivery_no. On THIS layout, the bare "Delivery:" label
    IS the delivery number (Layout A's "ignore bare Delivery" rule does not
    apply here — that rule is only for when a separate "Sabic Delivery:"
    label also exists, which Layout B never has).
  - "Shipment:" is a DIFFERENT field, printed a few lines below "Delivery:" —
    do NOT use it for delivery_no, they are never the same number.
  - "KNC SO:" → not needed, ignore it.

  Numeric shape check (these four numbers are easy to mix up when the rows
  sit close together — use digit COUNT to confirm you picked the right one):
    SO/STO   is REF NO 
    Delivery is Actual Delivery No
    Shipment          ← do NOT put this one in delivery_no
    KNC SO   


  WORKED EXAMPLE (read the four header rows top to bottom, one label per row):
    SO/STO:      4506639516
    Delivery:    809153788
    Shipment:    9800715
    KNC SO:      1000474436
  Correct extraction: sto = "4506639516", delivery_no = "809153788".
  "9800715" and "1000474436" are not used anywhere.

LAYOUT C — SABIC-direct / US export style: the header block is titled
"EXPORT REFERENCES" and lists "Invoice No.:", "Sales Order No.:",
"Shipment No.:", "Customer PO No.:". There is NO "SO/STO:" label and NO
"Sabic PO:"/"Sabic Delivery:" label anywhere on this layout — and unlike
Layout A/B, there is no document-level delivery field in the header block
at all (the delivery number instead prints on every row of the table —
see "ALTERNATIVE TABLE LAYOUT 2" below).
  - "Sales Order No.:" → sto (e.g. "4506627106" — same "450..." numbering
    pattern as every other layout's sales-order field).
  - Leave sabic_po and sabic_delivery empty — this layout never has them.
  - "Shipment No.:" → not needed, ignore it. It is a different reference
    from Sales Order No. and is NOT a delivery number.
  - Leave delivery_no empty for this layout. Python fills it in
    automatically from the table's per-row "delivery_number" field (see
    "ALTERNATIVE TABLE LAYOUT 2" below) — you don't need to do this
    yourself, just get delivery_number right on each row.
How to detect: header block titled "EXPORT REFERENCES", with "Sales Order
No.:"/"Shipment No.:" and NO "SO/STO:" and NO "Sabic PO:"/"Sabic
Delivery:" anywhere on the page.

LAYOUT D — dual reference-block style: TWO separate label blocks sit
side by side in the header, each with its OWN "Delivery:" line — a left
block headed "Order/STO:" (with its own "Delivery:" and "Shipment:"
directly under it) and a right block headed "PO:" (with a DIFFERENT
"Delivery:" and "Shipment:" directly under it). Neither block uses the
word "Sabic" or a plant name anywhere — that's what tells this apart
from Layout A, which always has a literal "Sabic PO:"/"Sabic Delivery:"
label. This means the page has TWO "Delivery:" labels total, one per
block — do not just grab the first one you see.
  - "Order/STO:" (left block) → sto.
  - "PO:" (right block) → sabic_po — base number only, drop the trailing
    sub-item suffix (e.g. "4506636062 000010" → "4506636062").
  - "Delivery:" under the RIGHT ("PO:") block → sabic_delivery — this is
    the delivery number that matches the MBL and Invoice. Leave
    delivery_no empty for this layout (same convention as Layout A).
  - "Delivery:" under the LEFT ("Order/STO:") block is a DIFFERENT,
    unrelated delivery reference (tied to the sales order, not the
    shipment) — do NOT use it for sabic_delivery, and do not confuse it
    with the right block's Delivery even though both say "Delivery:".
  - Both "Shipment:" values → not needed, ignore them.
  WORKED EXAMPLE:
    Order/STO:  4506636063        PO:        4506636062 000010
    Delivery:   809175393         Delivery:  809174008
    Shipment:   9810275           Shipment:  0009809604
  Correct extraction: sto = "4506636063", sabic_po = "4506636062",
  sabic_delivery = "809174008". "809175393" (the left block's Delivery)
  is NOT used anywhere — using it instead of "809174008" is the single
  most common mistake on this layout.
How to detect: a left block "Order/STO:"/"Delivery:"/"Shipment:" AND a
right block "PO:"/"Delivery:"/"Shipment:" both present, side by side,
with NO "Sabic PO:"/"Sabic Delivery:" literal label and NO plant-name
prefix anywhere on the page.

- Total pallets: some layouts print a summary line like "112 PALLETIZED 3920 BAGS(of 98 MT)"
  (often near "PKG DESCRIPTION"). If present, extract the leading number (112) as
  "total_pallets". This is a DOCUMENT-WIDE total, not a per-row value — it only
  appears once. If no such line exists, set "total_pallets" to 0.
- Total bags: some layouts (e.g. Layout C / SABIC-direct / US export) print a
  document-wide package count in the header, labeled "No of Packages:" (a
  plain number, e.g. "No of Packages: 7920") — this is the TOTAL BAG COUNT
  for the whole shipment, not a per-row value. If present, extract it as
  "total_bags". If no such field exists, set "total_bags" to 0.

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
- "product": Material name (e.g. "LLDPE 318BJ 149"). Preserve special
  characters EXACTLY as printed — a trademark symbol (™) must stay as the
  actual ™ character, NEVER spelled out as "TM"/"(TM)". Same for ® and ©.
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
- "delivery_number": ONLY set this if the table itself has a "Delivery
  Number" column printed on THIS row (this is Layout C / "ALTERNATIVE
  TABLE LAYOUT 2" below — no other layout's table has this column). Empty
  string "" for every other layout — they don't have this column at all,
  don't guess a value for it.

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
- The "UNIT" column here just shows "MT" — that's a unit-of-measure label
  (how the weight is measured), NOT a per-row pallet count. Set
  "pallet_qty" to 0 for every row on this layout, same as the standard
  layout's rule above ("Unit column shows only MT → pallet_qty = 0").
  Do NOT copy the BAGS value into pallet_qty — they are different numbers
  and must never be equal to each other in your output.
  (If a document-wide pallet total is printed elsewhere as a summary line,
  it's captured separately as "total_pallets" — see HEADER FIELDS above.
  Python computes each row's real pallet_qty from that total; you don't
  need to do that math yourself, just leave pallet_qty as 0 here.)

How to detect: if the column headers include "Grade" and "BATCH" as separate columns,
OR if the seal numbers start with "FJ" or "M" followed by digits, use this mapping.

══════════════════════════════════════════
ALTERNATIVE TABLE LAYOUT 2 — SABIC-direct / US export style
══════════════════════════════════════════
Columns: Item | Container Number | Product Description | Package Type | Delivery Number | Batch Number | HAZMAT UN & LABEL, PAGE NO. | Export HS No. | COO | Quantity/UOM | Gross Weight KGS | Net Weight KGS | Dimensions...

Key differences from the standard layout:
- Container Number is a SINGLE value in its own column — there is NO seal
  number anywhere on this layout (no stacking, no separate seal column).
  Set "seal" to empty string "" for every row.
- EVERY row already carries its own complete container number printed
  directly on it — this layout never has continuation/orphan rows the way
  the standard layout does. There is also NO "Verified Gross Mass" column
  at all — set has_vgm = true for ALL rows (same reasoning as the KNC
  layout above: no VGM column to gate on, so every row stands on its own
  and must not be dropped for lack of one).
- "Batch Number" column → lot. There is no "PKG CODE"/"Grade" column on
  this layout — set pkg_code to empty string "".
- There is NO "Bags" count column and no pallet/unit count column at all.
  "Quantity/UOM" here just repeats the weight (e.g. "24750.000 KG" is the
  same number as that row's Net Weight KGS) — it is NOT a bag or pallet
  count. Set both "bags" and "pallet_qty" to 0 for every row on this
  layout — there is nothing in the document to derive either from.
- "Delivery Number" column → this row's "delivery_number" field (see
  TABLE STRUCTURE above). Python fills in the document-level "delivery_no"
  field from this automatically — you don't need to do that yourself,
  just get delivery_number right on each row.

Container ID split across a page break: occasionally the container number
itself — not just the row — is cut in half by a page break, with the
first few characters at the bottom of one page and the remaining digits
sitting at the very top of the next page's table, ABOVE the next real
Item row. A container ID is always 4 letters + 7 digits = 11 characters —
if the container number at the bottom of a page is SHORTER than that,
look at the top of the next page: if there are digits sitting there that
do NOT look like a new Item number (Item numbers on this layout are small
integers like 11, 12, 13), those digits are the missing tail of the
previous page's container number — join them into one 11-character ID.
Example: a page ends with container number "HLBU3289" (only 8 characters,
incomplete) on an Item-12 row; the next page opens with "988" sitting
above the next real row — "988" is not an Item number, it's the missing
digits. The correct container_id for that row is "HLBU3289988" (4 letters
+ 7 digits), not the truncated "HLBU3289".

How to detect this layout: the table header row lists "Item" and
"Container Number" as separate columns (not stacked with a seal), there
is NO "Verified Gross Mass" column, and there is NO "Bags" count column —
only a "Quantity/UOM" column that duplicates the weight.

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
WORKED EXAMPLE 2 — several 2-lot containers in a row (the case that gets
mis-transcribed most often — do NOT let this pattern make you merge rows
or shift a continuation row's numbers onto a different container)
══════════════════════════════════════════
    TRHU6323910   ...   0062126564   12 PAL   720   18.312 MT   29.704 MT   18.0 MT
      1128359
                  ...   0062131595    5 PAL   300    7.630 MT                7.5 MT

    MRSU9878755   ...   0062131595   17 PAL  1020   25.942 MT   29.704 MT   25.5 MT
      1096929

    CAAU9119094   ...   0062126564    8 PAL   480   12.208 MT   29.704 MT   12.0 MT
      1096749
                  ...   0062131595    9 PAL   540   13.734 MT               13.5 MT

    MRSU7592757   ...   0062131595   17 PAL  1020   25.942 MT   29.704 MT   25.5 MT
      1096719

This is SIX physical rows → output EXACTLY six array entries, never four:
  Row 1: TRHU6323910, HAS VGM → open TRHU6323910. current = TRHU6323910.
  Row 2: no container ID, NO VGM, lot 0062131595, 300 bags → continuation
         of TRHU6323910. Output its OWN entry: container_id="",
         has_vgm=false, bags=300. Do NOT drop this row, and do NOT let its
         numbers (300 bags, lot 0062131595) end up on the MRSU9878755 row
         below — MRSU9878755 is a brand-new, unrelated container that
         happens to come next; it has its own 1020-bag row further down
         in the document and nothing to do with TRHU6323910's leftover lot.
  Row 3: MRSU9878755, HAS VGM → open MRSU9878755 (separate container,
         separate 1020-bag entry — untouched by row 2 above it).
  Row 4: CAAU9119094, HAS VGM → open CAAU9119094.
  Row 5: no container ID, NO VGM, lot 0062131595, 540 bags → continuation
         of CAAU9119094, its own entry — again, must NOT be merged into or
         overwrite the next container's (MRSU7592757's) row.
  Row 6: MRSU7592757, HAS VGM → open MRSU7592757, its own separate
         1020-bag entry.

General rule: every VGM-less, container-ID-less row is its OWN array
entry belonging to the container that opened directly above it — never
skip emitting it, and never let its data replace or merge into ANY other
row, including the very next VGM row. The number of array entries you
return must equal the number of physical table rows in the document,
counted rows included.

══════════════════════════════════════════
OUTPUT FORMAT
══════════════════════════════════════════
{
  "delivery_no": "string",
  "sto": "string",
  "sabic_po": "string",
  "sabic_delivery": "string",
  "total_pallets": 0,
  "total_bags": 0,
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
      "weight_unit": "MT",
      "delivery_number": "string or empty"
    }
  ]
}"""

INVOICE_PROMPT = """You are a shipping-document data extractor. Extract fields from this Commercial Invoice PDF. Return ONLY JSON — no markdown.

- "invoice_no": Invoice Number.
- "sales_ref": Sales Ref base number before slash (e.g. "4506618575" from "4506618575/0010").
- "delivery_no": Delivery No base number before slash.
- "product": Product description from line items. This field is placed
  verbatim into the final output, so get it character-for-character exact.

  Trademark/registered/copyright symbols: on many invoices these render as
  a small raised mark stuck directly against the word with no space (e.g.
  a raised "TM" right after "FORTIFY"). That raised mark is the ™ symbol —
  it is NOT the two letters "T" and "M". Self-check before you output this
  field: if what you're about to write contains "TM" glued onto a word with
  no space before it (e.g. "FORTIFYTM"), that is this exact mistake — fix
  it by replacing "TM" with the single character ™ (e.g. "FORTIFY™"). Same
  correction for a raised "R" → ® and a raised "C" → ©.
  Example: invoice shows FORTIFY with a raised trademark mark, followed by
  "C0570D 145" → output "product": "FORTIFY™ C0570D 145", never
  "FORTIFYTM C0570D 145".
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


def _s(value) -> str:
    """
    Safely stringify a Gemini-extracted field. A JSON null comes back as
    Python None, and plain str(None) == "None" — a non-empty, truthy string
    that silently poisons every `x or fallback` / `if x:` check downstream.
    Use this anywhere a string field comes straight from Gemini's JSON.
    """
    return "" if value is None else str(value)


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

    data["rows"] = _repair_split_container_ids(data.get("rows", []))
    _dump_json(pdf_path, "pkg_list_repaired.json", data)

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
            "delivery_number": _s(row.get("delivery_number")).strip(),
        })

    document_delivery_no = _s(data.get("delivery_no", "")).strip()
    if not document_delivery_no:
        # Layout C's table carries "delivery_number" per row instead of a
        # document-level header field (see PKG_LIST_PROMPT) — asking Gemini
        # to mirror that into delivery_no itself proved unreliable (the same
        # class of cross-referencing miss as the split container-id case),
        # so Python does the mirroring deterministically instead.
        for ln in flat_lines:
            if ln["delivery_number"]:
                document_delivery_no = ln["delivery_number"]
                break

    result = {
        "delivery_no":    document_delivery_no,
        "sto":            data.get("sto", ""),
        "sabic_po":       data.get("sabic_po", ""),
        "sabic_delivery": data.get("sabic_delivery", ""),
        "total_pallets":  _num(data.get("total_pallets"), 0),
        "total_bags":     _num(data.get("total_bags"), 0),
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
# MBL CROSS-REFERENCE — REPAIR MISSING/TRUNCATED CONTAINER IDS
# ═══════════════════════════════════════════════════════════════════════════

def repair_container_ids_via_mbl(mbl: dict, pkl: dict) -> dict:
    """
    Safety net for a packing-list row whose container_id came back
    missing (None) or truncated (doesn't match the 4-letter+7-digit
    format) — usually a page break that confused the vision model badly
    enough that _repair_split_container_ids()'s "next row is a bare digit
    fragment" pattern doesn't apply (the fragment gets lost entirely
    instead of showing up as its own row). The MBL already has the
    complete, correct container list for this shipment, so cross-
    referencing against it can resolve most of these:

      1. PREFIX MATCH: a truncated id (e.g. "HLBU3289") is the literal
         start of exactly one not-yet-claimed MBL container id — a
         truncation can only ever be a prefix of its real id, so this is
         safe whenever there's exactly one candidate.
      2. ELIMINATION: after every row with a complete id (or one just
         resolved by #1) is set aside, if there's EXACTLY ONE MBL
         container id left unclaimed and EXACTLY ONE packing-list row
         still missing an id entirely, they must be each other.
      3. DUPLICATE CLEANUP: if, after #1 and #2, EVERY real MBL container
         is already claimed by some other row, a row still left over
         cannot be a genuine distinct container — there's no room for one
         in the known inventory. If that row's (lot, net_weight,
         gross_weight) exactly matches an already-claimed row, Gemini
         extracted the same physical line twice — once correctly, once
         garbled with the id lost (typically a leftover page-break
         fragment, e.g. a stray "988" it couldn't attach anywhere,
         smeared onto a copy of the next real row's data). Drop it so it
         doesn't silently double-count that line's weight/pallets/bags.

    Deliberately conservative throughout: never assigns or drops on a
    guess. #1/#2 never assign when more than one candidate remains
    possible (weight/lot alone are often NOT unique fingerprints — e.g.
    two DIFFERENT real containers in the same shipment can legitimately
    share both the same lot and the same net weight). #3 only drops when
    the inventory is provably full AND there's an exact duplicate — a
    row that's merely unresolved, with no matching twin, is left alone
    rather than discarded on a hunch.
    """
    lines = pkl.get("lines", [])
    mbl_ids = [c["id"] for c in mbl.get("containers", []) if c.get("id")]
    if not lines or not mbl_ids:
        return pkl

    def is_complete(cid) -> bool:
        return bool(cid) and bool(_CONTAINER_RE.match(str(cid)))

    claimed = {ln["container_id"] for ln in lines if is_complete(ln.get("container_id"))}
    broken = [
        (i, str(ln.get("container_id") or "").strip().upper())
        for i, ln in enumerate(lines)
        if not is_complete(ln.get("container_id"))
    ]

    fixed = 0

    # Pass 1 — prefix match for truncated (non-empty) ids.
    for i, raw in list(broken):
        if not raw:
            continue
        candidates = [cid for cid in mbl_ids if cid not in claimed and cid.startswith(raw)]
        if len(candidates) == 1:
            lines[i]["container_id"] = candidates[0]
            claimed.add(candidates[0])
            broken.remove((i, raw))
            fixed += 1

    # Pass 2 — elimination for whatever's left (empty, or a truncated id
    # that didn't prefix-match anything).
    remaining_mbl = [cid for cid in mbl_ids if cid not in claimed]
    if len(remaining_mbl) == 1 and len(broken) == 1:
        i, _ = broken[0]
        lines[i]["container_id"] = remaining_mbl[0]
        broken.pop()
        remaining_mbl = []
        fixed += 1

    if fixed:
        print(f"  [ID REPAIR] Resolved {fixed} missing/truncated container id(s) via MBL cross-reference")

    # Pass 3 — every real container is already accounted for, so anything
    # still broken can only be noise. Drop it if (and only if) it's an
    # exact duplicate of an already-claimed row's data.
    dropped = set()
    if not remaining_mbl and broken:
        def fingerprint(ln):
            return (ln.get("lot"), _num(ln.get("net_weight"), 0), _num(ln.get("gross_weight"), 0))

        claimed_fingerprints = {
            fingerprint(ln) for ln in lines if is_complete(ln.get("container_id"))
        }
        for i, _ in broken:
            if fingerprint(lines[i]) in claimed_fingerprints:
                dropped.add(i)

    if dropped:
        print(f"  [ID REPAIR] Dropped {len(dropped)} row(s) with no resolvable container id that exactly "
              f"duplicate another row's lot/weight — every real container is already accounted for, so this "
              f"can only be a hallucinated re-extraction, not a genuine extra line")
        lines = [ln for i, ln in enumerate(lines) if i not in dropped]

    still_broken = len(broken) - len(dropped)
    if still_broken:
        print(f"  [ID REPAIR] {still_broken} row(s) still have no resolvable container id — left as-is, needs manual review")

    pkl["lines"] = lines
    return pkl


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

    # Seal fallback for when a container's last packing-list row gets
    # stolen away before it's used as the seal source (see below).
    mbl_seal_map = {c["id"]: c.get("seal", "") for c in mbl.get("containers", []) if c.get("id")}

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

            # Under-container is short AND over-container has excess.
            # Require over_cid to have MORE THAN ONE row before stealing —
            # a steal must never fully drain a real container down to zero
            # rows (that container would then vanish entirely from the
            # output instead of just losing one misattached lot).
            if (u_act < u_exp and o_act > u_act
                    and len(container_lines_map[over_cid]) > 1):
                fi = container_lines_map[over_cid][0]
                stolen = lines[fi]
                # Only require the under-container to become correct
                if u_act + stolen["bags"] == u_exp:
                    print(f"  [CROSS-CHECK FIX] Lot {stolen['lot']} ({stolen['bags']} bags): "
                          f"{over_cid} → {under_cid}")
                    stolen["container_id"] = under_cid
                    if container_lines_map[under_cid]:
                        stolen["seal"] = lines[container_lines_map[under_cid][0]]["seal"]
                    else:
                        # under_cid was fully drained by an earlier steal
                        # this same round (a 3+ container cascade) — no
                        # packing-list row left to read its seal from.
                        stolen["seal"] = mbl_seal_map.get(under_cid, stolen["seal"])
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
        if not r:
            continue
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

    if not mbl_ref and inv_ref and pkl_ref:
        # Some carriers (e.g. Hapag) never print a Sales Order/STO number
        # on the MBL at all — compare Invoice vs Packing List directly so
        # this cross-check doesn't just go silent when that happens.
        if inv_ref == pkl_ref:
            results.append(f"[OK] REF — Invoice({inv_ref}) = Packing List({pkl_ref}) (no ref on MBL — expected for this carrier)")
        else:
            results.append(f"[!]  REF — Invoice({inv_ref}) vs Packing List({pkl_ref}) (no ref on MBL)")

    mbl_del = _s(mbl.get("delivery_no", "")).split("/")[0]
    inv_del = _s(inv.get("delivery_no", "")).split("/")[0]
    pkl_sabic_del = _s(pkl.get("sabic_delivery", "")).split("/")[0]

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
    pkl_del = _s(pkl.get("delivery_no", "")).split("/")[0]
    if mbl_del and pkl_del and not pkl_sabic_del:
        if mbl_del == pkl_del:
            results.append(f"[OK] DELIVERY — MBL({mbl_del}) = Packing List({pkl_del})")
        else:
            results.append(f"[!]  DELIVERY — MBL({mbl_del}) vs Packing List({pkl_del})")

    inv_product = _s(inv.get("product", "")).upper()
    pkl_products = set()
    for line in pkl.get("lines", []):
        pkl_products.add(_s(line.get("product", "")).upper())
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

    # Bags cross-check: on layouts with no per-row bags column (e.g. the
    # SABIC-direct/US export layout, common with Hapag), bags come from the
    # MBL's per-container counts instead (see build_rows()) — this confirms
    # those add up to the packing list's own document-wide total, the same
    # arithmetic check printed on the document itself (e.g. 8 containers ×
    # 990 bags = 7920 = "No of Packages").
    mbl_bags_sum = sum(_num(c.get("bags"), 0) for c in mbl.get("containers", []))
    pkl_total_bags = _num(pkl.get("total_bags"), 0)
    if mbl_bags_sum and pkl_total_bags:
        if mbl_bags_sum == pkl_total_bags:
            results.append(f"[OK] BAGS — MBL containers sum({mbl_bags_sum}) = Packing List total({pkl_total_bags})")
        else:
            results.append(f"[!]  BAGS — MBL containers sum({mbl_bags_sum}) vs Packing List total({pkl_total_bags})")

    return results


# ═══════════════════════════════════════════════════════════════════════════
# ROW BUILDER
# ═══════════════════════════════════════════════════════════════════════════

def build_rows(mbl: dict, pkl: dict, inv: dict, eta_date: str = "") -> list[dict]:
    ref_no = _build_ref(mbl.get("ref_nos", []))
    if not ref_no:
        # Some carriers' MBLs (e.g. Hapag) never print a Sales Order/STO
        # number at all — fall back to the packing list's own Sales
        # Order/STO field, same precedence validate()'s pkl_ref already
        # uses (sabic_po first, then sto). The invoice's sales_ref should
        # independently match this too — that comparison already happens
        # in validate() via inv_ref, unaffected by this fallback.
        ref_no = _s(pkl.get("sabic_po", "")).strip() or _s(pkl.get("sto", "")).strip()

    # MBL's delivery_no is a single, unambiguous field (one label, one
    # value) — far more reliable than the packing list's header, which has
    # to be correctly classified into one of several overlapping layouts
    # first. validate() already treats the MBL's delivery as authoritative
    # when cross-checking against the packing list, so prefer it here too;
    # only fall back to the packing list's own fields when the MBL doesn't
    # have one at all (e.g. some carriers never print a delivery number).
    delivery_no = (
        _s(mbl.get("delivery_no")).split("/")[0].strip()
        or pkl.get("sabic_delivery")
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

    # Product is shipment-level, sourced from the invoice verbatim (no
    # normalization — client wants it placed exactly as printed, incl. ™),
    # and applied to every row, same as ref_no/eta_date.
    product = _s(inv.get("product", "")).strip()

    mbl_map = {}
    for c in mbl.get("containers", []):
        mbl_map[c["id"]] = {
            "type": c.get("type", ""),
            "seal": c.get("seal", ""),
            "bags": _num(c.get("bags"), 0),
        }

    lines = pkl.get("lines", [])

    # One MBL container can appear as MULTIPLE packing-list rows (the same
    # physical container split across two different lots/batches — a real,
    # common shape, not an error). The MBL's bag count is for the WHOLE
    # container, so when falling back to it, a shared container's bags must
    # be split across its rows proportional to each row's weight share —
    # not copied in full onto every row, which would double (or n-)count
    # it. Pre-sum each container's total packing-list weight up front so
    # _effective_bags() can compute that share.
    container_weight_totals: dict = {}
    for ln in lines:
        wt_kg = _to_kg(ln.get("net_weight", 0), ln.get("weight_unit", "MT"))
        cid = ln["container_id"]
        container_weight_totals[cid] = container_weight_totals.get(cid, 0) + wt_kg

    def _effective_bags(ln: dict) -> int | float:
        # Some carriers (e.g. Hapag) print per-container bag counts on the
        # MBL, but the packing list itself has no bags column at all (the
        # SABIC-direct/US export layout — PKG_LIST_PROMPT's Layout C /
        # "ALTERNATIVE TABLE LAYOUT 2" — sets bags=0 for every row there).
        # Fall back to the matching MBL container's bag count whenever the
        # packing list didn't give one. Computed once and reused for both
        # the pallet-ratio math below and each row's final pkg_qty output,
        # so both use the exact same number.
        bags = _num(ln.get("bags"), 0)
        if bags:
            return bags
        mbl_entry = mbl_map.get(ln["container_id"])
        if not mbl_entry or not mbl_entry["bags"]:
            return 0
        total_wt = container_weight_totals.get(ln["container_id"], 0)
        if not total_wt:
            return 0
        row_wt = _to_kg(ln.get("net_weight", 0), ln.get("weight_unit", "MT"))
        return round(mbl_entry["bags"] * (row_wt / total_wt))

    # ── Pallet count: three possible source formats ───────────────────────
    # (1) Given per row already (Unit column had "<N> PAL") — trust it as-is.
    # (2) Only a document-wide total is printed (e.g. "112 PALLETIZED 3920
    #     BAGS") and rows have a bag count — back-compute each row's share
    #     from its bags using the shipment's bags-per-pallet ratio.
    # (3) Same document-wide total, but rows have NO bag count at all —
    #     fall back to distributing by each row's share of the total NET
    #     WEIGHT instead. Bags is tried first since it's the more direct/
    #     reliable signal when present.
    # Only fall into (2)/(3) when NOT a single row already has a per-row
    # count — a partially-filled document keeps whatever's there rather
    # than guessing.
    all_rows_have_pallets = bool(lines) and all(_num(ln.get("pallet_qty"), 0) > 0 for ln in lines)
    total_bags_sum = sum(_effective_bags(ln) for ln in lines)
    total_weight_sum = sum(_to_kg(ln.get("net_weight", 0), ln.get("weight_unit", "MT")) for ln in lines)
    total_pallets = _num(pkl.get("total_pallets"), 0)

    bags_per_pallet = None
    weight_per_pallet = None
    if not all_rows_have_pallets and total_pallets:
        if total_bags_sum:
            bags_per_pallet = total_bags_sum / total_pallets
        elif total_weight_sum:
            weight_per_pallet = total_weight_sum / total_pallets

    rows = []
    for ln in lines:
        cid = ln["container_id"]
        seal = ln.get("seal", "")
        ctype = ""

        if cid in mbl_map:
            ctype = mbl_map[cid].get("type", "")
            if not seal:
                seal = mbl_map[cid].get("seal", "")
        ctype = normalize_container_type(ctype)

        # Convert to KG
        weight_unit = ln.get("weight_unit", "MT")
        net_wt_kg = _to_kg(ln.get("net_weight", 0), weight_unit)
        gross_wt_kg = _to_kg(ln.get("gross_weight", 0), weight_unit)
        bags = _effective_bags(ln)

        # Bags vs Big Bags
        pkg_type = _determine_pkg_type(net_wt_kg, bags)

        # Pallet qty — per row if given, else back-computed from the total
        # (by bags if available, else by weight — see the comment above).
        raw_pallet_qty = _num(ln.get("pallet_qty"), 0)
        if all_rows_have_pallets:
            pallet_qty = raw_pallet_qty
        elif bags_per_pallet:
            pallet_qty = round(bags / bags_per_pallet)
        elif weight_per_pallet:
            pallet_qty = round(net_wt_kg / weight_per_pallet)
        else:
            pallet_qty = raw_pallet_qty

        rows.append({
            "ref_no":          ref_no,
            "delivery_no":     delivery_no,
            "container_no":    cid,
            "product":         product,
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
            "eta_date":        eta_date,
        })

    return rows
