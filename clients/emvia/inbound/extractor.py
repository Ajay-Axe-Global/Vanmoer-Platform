"""
Emvia Inbound — Extraction, validation, and row-building. Covers TWO
warehouses, each with its own Packing List layout:

  - Warehouse 1147 (KRUIPIN): VE Staal B.V.'s "Packing List Enclosure"
    bundle table (PKG_LIST_PROMPT / extract_packing_list()) — steel bars,
    PDF or Excel source (see excel_extractor.py for the Excel path).
  - Warehouse NNRC 660: Chevron Phillips-style "Export Packing List"
    (PKG_LIST_NNRC_PROMPT / extract_packing_list_nnrc()) — resin in bags on
    pallets, PDF only. task.py routes to the right one by the UI's
    warehouse selection.

The MBL, by contrast, is shared across both warehouses and is NOT tied to
either layout above — different shipments arrive via different ocean
carriers (Hapag-Lloyd, MSC, Maersk, ...) regardless of which warehouse
they're headed to, so MBL extraction is a two-call pipeline exactly like
Vinmar Inbound's: identify_carrier() names the carrier, then extract_mbl()
dispatches to that carrier's own tuned prompt (CARRIER_MBL_PROMPTS),
falling back to a generic prompt for any carrier not yet onboarded. Add a
new carrier by adding its own prompt + a CARRIER_MBL_PROMPTS entry — do not
bend an existing carrier's prompt to also cover a different one's layout.

Country-code lookup, container-type normalization, and MT/KG conversion are
shared with Sabic/Vinmar Inbound via helpers/doc_common.py.

Batch numbers on the 1147 Packing List are cross-checked/corrected against
the PDF's own text layer (see _pdf_batch_numbers below) rather than trusted
purely from the Gemini call — a page break splitting one bundle row's
bundle-number line from its own "// <batch>" line onto the NEXT page proved
unreliable for the model to re-pair correctly even with explicit prompt
instructions (it kept attaching the orphaned batch number to the following
row instead of the row it actually belongs to). Since that document has a
real embedded text layer, not a scanned image, pdfplumber reads those "//"
tokens with plain regex — no vision/layout judgement involved — so it's used
as the source of truth for batch_no whenever its count of tokens matches the
number of bundle rows Gemini returned (see extract_packing_list()).
"""

import re

import pdfplumber

from helpers.doc_common import (
    dump_json,
    fix_container_id,
    get_country_code,
    normalize_container_type,
    num,
    s,
    to_kg,
)
from helpers.gemini_client import call_gemini

# ═══════════════════════════════════════════════════════════════════════════
# MBL CARRIER IDENTIFICATION (call #1) — shared across both warehouses
# ═══════════════════════════════════════════════════════════════════════════

CARRIER_ID_PROMPT = """You are looking at a Master Bill of Lading / Sea Waybill PDF. Identify who \
ISSUED it — the company whose logo/name is in the title block and who \
signs it "AS A CARRIER" (or as forwarding agent) at the bottom, and any \
"CARRIER:" field. Return ONLY a JSON object, no markdown.

Normalize the name to exactly one of these if it matches (case-sensitive, \
use this exact spelling): "HAPAG-LLOYD", "MSC", "MAERSK", "CMA CGM", "COSCO", \
"ONE", "EVERGREEN", "YANG MING", "ZIM", "OOCL", "HMM", "PIL", "BORCHARD LINES".

Aliases to watch for: "Mediterranean Shipping Company" / "MSC Mediterranean \
Shipping Company S.A." -> "MSC". A logo/branding of "ONE" with "Ocean \
Network Express" printed nearby -> "ONE". "HMM CO., LTD." -> "HMM". \
"Orient Overseas Container Line" -> "OOCL". "Maersk A/S" / "Maersk Line" \
-> "MAERSK". "Borchard Lines Limited" -> "BORCHARD LINES".

If the issuer is real but not in that list, return its name as printed on \
the document. If you cannot tell at all, return "UNKNOWN".

{"carrier": "string"}"""


def identify_carrier(pdf_path: str) -> str:
    data = call_gemini(CARRIER_ID_PROMPT, pdf_path=pdf_path, max_output_tokens=256)
    carrier = s(data.get("carrier", "UNKNOWN")).strip().upper()
    return carrier or "UNKNOWN"


# ═══════════════════════════════════════════════════════════════════════════
# CARRIER-SPECIFIC MBL PROMPTS (call #2)
# ═══════════════════════════════════════════════════════════════════════════
# Shared return schema + unit-handling rule spliced into every carrier
# prompt below — the wording only needs to be maintained in one place.

RETURN_SCHEMA = """
Return:
{
  "mbl_no": "string", "port_of_loading": "string", "port_of_discharge": "string",
  "destination_country": "string",
  "containers": [
    {"id": "string", "seal": "string", "type": "string", "gross_weight": 0,
     "gross_weight_unit": "MT", "package_type": "string"}
  ]
}"""

UNIT_RULE = """
⚠️ UNIT — a weight label's parenthesized/adjacent unit is NOT always the
same on every document; some print gross weight already in KGS, others in
MTS/TONS. Read whichever unit is ACTUALLY printed for THIS document and set
"gross_weight_unit" accordingly: "MT" for MTS/MT/TONS/TON, "KG" for KGS/KG.
Return "gross_weight" exactly as printed either way — the unit conversion
happens afterward in code, not by you; do not multiply or divide the number
yourself regardless of which unit it's in."""

PACKAGE_TYPE_RULE = """
⚠️ PACKAGE TYPE — this shipment's cargo is packaged as EITHER bundles
(steel bars/pipes/angles) OR bags (resin/plastic pellets), never both.
Somewhere near the goods description you'll find a package count phrase
naming which one — e.g. "44 BUNDLE(S)", "53 BUNDLES", "13 BUNDLES" ->
"package_type": "BUNDLE"; or "990 BAG(S)", "990 BAGS", "1980 BAG" ->
"package_type": "BAG". Set this SAME value on every container in this
shipment (it's a document-wide property, read it once and apply it to
every container entry) — read whichever word ("BUNDLE"/"BUNDLES" vs
"BAG"/"BAGS") is ACTUALLY printed on THIS document, do not guess from the
commodity name alone. Leave it "" only if genuinely neither word appears
anywhere on the document."""


# Hapag-Lloyd "Multimodal Transport or Port to Port Shipment" Bill of Lading
HAPAG_LLOYD_MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this Hapag-Lloyd \
Bill of Lading PDF (it may span multiple pages — read all of them) and \
return ONLY a JSON object, no markdown, no explanation.

HEADER FIELDS:
- "mbl_no": the "B/L-No." value (top right, e.g. "HLCUB01260673770") — NOT
  the "Carrier's Reference" number printed right next to it, that's a
  different number.
- "port_of_loading": "Port of Loading" value.
- "port_of_discharge": "Port of Discharge" value.
- "destination_country": the country of the "Place of Delivery" / final
  destination city (e.g. Antwerp -> "Belgium") — infer the country from the
  city if only a city is printed.

CONTAINER TABLE — one block per container, in the "Container Nos., Seal
Nos., Marks and Nos." / "Number and Kind of Packages; Description of Goods"
columns. Each block is shaped like:
  <CONTAINER ID>
  SEALS : <SEAL NUMBER>
  <a following unrelated line, e.g. a bare number — see warning below>
  MARKS & NOS: ...
  <container size/type description, e.g. "1 CONT. 20'X8'6" GENERAL PURPOSE
   CONT. SLAC*">
  <N BUNDLES>
  ...
  GROSS WT.(<UNIT>): <value>
  Example:
    "UACU 3944378"
    "SEALS : HLG6350667"
    "033949"
    "1 CONT. 20'X8'6" GENERAL PURPOSE CONT. SLAC*"
    "53 BUNDLES"
    "GROSS WT.(MTS): 24.879"
  -> id "UACU3944378" (strip the space), seal "HLG6350667" (ONLY the token
  printed immediately after "SEALS :" on that same line — this document
  prints exactly one seal number here), type "20'X8'6" GENERAL PURPOSE
  CONT." (the size/type description text, drop the trailing "SLAC*"
  stowage-plan marker), gross_weight 24.879 (the "GROSS WT.(...)" value
  EXACTLY as printed, do NOT convert or rescale it yourself), gross_weight_unit
  "MT" (because the label read "GROSS WT.(MTS)" — see unit rule below).
@@UNIT_RULE@@

⚠️ A bare number on its OWN line right after the "SEALS :" line (e.g.
"033949" in the example above) is NOT a second seal and is NOT part of the
seal value — it belongs to unrelated document text (marks/numbers block
layout), leave it out of "seal" entirely. The seal is only ever the single
token that appears directly on the same line as the "SEALS :" label.

Read every container block on every page — do not stop after the first one.
@@PACKAGE_TYPE_RULE@@
@@RETURN_SCHEMA@@"""


# MSC "Sea Waybill" — main page just refers to an attached "RIDER PAGE" for
# the actual container/goods details (see MSC's BL sample: "PLEASE SEE
# ATTACHED RIDER PAGE(S) FOR DESCRIPTION OF PACKAGES AND GOODS" on the main
# page), so the container table itself is read from that rider page, not
# the front page.
MSC_MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this MSC \
(Mediterranean Shipping Company) Sea Waybill PDF — the front page states \
header fields but refers to an attached "RIDER PAGE" for the actual \
container/cargo table, read every page including the rider page(s) — and \
return ONLY a JSON object, no markdown, no explanation.

HEADER FIELDS (front page):
- "mbl_no": the "SEA WAYBILL No." value (top right, e.g. "MEDUAAU53094").
- "port_of_loading": "PORT OF LOADING" value.
- "port_of_discharge": "PORT OF DISCHARGE" value.
- "destination_country": the country of the "PORT OF DISCHARGE" city (e.g.
  "ANTWERP, BELGIUM" -> "Belgium") — infer the country from the city if
  only a city is printed.

CONTAINER TABLE — on the "RIDER PAGE", in the "Container Numbers, Seal
Numbers and Marks" column, one block per container shaped like:
  <CONTAINER ID>
  <TYPE, e.g. "40' HIGH CUBE">
  SEAL NUMBER:<SEAL NUMBER>
  ...
That SAME row's "Gross Cargo Weight" column (to the right, on the rider
page) gives this container's gross weight, e.g. "25,245.000 KGS." Example:
  "MSDU8106323"
  "40' HIGH CUBE"
  "SEAL NUMBER:353408"
  ... (same row) "25,245.000 KGS."
  -> id "MSDU8106323", type "40' HIGH CUBE", seal "353408", gross_weight
  25245.000 (exactly as printed, do NOT convert or rescale it yourself),
  gross_weight_unit "KG" (because the figure read "KGS.").
@@UNIT_RULE@@

Read every container block on every rider page — a shipment can have more
than one container, each with its own repeating block; do not stop after
the first one.
@@PACKAGE_TYPE_RULE@@
@@RETURN_SCHEMA@@"""


# OOCL (Orient Overseas Container Line) "Sea Waybill" — page 1 prints a
# SUMMARY row (shipment-wide totals, under an "ITN :" reference code that is
# NOT a real container number) that looks structurally like a container row
# but isn't one; the REAL per-container table is on a LATER page under
# "** TO BE CONTINUED ON ATTACHED LIST **". Mistaking the page-1 summary
# for an extra container has been seen to DOUBLE the total gross weight
# (summary row's full shipment total + the real containers' own weights
# both counted).
OOCL_MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this OOCL \
(Orient Overseas Container Line) Sea Waybill PDF — it spans multiple pages, \
and the REAL per-container table is on a LATER page, after a line reading \
"** TO BE CONTINUED ON ATTACHED LIST **", NOT the first page — read every \
page — and return ONLY a JSON object, no markdown, no explanation.

HEADER FIELDS:
- "mbl_no": the "SEA WAYBILL NO. (WAYBILL)" value (e.g. "OOLU2171123450").
- "port_of_loading": "PORT OF LOADING" value.
- "port_of_discharge": "PORT OF DISCHARGE" value.
- "destination_country": the country of the "PORT OF DISCHARGE" / "PLACE OF
  DELIVERY" city (e.g. "ANTWERP, BELGIUM" -> "Belgium").

⚠️ CRITICAL — do not confuse the first page's summary row with a real
container. The first "CNTR. NOS. W/SEAL NOS." / "MARK & NUMBERS" cell on
page 1 typically shows a code starting with "ITN :" followed by a value
that does NOT match the 4-letters+7-digits container ID pattern (e.g.
"ITN : X20260622025380" — starts with a letter+digits, wrong shape and
wrong length for a real container), right next to shipment-wide "TOTAL
PALLETS", "TOTAL NET WEIGHT", and "TOTAL GROSS WEIGHT" figures and a
"** TO BE CONTINUED ON ATTACHED LIST **" notice. That whole block is a
SUMMARY for the ENTIRE shipment — it is NEVER a container of its own, no
matter how it's laid out. Do not extract it as a "containers" entry, and do
not use its TOTAL GROSS WEIGHT figure as any one container's own weight —
it is the sum of every REAL container's weight (a cross-check value only,
never a container's own row).

REAL CONTAINER TABLE — on a later page, one line per container, shaped
like:
  <CONTAINER ID> /<SEAL> / <BAGS COUNT> BAGS /FCL/FCL /<TYPE>/<GROSS
  WEIGHT>KGS;<MEASUREMENT>CBM
  Example: "CSGU6949654 /11225546 / 990 BAGS /FCL/FCL /40HQ/25245.000KGS;
  51.703CBM" -> id "CSGU6949654", seal "11225546", type "40HQ",
  gross_weight 25245.000 (exactly as printed, do NOT convert or rescale it
  yourself), gross_weight_unit "KG" (because the figure read "KGS").
@@UNIT_RULE@@

Read every such line on every "ATTACHED LIST" page — a shipment can have
several containers, each its own line; do not stop after the first one, and
do not stop early just because a page says "DELIBERATELY LEFT BLANK AND
CONTINUE ON NEXT PAGE" (keep reading the following page for more container
lines, or confirm the table has genuinely ended if no more lines follow).
@@PACKAGE_TYPE_RULE@@
@@RETURN_SCHEMA@@"""


# CMA CGM "Bill of Lading for Combined Transport and Port to Port Shipment"
# — numbered field boxes (SHIPPER/EXPORTER (2), DOCUMENT NO (5), etc.), same
# family as other CMA CGM templates, but this one's own trap: the FIRST
# "MARKS AND NUMBERS"/"DESCRIPTION OF GOODS" entry on sheet 1 is a
# shipment-wide summary block (under plain "N/M" marks, with its own
# "TOTAL NET WEIGHT"/"TOTAL GROSS WEIGHT" lines) — the REAL per-container
# rows follow it, spread across this sheet and the next.
CMA_CGM_MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this CMA CGM \
"Bill of Lading for Combined Transport and Port to Port Shipment" PDF — it \
spans multiple sheets, and the container rows continue across them, read \
every sheet — and return ONLY a JSON object, no markdown, no explanation.

HEADER FIELDS:
- "mbl_no": the "DOCUMENT NO (5)" value (e.g. "NAM8654926") — same value as
  the "BL/No." printed near the signature block.
- "port_of_loading": "PORT OF LOADING (12)" value.
- "port_of_discharge": "PORT OF DISCHARGE FROM VESSEL (13)" value.
- "destination_country": the country of the port of discharge city (e.g.
  "ANTWERP, BELGIUM" -> "Belgium").

⚠️ CRITICAL — do not confuse the first summary entry with a real container.
The very first "MARKS AND NUMBERS (16)" cell is usually plain "N/M" with a
"DESCRIPTION OF GOODS (18)" free-text block describing the WHOLE shipment
(e.g. "4x40HC CONTAINERS: TOTAL PALLETS : 72 ... TOTAL NET WEIGHT: 99000
KGS TOTAL GROSS WEIGHT: 100981 KGS ...") — that block is shipment-wide, it
is NEVER a container of its own, and its own weight figures are a
cross-check total only, never any one container's own weight. It also
states the container TYPE for the whole shipment (e.g. "4x40HC CONTAINERS"
-> every container is "40HC") — capture that as "container_type" wherever
a per-row type isn't given (see below), it applies to every real container.

REAL CONTAINER ROWS follow that summary block (and continue onto the next
sheet), one row per container, shaped like:
  <CONTAINER ID>
  SN# <SEAL>
  <PACKAGE COUNT>
  BAG
  <GROSS WEIGHT>KGS
  <MEASUREMENT>CBM
  Example: "CMAU6889780" / "SN# 11227458" / "990" / "BAG" / "25246.000KGS"
  / "51.703CBM" -> id "CMAU6889780", seal "11227458", gross_weight
  25246.000 (exactly as printed, do NOT convert or rescale it yourself),
  gross_weight_unit "KG" (the figure read "KGS"). "BAG" here is the
  PACKAGING type (bags), not the container size — leave this row's own
  "type" empty and rely on the shipment-wide container_type from the
  summary block instead, unless this specific row states its own different
  size/type.
@@UNIT_RULE@@

A "TOTAL" row at the very end (summing every real container's own gross
weight and measurement) is a cross-check value only, never a container of
its own.

Read every real container row across every sheet — do not stop after the
first sheet.
@@PACKAGE_TYPE_RULE@@
@@RETURN_SCHEMA@@"""


# Maersk "Non-Negotiable Waybill" — the front page's "PARTICULARS FURNISHED
# BY SHIPPER" block is a shipment-wide summary/description with NO
# container IDs at all (just "N containers said to contain..." and
# shipment totals); the REAL per-container rows are on a CONTINUATION page.
MAERSK_MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this Maersk \
"Non-Negotiable Waybill" PDF — it spans multiple pages, and the REAL \
per-container rows are on a continuation page (labeled "Page : 2" or \
similar), NOT the front page — read every page — and return ONLY a JSON \
object, no markdown, no explanation.

HEADER FIELDS:
- "mbl_no": the "B/L No." value (top right, e.g. "274990612").
- "port_of_loading": "Port of Loading" value.
- "port_of_discharge": "Port of Discharge" value.
- "destination_country": the country of the "Port of Discharge" city (e.g.
  "ANTWERP, BELGIUM" -> "Belgium").

⚠️ CRITICAL — the front page's "PARTICULARS FURNISHED BY SHIPPER" block
(e.g. "2 containers said to contain 1980 BAG ... TOTAL NET WEIGHT: 49500
KGS TOTAL GROSS WEIGHT: 50490 KGS ...") names NO actual container IDs — it
is a shipment-wide summary only, never a container of its own, and its own
weight figures are a cross-check total only, never any one container's own
weight.

REAL CONTAINER ROWS are on a later/continuation page, one row per
container, shaped like:
  <CONTAINER ID> <TYPE, e.g. "40 DRY 8'6"> <BAGS COUNT> BAG <GROSS
  WEIGHT> KGS <MEASUREMENT> CBM
  Shipper Seal : <SEAL>
  Example: "MRKU0510841 40 DRY 8'6 990 BAG 25245.000 KGS 51.7030 CBM" then
  next line "Shipper Seal : 283978" -> id "MRKU0510841", type "40 DRY 8'6",
  seal "283978", gross_weight 25245.000 (exactly as printed, do NOT convert
  or rescale it yourself), gross_weight_unit "KG" (the figure read "KGS").
@@UNIT_RULE@@

Read every such row on the continuation page — a shipment can have more
than one container, each its own two-line entry; do not stop after the
first one.
@@PACKAGE_TYPE_RULE@@
@@RETURN_SCHEMA@@"""


# HMM "Bill of Lading" — a compact single-line-per-container format with NO
# per-container weight breakdown at all; the ONE "Gross Weight" figure
# printed opposite the goods description applies to the WHOLE shipment (in
# practice this carrier's Emvia shipments have been single-container, in
# which case that one figure simply IS that one container's weight — if a
# future shipment has more than one container with no further breakdown,
# split the total evenly across them, since no other figure is given).
HMM_MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this HMM \
Bill of Lading PDF and return ONLY a JSON object, no markdown, no \
explanation.

HEADER FIELDS:
- "mbl_no": the "B/L No." value (may show a carrier prefix like "HDMU"
  immediately before the booking number, e.g. "HDMU MAAE69596301" — return
  it exactly as printed, prefix included).
- "port_of_loading": "Port of Loading" value.
- "port_of_discharge": "Port of Discharge" value.
- "destination_country": the country of the "Port of Discharge" /
  "Place of Delivery" city (e.g. "ANTWERP, BELGIUM" -> "Belgium").

CONTAINER ROW(S) — in the "Container No./Seal No." column, one line per
container shaped like:
  <CONTAINER ID> / <SEAL NUMBER>
  <TYPE CODE>  CY / CY
  Example: "GCXU5024097 / 1557133" then "DC 4H CY / CY" -> id
  "GCXU5024097", seal "1557133" (some scans print this pair with the seal
  digits run together with the container id on one line and a slash
  between them — always the last several digits after the final "/" are
  the seal, the container id is exactly the first 4 letters + 7 digits).

⚠️ CONTAINER TYPE — the row's own "<TYPE CODE>" (e.g. "DC 4H") is a terse
ISO type-size code, NOT the container size on its own, and must NOT be
returned as-is. "DC" means Dry Container — the standard/general-purpose
container, NEVER a High Cube one, regardless of any digit+letter code
after it (e.g. "4H" is an internal ISO size/type code, not a "High Cube"
indicator, even though it contains the letter "H"). The container SIZE
comes from elsewhere on the document — the "Total Number of Containers or
Packages (in words)" field (e.g. "1 X 40'H DC CONTAINER" -> size "40")
states it, applying to every container in the shipment. Combine these
into the final "type" value yourself: size + "FT" (e.g. "40FT") whenever
the row's own code contains "DC" — never output "HC"/"High Cube" for a
"DC" row. Only use "HC" if the row's own code, or the goods description,
explicitly says "High Cube" / "HC" / "9'6" instead of "DC".

- "gross_weight": the single "Gross Weight" figure printed in that column
  for the shipment (e.g. "26,550.000"), EXACTLY as printed — do not
  convert or rescale it yourself. If more than one container is listed and
  no other weight breakdown is given anywhere on the document, divide this
  total evenly across every container listed (equal shares) and use that
  as each container's own "gross_weight" — otherwise (the common case,
  one container) this figure is simply that one container's own weight.
- "gross_weight_unit": "MT" if the figure's unit is MTS/MT/TONS/TON, "KG"
  if it's KGS/KG — read whichever is ACTUALLY printed for THIS document.
@@PACKAGE_TYPE_RULE@@
@@RETURN_SCHEMA@@"""


# Borchard Lines "Bill of Lading" — the "Marks and Nos; Container No:"
# column lists every container with its own type + seal, but net/gross
# weight is stated only ONCE, as a uniform per-container figure inside the
# shared goods-description text (the containers are declared as an equal,
# uniformly-loaded group) — that same figure applies to EVERY container,
# not just one.
BORCHARD_LINES_MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this \
Borchard Lines Bill of Lading PDF and return ONLY a JSON object, no \
markdown, no explanation.

HEADER FIELDS:
- "mbl_no": the "B/L No." value (top right, e.g. "BORUBSB470AXAN01").
- "port_of_loading": "Port of loading" value.
- "port_of_discharge": "Port of discharge" value.
- "destination_country": the country of the "Port of discharge" city (e.g.
  "ANTWERP" -> "Belgium" if no country is printed alongside it, infer from
  the city).

CONTAINER LIST — in the "Marks and Nos; Container No:" column, one block
per container shaped like:
  <CONTAINER ID>  <TYPE, e.g. "40 HC">
  Seal no  <SEAL NUMBER>
  Example: "BORU7010780   40 HC" then "Seal no   00500126" -> id
  "BORU7010780", type "40 HC", seal "00500126". Read every such block —
  this carrier typically lists SEVERAL containers this way (e.g. six), one
  block each; do not stop after the first one.

⚠️ CRITICAL — WEIGHT. The goods-description text (to the right of the
container list) states net/gross weight only ONCE, but as a PER-CONTAINER
figure, not a shipment-wide one — look for a phrase like "Total Gross
Weight: 27.360 MT" appearing BEFORE a LARGER shipment-wide "Total Gross
Weight: 164.160 MT" figure later in the same text (the two numbers are
related by simple division: smaller × number of containers = larger, e.g.
27.360 × 6 = 164.160). The SMALLER of the two figures is what applies to
EVERY container listed above — use it, EXACTLY as printed, as
"gross_weight" for every single container (all containers share this same
value, since the document declares them as one uniformly-loaded group,
e.g. "6 X40(H.C) CNTR SAID TO CONTAIN ..."). The LARGER figure (matching
the "G.W." value in the header box near the top) is the shipment-wide
grand total — a cross-check value only, never any one container's own
weight; do not use it as a per-container figure.

@@UNIT_RULE@@
@@PACKAGE_TYPE_RULE@@
@@RETURN_SCHEMA@@"""

# Fallback for any carrier not yet onboarded (each will get its own tuned
# prompt as new samples come in) — same field shape as every
# carrier-specific prompt so build_rows()/extract_mbl() never need to know
# which prompt actually ran.
GENERIC_MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this Master \
Bill of Lading / Sea Waybill PDF (it may span multiple pages/sheets, and \
container details may be on an attached rider/continuation page — read all \
of them) and return ONLY a JSON object, no markdown, no explanation.

- "mbl_no": the Bill of Lading / Waybill number.
- "port_of_loading": Port of Loading.
- "port_of_discharge": Port of Discharge.
- "destination_country": the country of the destination port/place of
  delivery city — infer the country from the city if only a city is printed.
- "containers": array of every container, each with:
  - "id": container number, exactly 4 letters + 7 digits, no spaces.
  - "seal": seal number.
  - "type": container type/size as printed (e.g. "40' High Cube", "20FT").
  - "gross_weight": this container's gross weight, EXACTLY as printed — do
    NOT convert or rescale it yourself.
  - "gross_weight_unit": "MT" if the figure's unit is MTS/MT/TONS/TON, "KG"
    if it's KGS/KG — read whichever is ACTUALLY printed for THIS document.
@@UNIT_RULE@@
@@PACKAGE_TYPE_RULE@@
@@RETURN_SCHEMA@@"""


for _name in (
    "HAPAG_LLOYD_MBL_PROMPT", "MSC_MBL_PROMPT", "OOCL_MBL_PROMPT", "CMA_CGM_MBL_PROMPT",
    "MAERSK_MBL_PROMPT", "HMM_MBL_PROMPT", "BORCHARD_LINES_MBL_PROMPT", "GENERIC_MBL_PROMPT",
):
    globals()[_name] = (
        globals()[_name]
        .replace("@@UNIT_RULE@@", UNIT_RULE)
        .replace("@@PACKAGE_TYPE_RULE@@", PACKAGE_TYPE_RULE)
        .replace("@@RETURN_SCHEMA@@", RETURN_SCHEMA)
    )


CARRIER_MBL_PROMPTS = {
    "HAPAG-LLOYD":     HAPAG_LLOYD_MBL_PROMPT,
    "MSC":             MSC_MBL_PROMPT,
    "OOCL":            OOCL_MBL_PROMPT,
    "CMA CGM":         CMA_CGM_MBL_PROMPT,
    "MAERSK":          MAERSK_MBL_PROMPT,
    "HMM":             HMM_MBL_PROMPT,
    "BORCHARD LINES":  BORCHARD_LINES_MBL_PROMPT,
}

# ═══════════════════════════════════════════════════════════════════════════|
# PACKING LIST PROMPT — WAREHOUSE 1147 — VE Staal B.V. "Packing List         |
# Enclosure" (bundle table)                                                  |
# ═══════════════════════════════════════════════════════════════════════════|

PKG_LIST_PROMPT = """You are a shipping-document data extractor. Extract all data from this VE \
Staal B.V. Packing List PDF — it is a cover page followed by one or more \
"Packing List Enclosure" pages, read every page — and return ONLY a JSON \
object, no markdown, no explanation.
 
COVER PAGE:
- "destination_country": the "COUNTRY OF FINAL DESTINATION" value (e.g.
  "BELGIUM").
 
LAST ENCLOSURE PAGE — a "GRAND TOTAL :" row (the very last totals row in the
whole document, after every container/order/grade/size has been summed):
- "grand_total_pieces": that row's own "Pcs of Bar" figure, transcribed
  exactly as printed on THIS document (a whole number).
- "grand_total_net_mt": that row's own "NetWt (TO)" figure, transcribed
  exactly as printed on THIS document (a decimal number of metric tons).
These two are a printed ground truth used only to verify your own row
extraction below — read them directly off the page, do not compute them
yourself, and do not reuse a number from any other example in these
instructions.
 
ENCLOSURE PAGES — a table with columns: Grade/Cust. Material | Size | Shape
| Length | Tolerance | Heat No | Bundle | Pcs of Bar | Gross Wt (TO) | Tare
Wt (TO) | NetWt (TO), grouped under a "CONTAINER NO : <id>" header (repeated
on every page it spans) and further split into "ORDER NO <n>" sub-sections.
⚠️ An "ORDER NO" section can CONTINUE onto a following page WITHOUT
repeating its "ORDER NO <n>" label — a page that starts straight into more
bundle rows (no new "ORDER NO" line visible) still belongs to the last
"ORDER NO" seen, do not treat a missing label as the start of some
unlabeled group. Ignore the order-no grouping itself for the output (it's
not one of the fields below), just make sure you read every real bundle row
underneath it, from whichever order section it falls under, including rows
that continue after a page break with no new header.
 
⚠️ CRITICAL — FIRST ROW AFTER A SECTION HEADER. On this document layout,
the very first bundle row printed immediately after a category header line
(e.g. "COLD DRAWN GROUND - 2G") and/or an "ORDER NO" line is a REAL data
row — but the model commonly SKIPS it because its Grade/Cust. Material
column text gets visually merged with or truncated by the header above it.
Here is EXACTLY what this looks like:
 
  COLD DRAWN GROUND - 2G          ORDER NO        2602036/KS
    1/1.4307   12.000 MM   ROUND   6.00 - 6.10   h9-K240   GFD42   14001141168
                                                                     // 4204970
                                                            91   0.492   0.002   0.490
  1.4301/1.4307   12.000 MM   ROUND   6.00 - 6.10   h9-K240   GFD42   14001141167
                                                                     // 4204958
                                                            104   0.562   0.002   0.560
 
That is TWO separate bundle rows, not one:
  ROW 1 (the one that gets skipped): Grade "1/1.4307" (truncated — the
  leading "1.430" is absorbed into the category header's layout), bundle
  14001141168, batch_no "4204970", pieces 91, gross_weight_mt 0.492,
  net_weight_mt 0.490.
  ROW 2: Grade "1.4301/1.4307" (full, normal-looking), bundle 14001141167,
  batch_no "4204958", pieces 104, gross_weight_mt 0.562, net_weight_mt
  0.560.
 
THE MOST COMMON WRONG OUTPUT for the above is:
  - ROW 1 is silently dropped, and its batch_no "4204970" is stolen and
    assigned to ROW 2's weights (pieces 104, net 0.560) — WRONG.
  - ROW 2's own batch_no "4204958" disappears entirely — WRONG.
  - The total pieces end up short by exactly 91 (ROW 1's pieces) — WRONG.
This happens because the model sees the truncated "1/1.4307" Grade and
treats it as part of the header rather than as a data row. It is ALWAYS
a data row. Every category/order header is ALWAYS followed by at least
one data row with a Bundle number, a "// <batch>" line, and its own
Pcs/weights — if you cannot find that first row, look again: its Grade
column is simply truncated, not absent.
 
Each REAL bundle row has a Bundle cell spanning TWO lines: a long bundle
number, then a SECOND line starting with "//" followed by a shorter number —
e.g.:
  "13000549195"
  "// 8664556"
-> that row's "batch_no" is "8664556" (whatever digits are actually printed
after "//" on the SECOND line — never the long first-line bundle number
itself).
 
⚠️ CRITICAL — PAGE-BREAK SPLIT ROWS. This two-line Bundle cell can be torn
apart by a page break: a page can END right after a row's bundle-number
line AND its Pcs/Gross Wt/Tare Wt/NetWt figures (a complete-LOOKING row),
with that SAME row's "// <batch>" second line pushed onto the very TOP of
the NEXT page, printed there alone before any new row's Grade/Cust.
Material starts.
 
REAL WORKED EXAMPLE — exactly how this layout appears at an actual page
boundary on this type of document:
 
  ══════ end of page 2 ══════
  1.4301/1.4307   40 X 40 X 4 MM   ANGLE   6.00 - 6.20   EN 10056   GFG96
  13000549194
  // 8664547
                                                    36    0.516   0.002   0.514
  1.4301/1.4307   40 X 40 X 4 MM   ANGLE   6.00 - 6.20   EN 10056   GFH12
  13000549193
                                                    37    0.534   0.002   0.532
  ══════ start of page 3 ══════
  // 8664550
  1.4301/1.4307   40 X 40 X 4 MM   ANGLE   6.00 - 6.20   EN 10056   GFH23
  13000549186
  // 8664588
                                                    39    0.564   0.002   0.562
  1.4301/1.4307   40 X 40 X 4 MM   ANGLE   6.00 - 6.20   EN 10056   GFH23
  13000549185
  // 8664591
                                                    39    0.564   0.002   0.562
 
STEP-BY-STEP CORRECT READING of the above:
 
  (a) LAST COMPLETE ROW OF PAGE 2 — bundle 13000549194, batch_no "8664547"
      (its "//" line IS on the same page, so it's fully complete), pieces 36,
      gross_weight_mt 0.516, net_weight_mt 0.514.
 
  (b) LAST ROW OF PAGE 2 — SPLIT ACROSS THE PAGE BREAK — bundle
      13000549193 has its Pcs/weights on page 2 (37 / 0.534 / 0.532) but
      its "//" batch line is MISSING from page 2. That batch line is the
      VERY FIRST line of page 3: "// 8664550". So:
        batch_no = "8664550"
        pieces = 37
        gross_weight_mt = 0.534
        net_weight_mt = 0.532
      The "8664550" is NOT the batch of the row printed below it on page 3
      (bundle 13000549186) — it belongs UPWARD to bundle 13000549193 whose
      weights (37, 0.534, 0.532) are on the previous page.
 
  (c) FIRST FULL ROW OF PAGE 3 — bundle 13000549186, batch_no "8664588"
      (its own "//" line is right below it, on the same page), pieces 39,
      gross_weight_mt 0.564, net_weight_mt 0.562.
 
  (d) SECOND ROW OF PAGE 3 — bundle 13000549185, batch_no "8664591",
      pieces 39, gross_weight_mt 0.564, net_weight_mt 0.562.
 
The CORRECT output for these four rows:
  {"product": "EN 10056", "batch_no": "8664547", "pieces": 36,
   "net_weight_mt": 0.514, "gross_weight_mt": 0.516},
  {"product": "EN 10056", "batch_no": "8664550", "pieces": 37,
   "net_weight_mt": 0.532, "gross_weight_mt": 0.534},
  {"product": "EN 10056", "batch_no": "8664588", "pieces": 39,
   "net_weight_mt": 0.562, "gross_weight_mt": 0.564},
  {"product": "EN 10056", "batch_no": "8664591", "pieces": 39,
   "net_weight_mt": 0.562, "gross_weight_mt": 0.564}
 
⚠️ THE MOST COMMON WRONG OUTPUT looks like this (DO NOT produce this):
  {"batch_no": "8664547", "pieces": 36, ...},
  {"batch_no": "8664556", "pieces": 37, ...},   ← WRONG: "8664556" is from
       the PREVIOUS row, duplicated because the model didn't see the
       orphaned "// 8664550" at the top of page 3. The correct batch is
       "8664550".
  {"batch_no": "8664550", "pieces": 39, ...},   ← WRONG: "8664550" paired
       with 39/0.564/0.562 which actually belong to batch "8664588". The
       model attached the orphaned batch to the NEXT row instead of the
       PREVIOUS row.
  {"batch_no": "8664588", "pieces": 39, ...}
This cascading error happens when the orphaned "//" line at the top of a
new page is attached DOWNWARD to the next row instead of UPWARD to the
last row of the previous page. It also causes batch "8664556" to be
duplicated (appearing on two rows) — which is ALWAYS a red flag.
 
⚠️ PAGE-BOUNDARY PROCEDURE — every time you reach a page boundary inside
the bundle table, STOP and follow these steps BEFORE continuing:
  1. Look at the VERY FIRST non-header line on the new page. Is it a
     standalone "// <digits>" line with NO bundle-number line directly
     above it on THIS page?
  2. If YES → that "//" line's batch_no belongs to the LAST bundle row
     you read on the PREVIOUS page. Go back and assign it NOW.
  3. If NO → the previous page's last row already had its "//" line, and
     this page starts fresh with a new row. Proceed normally.
Do this check at EVERY page transition. Never skip it.

⚠️ CRITICAL — NEVER EMIT A SPLIT ROW TWICE. An orphaned "// <batch>" line
at the top of a new page (step 2 above) belongs to a row you ALREADY
emitted — it fills in that row's missing batch_no, it does NOT start, and
must NEVER produce, a second row of its own. Concretely: if the previous
page's last row already gave you its bundle number, pieces, gross/tare/net
weight (everything except the batch), and you then read an orphaned "//"
line at the top of the next page, go back and set THAT SAME row's batch_no
— do not also emit a brand-new row that repeats that row's pieces/weights
a second time. One bundle row's Pcs/Gross Wt/Tare Wt/NetWt figures must
appear in your output EXACTLY ONCE, never twice under two different batch
numbers. This exact mistake — duplicating a split row instead of just
patching its batch_no — is the single most common way this document's row
count and Grand Total end up wrong; watch for it specifically.
 
⚠️ CRITICAL — every batch number on this document is DIFFERENT, even
between consecutive rows. If you find yourself about to output the SAME
batch_no on two or more rows, that is a red flag you did not actually read
one of them — go back and re-read that row's own "//" line individually.
Never copy a neighboring row's batch number. Two mistakes are equally wrong:
(a) pairing the orphaned batch with the following row's numbers instead of
the row it belongs to, and (b) duplicating the previous row's batch number
because the orphaned line was missed — both have been seen to happen.

⚠️ SPECIAL CASE — ORPHANED "//" FOLLOWED BY A TOTAL ROW, NOT A DATA ROW.
The orphaned "//" line at the top of a new page can land right BEFORE a
SIZE TOTAL / SHAPE TOTAL / GRADE TOTAL row — NOT before a new data row.
This happens when the split bundle was the LAST (or only) row in its size
group, so the SIZE TOTAL printed directly after the orphaned "//" has the
EXACT SAME Pcs and weight figures as the split bundle itself. Here is
exactly what this looks like:

  ══════ end of page N ══════
  1.4301/1.4307   28.000 MM   ROUND   3.00 - 3.10   h9   GFG44   14001148100
                                                    37    0.551   0.001   0.550
  ══════ start of page N+1 ══════
  // 4840849
                    SIZE TOTAL
                                                    37    0.551   0.001   0.550
                    SHAPE TOTAL
                                                   676    4.769   0.011   4.758
                    GRADE TOTAL
                                                   676    4.769   0.011   4.758

CORRECT READING — ONE row only:
  bundle 14001148100, batch_no "4840849", pieces 37, gross_weight_mt 0.551,
  net_weight_mt 0.550. The "// 4840849" belongs UPWARD to bundle
  14001148100 on the previous page. The SIZE TOTAL line below it is just a
  subtotal — it has NO Bundle number, NO Heat No, NO Tolerance of its own,
  it is NOT a data row. Do NOT read the SIZE TOTAL's numbers as a second
  row's Pcs/weights.

THE MOST COMMON WRONG OUTPUT for the above (DO NOT produce this):
  {"batch_no": "<fabricated>", "pieces": 37, "net_weight_mt": 0.550, ...},
  {"batch_no": "4840849",      "pieces": 37, "net_weight_mt": 0.550, ...}
  ← WRONG: TWO rows with identical pieces and weights. The model could not
  find the "//" line for bundle 14001148100 on the same page, so it
  fabricated a batch number for one row (sometimes by misreading part of
  the bundle number "14001148100" itself as a batch) and then created a
  SECOND row when it found the orphaned "// 4840849" on the next page
  paired with the SIZE TOTAL's identical numbers. This always doubles the
  real bundle's contribution — the row count, pieces total, and net weight
  total all end up higher than the document's own GRAND TOTAL.

⚠️ This document is a photocopy/scan — digits can look similar to each other
(e.g. 2 vs 6, 8 vs 6, 3 vs 8), which is a common cause of misreading one
digit in an otherwise-correct number. Read each digit of the batch number
individually and carefully, directly off the row you are currently on,
rather than pattern-matching against a nearby row's number or reusing a
value from memory. Double check every batch number digit-by-digit before
finalizing it.
 
For every such row, extract:
- "product": this row's "Tolerance" column value (e.g. "h9-K240", "h9") —
  this is the per-row product identifier on this document, not the page-1
  "Description Of Goods" text.
- "batch_no": as described above.
- "pieces": the "Pcs of Bar" column value (integer).
- "net_weight_mt": the "NetWt (TO)" column value for this row (already in
  metric tons).
- "gross_weight_mt": the "Gross Wt (TO)" column value for this row (already
  in metric tons).
 
⚠️ Do NOT extract a "SIZE TOTAL" / "SHAPE TOTAL" / "GRADE TOTAL" / "ORDER
TOTAL" / "GRAND TOTAL" row as a bundle — those ARE, however, real printed
subtotals you must use for a self-check (see below); just don't emit them
as bundle rows themselves (no Tolerance/Heat No/Bundle number of their own).
 
⚠️ MANDATORY SELF-CHECK — you MUST perform ALL of these checks BEFORE
producing your final JSON. This document lists many near-identical-looking
rows (same Tolerance, similar weights) across several pages, which makes it
easy to skip rows without noticing. The checks below are NOT optional — if
any check fails, you MUST go back and fix the extraction before returning.
 
CHECK 1 — FIRST ROW: for each category header (e.g. "COLD DRAWN GROUND -
2G") and/or "ORDER NO" line, verify that you extracted the bundle row
printed IMMEDIATELY after it. If the first row you extracted for that
section has a different bundle number than the one printed right below the
header, you skipped the first row — go back and read it. Its Grade column
will look truncated (e.g. "1/1.4307" instead of "1.4301/1.4307").
 
CHECK 2 — SIZE TOTALS: for each "SIZE TOTAL" line, sum the "Pcs of Bar"
and "NetWt (TO)" of the bundle rows you extracted for that same
Size/Tolerance group directly above it. If they don't match the printed
SIZE TOTAL, you missed or misread a row in that group — go back and fix it.
 
CHECK 3 — GRAND TOTAL (MANDATORY, DO NOT SKIP): read the document's own
"GRAND TOTAL :" row (the very last totals line). Sum ALL your extracted
bundle rows' "pieces" — the total MUST equal grand_total_pieces exactly.
Sum ALL your extracted bundle rows' "net_weight_mt" — the total MUST equal
grand_total_net_mt exactly. If either sum does not match:
  (a) You almost certainly skipped a row. The most common skipped row is the
      FIRST row after a category/order header (see CHECK 1 above).
  (b) Go back to CHECK 1, verify every section's first row, then re-check.
  (c) Do NOT return your JSON until the sums match.
 
Read every real bundle row on every page, including across an "ORDER NO"
section boundary and a page boundary — do not stop early.
 
Group the rows under their container:
"containers": [
  {"container_no": "string", "bundles": [
    {"product": "string", "batch_no": "string", "pieces": 0,
     "net_weight_mt": 0, "gross_weight_mt": 0}
  ]}
]
 
Return:
{
  "destination_country": "string",
  "grand_total_pieces": 0,
  "grand_total_net_mt": 0,
  "containers": [
    {"container_no": "string", "bundles": [
      {"product": "string", "batch_no": "string", "pieces": 0,
       "net_weight_mt": 0, "gross_weight_mt": 0}
    ]}
  ]
}"""


# ═══════════════════════════════════════════════════════════════════════════
# PACKING LIST PROMPT — WAREHOUSE NNRC 660 (resin in bags, on pallets)
# ═══════════════════════════════════════════════════════════════════════════
# This warehouse has TWO known Packing List layouts from different shippers
# — the prompt self-detects which one it's looking at first, the same way
# Vinmar's/Sabic's own PKG_LIST_PROMPT self-detects multiple layouts.
#
# LAYOUT A — Chevron Phillips "Export Packing List": one line-item table
# with columns Inv Line# | Material# | Description | Lot/Batch | Packages |
# Pallets | Net Wt. | Gross Wt. | Wt. Unit, grouped under a "Marks &
# Numbers" cell (CNT#:/Seal:) that names each block's container. States its
# own Packages/Pallets per row directly.
#
# LAYOUT B — Saudi Polymers Company "Packing List": TWO pages. Page 1 is a
# shipment-WIDE summary only (total bags, "N BAGS PER CONTAINER", "N
# PALLETS PER CONTAINER", "N KG NET PER BAG") with NO real container
# details on it at all — critically, its "Vessel's Name" field (e.g. "GSL
# TRIPOLI 631W") is NOT a container marking, "631W" there is a VOYAGE
# NUMBER, never treat it as one. The REAL per-container table is on page 2,
# columns "Container No." | "Seal No." | "Product Type" | "Batch No." |
# "Qty" — one row per batch, and a container can legitimately span TWO OR
# MORE rows (same Container No. repeated) when more than one batch was
# loaded into it. This layout never prints a Packages/Pallets count per
# row — those are computed afterward in code from page 1's stated
# kg-per-bag / bags-per-pallet ratios, not by you.

PKG_LIST_NNRC_PROMPT = """You are a shipping-document data extractor. Extract all data from this \
Packing List PDF and return ONLY a JSON object, no markdown, no explanation.

This document uses ONE of two layouts. Identify which one FIRST, then apply
ONLY that layout's rules below.

══════════════════════════════════════════
LAYOUT A — Chevron Phillips "Export Packing List"
══════════════════════════════════════════
How to detect: a single page (or a few pages) with ONE line-item table,
columns Inv Line# | Material# | Description | Lot/Batch | Packages |
Pallets | Net Wt. | Gross Wt. | Wt. Unit, and a "Marks & Numbers" column
naming each container.

HEADER FIELD:
- "destination_country": the country on the "Ultimate Consignee" address
  block (its last address line, e.g. "BELGIUM"). If that block has no
  country, fall back to the "Buyer / Consignee" address block's country
  instead.

LINE-ITEM TABLE: the "Marks & Numbers" column names the CONTAINER a block
of one or more line-item rows belongs to, shaped like:
  <N> Sea-Container <SIZE>ft
  CNT#: <CONTAINER ID>
  Seal:<SEAL NUMBER>
That container block applies to every line-item row printed until the NEXT
"Marks & Numbers" block starts (a shipment can have more than one
container, each introducing its own block of one or more line items — read
every block on every page, do not stop after the first container).

For every line-item row under a container block, extract:
- "product": the Description cell's PRODUCT NAME ONLY, not the whole cell.
  That cell is shaped like "<BRAND+GRADE>\\n<GENERIC MATERIAL CLASS> in
  <PACKAGING TYPE>" — e.g. "MARLEX EHM 6007\\nPOLYETHYLENE in Bag" — take
  only the first line, the brand+grade ("MARLEX EHM 6007"), and drop the
  second line entirely (it's a generic material class + packaging
  descriptor, not part of the product name, e.g. "POLYETHYLENE in Bag",
  "POLYPROPYLENE in Boxes"). This naming convention is NOT universal across
  every product family, so apply it as a general principle (specific
  brand+grade name vs. generic material-class/packaging text), not a fixed
  pattern to match literally.
- "batch_no": the "Lot / Batch" column value (e.g. "NTD112140").
- "pieces": the leading integer of the "Packages" column (e.g. from
  "990 Bag(s)" extract 990).
- "pallet_count": the leading integer of the "Pallets" column (e.g. from
  "18 Pallet(s)" extract 18) — this document states its own pallet count
  directly, unlike some other layouts; use it as printed, do not compute it.
- "net_weight": the "Net Wt." column value for this row, EXACTLY as
  printed — do not convert or rescale it yourself.
- "gross_weight": the "Gross Wt." column value for this row, EXACTLY as
  printed — do not convert or rescale it yourself.
- "weight_unit": the "Wt. Unit" column value for THIS row (e.g. "TON",
  "KGS") — read whichever is actually printed, the unit conversion happens
  afterward in code, not by you. ⚠️ MANDATORY — every row on this layout
  has one, most commonly "TON" for every row in the whole document; never
  leave this field blank/null/omitted.

Do NOT extract a "Total" row (the summary row at the bottom of a
container's line items) as a line item itself.

══════════════════════════════════════════
LAYOUT B — Saudi Polymers Company "Packing List"
══════════════════════════════════════════
How to detect: "Saudi Polymers Company" letterhead; page 1 has NO
container-level detail, only a shipment-wide summary paragraph (e.g. "8 FCL
x 40' CONTAINER CONTAINING:- ... 7,920 BAGS/990 BAGS PER CONTAINER 18
PALLETS PER CONTAINER 55 BAGS PER PALLET - 25 KG NET PER BAG ..."); page 2
has a table headed "Container No." | "Seal No." | "Product Type" | "Batch
No." | "Qty".

HEADER FIELDS (from page 1):
- "destination_country": the country on the "Ship-To customer" address
  block's last line (e.g. "DIRECT SHIPMENT BELGIUM ANTWERPEN" -> "BELGIUM").
- "kg_per_bag": the number in "<N> KG NET PER BAG" (e.g. from "25 KG NET
  PER BAG" extract 25).
- "bags_per_pallet": the number in "<N> BAGS PER PALLET" (e.g. from "55
  BAGS PER PALLET" extract 55).
These two are shipment-wide constants (same for every row) used afterward
in code to COMPUTE each row's bag/pallet count from its own net weight —
you do not need to compute pieces or pallets yourself on this layout, and
there is no Packages/Pallets column printed per row to read one from.

⚠️ CRITICAL — the "Vessel's Name" field on page 1 (e.g. "GSL TRIPOLI
631W") is NOT a container reference of any kind — "631W" there is a VOYAGE
NUMBER, part of the vessel/voyage identifier, completely unrelated to any
container. NEVER extract it as a container_no, and never attribute any
line-item row to it. The ONLY real container identifiers on this
document are in the page-2 table's own "Container No." column.

PAGE 2 TABLE — one row per batch, columns Container No. | Seal No. |
Product Type | Batch No. | Qty:
  Example: "TRHU4600234   0837466   HHM 5502BN BAG   SPG31215   1.375 MT"
  then a SECOND row: "TRHU4600234   0837466   HHM 5502BN BAG   SPG31221
  23.375 MT" -> this is the SAME container (TRHU4600234) split across TWO
  batches — both rows are real, keep them separate, do NOT merge them or
  treat the repeated container number as an error. Most containers on this
  layout have only ONE row; a container appearing on 2+ consecutive rows
  with a DIFFERENT Batch No. each time is normal and expected, not a
  mistake — read every row exactly as printed, including every repeat of a
  container number when its batch number differs.

For every row, extract:
- "container_no": the "Container No." column value.
- "product": the "Product Type" column value's PRODUCT NAME ONLY (e.g.
  from "HHM 5502BN BAG" extract "HHM 5502BN", dropping the trailing
  generic packaging word "BAG" — same principle as Layout A's product
  extraction, just applied to this layout's own column).
- "batch_no": the "Batch No." column value (e.g. "SPG32222").
- "net_weight": the "Qty" column value for this row, EXACTLY as printed
  (e.g. 24.750) — this is a metric-ton figure, do not convert or rescale
  it yourself.
- "weight_unit": "MT" (this layout's Qty column is always in metric tons).
- "gross_weight": leave 0 — this layout states only ONE weight figure
  (labeled "Net Wt Delivered" on page 1 and "Qty" on page 2), there is no
  separate gross weight anywhere on the document.
- "pieces" / "pallet_count": leave both 0 — not printed per row on this
  layout, computed afterward in code from "kg_per_bag"/"bags_per_pallet"
  instead (see HEADER FIELDS above).

Do NOT extract a subtotal/total row (if any) as a line item itself.

══════════════════════════════════════════
OUTPUT FORMAT (same shape regardless of which layout you detected — leave
"kg_per_bag"/"bags_per_pallet" at 0 if you detected Layout A, which doesn't
have them)
══════════════════════════════════════════
Group the rows under their container:
"containers": [
  {"container_no": "string", "items": [
    {"product": "string", "batch_no": "string", "pieces": 0,
     "pallet_count": 0, "net_weight": 0, "gross_weight": 0,
     "weight_unit": "string"}
  ]}
]

Return:
{
  "destination_country": "string",
  "kg_per_bag": 0,
  "bags_per_pallet": 0,
  "containers": [
    {"container_no": "string", "items": [
      {"product": "string", "batch_no": "string", "pieces": 0,
       "pallet_count": 0, "net_weight": 0, "gross_weight": 0,
       "weight_unit": "string"}
    ]}
  ]
}"""


# ═══════════════════════════════════════════════════════════════════════════
# DETERMINISTIC BATCH-NUMBER CROSS-CHECK (pdfplumber, no LLM involved)
# ═══════════════════════════════════════════════════════════════════════════

_BATCH_NO_RE = re.compile(r"//\s*(\d{5,9})")

# The Bundle cell's OWN first-line number (e.g. "14001148172", "13000549194")
# — always 10-13 digits on every sample seen, cleanly distinct from the
# 5-9-digit batch number that follows "//" on the line below it. Unlike the
# batch line, this number is NEVER split across a page break (only the "//"
# line gets orphaned onto the next page — see PKG_LIST_PROMPT's worked
# example) — so its count is a page-break-immune ground truth for the TRUE
# number of bundle rows in the document, usable even when the batch-number
# count above doesn't line up.
_BUNDLE_NO_RE = re.compile(r"\b(\d{10,13})\b")


def _pdf_batch_numbers(pdf_path: str) -> list[str]:
    """Every '// <digits>' batch-number token in the PDF's own text layer,
    in true document reading order (page by page, top to bottom) — read via
    plain regex on pdfplumber's extracted text, not a vision model, so it
    can't misread a digit or mis-pair a batch with the wrong row's weights
    the way Gemini did across a page break. Used by extract_packing_list()
    to override the model's own batch_no assignment whenever the count
    matches exactly (see there for why an exact-count match is required
    before trusting a positional zip)."""
    with pdfplumber.open(pdf_path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return _BATCH_NO_RE.findall(full_text)


def _pdf_bundle_numbers(pdf_path: str) -> list[str]:
    """Every bundle cell's own long first-line number, in document order —
    see _BUNDLE_NO_RE above for why this is a more reliable row-count ground
    truth than the batch-token count (immune to the page-break split that
    only ever affects the "//" line, never this one)."""
    with pdfplumber.open(pdf_path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return _BUNDLE_NO_RE.findall(full_text)


def _normalize_package_type(raw: str) -> str:
    """Collapses whatever the model returned for the MBL's package-count
    phrase (see PACKAGE_TYPE_RULE) to exactly "BUNDLE", "BAG", or "" —
    used by build_rows() to decide which of the Bundles/Bags output
    columns a row's pieces figure belongs in."""
    upper = s(raw).strip().upper()
    if "BUNDLE" in upper:
        return "BUNDLE"
    if "BAG" in upper:
        return "BAG"
    return ""


# ═══════════════════════════════════════════════════════════════════════════
# EXTRACTION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def _normalize_container_type_emvia(raw: str) -> str:
    """Wraps the shared doc_common.normalize_container_type() with one
    Emvia-specific safety net: "DC" (Dry Container) means the standard,
    non-High-Cube container — but the shared helper's default for an
    unrecognized 40'/20' string is "40HC", and a bare code like "DC 4H"
    (HMM's per-row type designator, with NO literal "40"/"20" digit
    substring at all) doesn't match anything in the shared helper's
    keyword lists, so it fell straight through as the literal raw string
    ("DC 4H") instead of ever reaching that default. The MBL prompt is now
    taught to resolve "DC" rows to "<size>FT" itself (see HMM_MBL_PROMPT),
    but this stays as a defensive second layer: if a "DC" string somehow
    still reaches here without a recognized "FT"/"HC" keyword, force it
    toward FT rather than letting the shared helper's HC-biased default
    win for a container that is explicitly NOT high-cube."""
    upper = (raw or "").upper()
    if "DC" in upper and "HC" not in upper and "HIGH CUBE" not in upper and "FT" not in upper:
        raw = f"{raw} FT"
    return normalize_container_type(raw)


def extract_mbl(pdf_path: str) -> dict:
    # Two-call pipeline (see module docstring): identify the carrier, then
    # dispatch to that carrier's own tuned prompt — falls back to
    # GENERIC_MBL_PROMPT for any carrier not yet onboarded.
    carrier = identify_carrier(pdf_path)
    prompt = CARRIER_MBL_PROMPTS.get(carrier, GENERIC_MBL_PROMPT)

    data = call_gemini(prompt, pdf_path=pdf_path, max_output_tokens=8192)
    dump_json(pdf_path, "mbl_raw.json", data)

    containers = []
    for c in data.get("containers", []):
        cid, seal = fix_container_id(c.get("id", ""), c.get("seal", ""))
        unit = s(c.get("gross_weight_unit")).strip().upper() or "MT"
        containers.append({
            "id":              cid,
            "seal":            s(seal or c.get("seal")).strip(),
            "type":            s(c.get("type")).strip(),
            # Converted here in code from whatever unit the model reported
            # off the document's own "(MTS)"/"(KGS)" label — never trust an
            # LLM to do its own unit arithmetic inside the prompt, that's
            # exactly how a "(KGS)"-labeled document got wrongly ×1000'd.
            "gross_weight_kg": to_kg(c.get("gross_weight"), unit),
            # "BUNDLE" or "BAG" — read off the MBL's own package-count
            # phrase (see PACKAGE_TYPE_RULE); build_rows() routes the
            # Packing List's pieces figure into the matching Bundles/Bags
            "package_type":    _normalize_package_type(c.get("package_type")),
        })
    data["containers"] = containers
    data["carrier"] = carrier

    dump_json(pdf_path, "mbl.json", data)
    print(f"  [MBL] Carrier identified as {carrier} — "
          f"{'carrier-specific' if carrier in CARRIER_MBL_PROMPTS else 'generic fallback'} prompt used")
    return data


def extract_packing_list(pdf_path: str) -> dict:
    data = call_gemini(PKG_LIST_PROMPT, pdf_path=pdf_path, max_output_tokens=32768)
    dump_json(pdf_path, "pkg_list_raw.json", data)

    containers = []
    for c in data.get("containers", []):
        cid, _ = fix_container_id(c.get("container_no", ""))
        bundles = []
        for b in c.get("bundles", []):
            net_weight_mt = num(b.get("net_weight_mt"), 0)
            gross_weight_mt = num(b.get("gross_weight_mt"), 0)
            bundles.append({
                "product":          s(b.get("product")).strip(),
                "batch_no":         s(b.get("batch_no")).strip(),
                "pieces":           num(b.get("pieces"), 0),
                # Kept in MT for validate()'s Grand-Total self-check (the
                # document states its own totals in MT); *_weight_kg below
                # is what build_rows() actually consumes, so PDF- and
                # Excel-sourced bundles share one uniform row-building path.
                "net_weight_mt":    net_weight_mt,
                "gross_weight_mt":  gross_weight_mt,
                "net_weight_kg":    to_kg(net_weight_mt, "MT"),
                "gross_weight_kg":  to_kg(gross_weight_mt, "MT"),
            })
        containers.append({"container_no": cid, "bundles": bundles})
    data["containers"] = containers
    data["grand_total_pieces"] = num(data.get("grand_total_pieces"), 0)
    data["grand_total_net_mt"] = num(data.get("grand_total_net_mt"), 0)

    # Deterministic batch-number cross-check/override — see module
    # docstring and _pdf_batch_numbers() for why this is trusted over
    # Gemini's own batch_no whenever the counts line up exactly: only an
    # EXACT count match is safe to apply positionally (if Gemini
    # under/over-counted rows, a positional zip would silently misalign
    # everything worse than leaving its own — already validate()-checked —
    # batch_no values in place).
    all_bundles = [b for c in containers for b in c["bundles"]]
    pdf_batches = _pdf_batch_numbers(pdf_path)
    pdf_bundle_nos = _pdf_bundle_numbers(pdf_path)

    # Row-count ground truth (see _BUNDLE_NO_RE) — independent of, and more
    # reliable than, the batch-token count below, since it can't be thrown
    # off by a page-break-orphaned "//" line. A mismatch here means Gemini
    # duplicated or skipped whole rows (not just mis-paired a batch number),
    # which validate() reports on explicitly.
    data["pdf_bundle_row_count"] = len(pdf_bundle_nos)
    data["gemini_bundle_row_count"] = len(all_bundles)

    if pdf_batches and len(pdf_batches) == len(all_bundles):
        for bundle, batch_no in zip(all_bundles, pdf_batches):
            bundle["batch_no"] = batch_no
        data["batch_no_source"] = "pdf_text_layer"
    else:
        # Falling back to Gemini's own batch_no readings — at minimum,
        # strip out the one specific failure mode seen in practice: a
        # batch_no that is actually the ROW'S OWN bundle number (the long
        # first-line value, e.g. "14001148100") rather than its "//" value.
        # That can never be a real batch on this document — it always means
        # Gemini mixed the two up, not a genuine reading — so blank it out
        # rather than show a value that's confirmed wrong.
        bundle_no_set = set(pdf_bundle_nos)
        suspect_count = 0
        for b in all_bundles:
            if b["batch_no"] and b["batch_no"] in bundle_no_set:
                b["batch_no"] = ""
                suspect_count += 1
        data["batch_no_source"] = "gemini"
        data["batch_no_count_mismatch"] = {
            "pdf_text_layer_count": len(pdf_batches),
            "gemini_row_count": len(all_bundles),
        }
        if suspect_count:
            data["suspect_batch_number_count"] = suspect_count

    dump_json(pdf_path, "pkg_list.json", data)
    return data


_KNOWN_MT_UNITS = ("MT", "MTS", "TON", "TONS", "TONNE", "TONNES")
_KNOWN_KG_UNITS = ("KG", "KGS")


def _normalize_weight_unit(unit: str) -> tuple[str, bool]:
    """doc_common.to_kg() only recognizes the literal string 'MT' as
    metric-tons (anything else passes through unconverted as KG) — NNRC's
    "Wt. Unit" column prints "TON" rather than "MT"/"MTS", so it has to be
    mapped explicitly or a genuine ton figure would silently be treated as
    already-KG and never multiplied by 1000. Returns (normalized_unit,
    was_recognized) — every NNRC sample seen so far uses TON, so a
    missing/unrecognized unit defaults to "MT" (the far more likely case)
    rather than "KG" — silently under-reporting a real ton figure by 1000x
    is a worse failure than the rarer case of a genuinely-KG value getting
    misconverted, which produces an obviously wrong (huge) number that
    validate() 's cross-checks can catch. `was_recognized=False` is
    surfaced in validate() either way so a missing unit is never silent."""
    upper = s(unit).strip().upper()
    if upper in _KNOWN_MT_UNITS:
        return "MT", True
    if upper in _KNOWN_KG_UNITS:
        return "KG", True
    return "MT", False


def extract_packing_list_nnrc(pdf_path: str) -> dict:
    data = call_gemini(PKG_LIST_NNRC_PROMPT, pdf_path=pdf_path, max_output_tokens=16384)
    dump_json(pdf_path, "pkg_list_nnrc_raw.json", data)

    # Layout B (Saudi Polymers) only — shipment-wide constants used to
    # deterministically compute each row's own pieces/pallet_count in code
    # below, rather than asking the model to do that division itself (it
    # never has a printed Packages/Pallets figure to read on this layout).
    kg_per_bag = num(data.get("kg_per_bag"), 0)
    bags_per_pallet = num(data.get("bags_per_pallet"), 0)

    containers = []
    unrecognized_unit_count = 0
    for c in data.get("containers", []):
        cid, _ = fix_container_id(c.get("container_no", ""))
        bundles = []
        for item in c.get("items", []):
            unit, recognized = _normalize_weight_unit(item.get("weight_unit"))
            if not recognized:
                unrecognized_unit_count += 1

            net_weight_kg = to_kg(item.get("net_weight"), unit)
            gross_weight_kg = to_kg(item.get("gross_weight"), unit)
            # Layout B states only one weight figure at all (see prompt) —
            # mirror net into gross when the model correctly left gross at 0,
            # same "only one figure exists" convention as the Excel path.
            if not gross_weight_kg:
                gross_weight_kg = net_weight_kg

            pieces = num(item.get("pieces"), 0)
            pallet_count = num(item.get("pallet_count"), 0)
            # Layout B fallback: derive from this row's own net weight using
            # the shipment-wide kg-per-bag / bags-per-pallet ratios — exact
            # for this layout since NetWt = pieces × kg_per_bag by
            # construction (bags are a fixed-weight unit here, unlike
            # warehouse 1147's steel bars).
            if not pieces and kg_per_bag:
                pieces = round(net_weight_kg / kg_per_bag)
            if not pallet_count and bags_per_pallet and pieces:
                pallet_count = round(pieces / bags_per_pallet)

            bundles.append({
                "product":          s(item.get("product")).strip(),
                "batch_no":         s(item.get("batch_no")).strip(),
                "pieces":           pieces,
                "pallet_count":     pallet_count,
                "net_weight_kg":    net_weight_kg,
                "gross_weight_kg":  gross_weight_kg,
            })
        containers.append({"container_no": cid, "bundles": bundles})
    data["containers"] = containers
    data["packing_list_layout"] = "nnrc"
    data["unrecognized_weight_unit_count"] = unrecognized_unit_count

    dump_json(pdf_path, "pkg_list_nnrc.json", data)
    return data


# ═══════════════════════════════════════════════════════════════════════════
# CROSS-DOCUMENT VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

def validate(mbl: dict, pkl: dict) -> list[str]:
    results = []

    carrier = s(mbl.get("carrier")).strip()
    if carrier:
        if carrier in CARRIER_MBL_PROMPTS:
            results.append(f"[OK] CARRIER — {carrier} (carrier-specific MBL prompt)")
        else:
            results.append(f"[!]  CARRIER — {carrier} (generic fallback MBL prompt — not yet carrier-tuned, "
                            f"verify every MBL field manually)")

    if pkl.get("batch_no_source") == "pdf_text_layer":
        results.append("[OK] BATCH NO — read directly from the PDF's text layer (deterministic, not the AI model)")
    elif "batch_no_count_mismatch" in pkl:
        mismatch = pkl["batch_no_count_mismatch"]
        results.append(f"[!]  BATCH NO — PDF text layer has {mismatch['pdf_text_layer_count']} '//' batch tokens "
                        f"but extraction returned {mismatch['gemini_row_count']} bundle rows; counts must match "
                        f"exactly to safely auto-correct, so batch numbers below are the AI model's own reading "
                        f"(unverified) — check them manually")
        suspect_count = num(pkl.get("suspect_batch_number_count"), 0)
        if suspect_count:
            results.append(f"[X]  BATCH NO — {suspect_count} row(s) had their batch_no cleared because the "
                            f"extraction returned the row's own BUNDLE number instead of its \"//\" batch number "
                            f"— fill those in manually from the PDF")

    # Row-count ground truth, independent of the batch-token check above and
    # immune to the same page-break split (see _BUNDLE_NO_RE) — pinpoints
    # whether rows were DUPLICATED or SKIPPED, rather than just "skipped or
    # misread" (the Pieces/Net Weight Grand-Total checks further below can't
    # tell the two apart on their own).
    pdf_bundle_row_count = num(pkl.get("pdf_bundle_row_count"), 0)
    gemini_bundle_row_count = num(pkl.get("gemini_bundle_row_count"), 0)
    if pdf_bundle_row_count:
        if pdf_bundle_row_count == gemini_bundle_row_count:
            results.append(f"[OK] ROW COUNT — PDF text layer confirms {pdf_bundle_row_count} bundle rows, matches extraction")
        elif gemini_bundle_row_count > pdf_bundle_row_count:
            results.append(f"[X]  ROW COUNT — PDF text layer has {pdf_bundle_row_count} bundle number(s) but "
                            f"extraction returned {gemini_bundle_row_count} rows — a row was almost certainly "
                            f"DUPLICATED (commonly the one whose \"//\" batch line was orphaned across a page "
                            f"break, see the Batch No check above); re-check the Packing List pages manually")
        else:
            results.append(f"[X]  ROW COUNT — PDF text layer has {pdf_bundle_row_count} bundle number(s) but "
                            f"extraction returned only {gemini_bundle_row_count} rows — one or more rows were "
                            f"SKIPPED; re-check the Packing List pages manually")

    if s(mbl.get("mbl_no")).strip():
        results.append(f"[OK] MBL No — {s(mbl.get('mbl_no')).strip()}")
    else:
        results.append("[X]  MBL No — not found on the MBL")

    mbl_containers = mbl.get("containers", [])
    package_types = {c.get("package_type", "") for c in mbl_containers}
    if mbl_containers and "" in package_types:
        results.append("[!]  PACKAGE TYPE — MBL didn't state BUNDLE/BAG for at least one container — verify "
                        "manually (Bags and Bundles columns are both filled from the same pieces figure "
                        "regardless, so this doesn't affect the output, only confirms what the MBL actually says)")
    elif len(package_types) > 1:
        results.append(f"[!]  PACKAGE TYPE — MBL containers disagree on package type ({sorted(package_types)}) — "
                        f"a shipment is normally all-bundles or all-bags, verify manually")

    mbl_cids = {c["id"] for c in mbl.get("containers", []) if c.get("id")}
    pkl_cids = {c["container_no"] for c in pkl.get("containers", []) if c.get("container_no")}
    common = mbl_cids & pkl_cids
    only_mbl = mbl_cids - pkl_cids
    only_pkl = pkl_cids - mbl_cids

    if common:
        results.append(f"[OK] CONTAINERS — {len(common)} matched across MBL & Packing List")
    for c in sorted(only_mbl):
        results.append(f"[!]  CONTAINER — {c} only in MBL (not in Packing List)")
    for c in sorted(only_pkl):
        results.append(f"[!]  CONTAINER — {c} only in Packing List (not in MBL)")

    mbl_gross_sum = sum(num(c.get("gross_weight_kg"), 0) for c in mbl.get("containers", []))
    pkl_gross_sum = sum(
        num(b.get("gross_weight_kg"), 0)
        for c in pkl.get("containers", [])
        for b in c.get("bundles", [])
    )
    if mbl_gross_sum and pkl_gross_sum:
        if abs(mbl_gross_sum - pkl_gross_sum) < 1:
            results.append(f"[OK] GROSS WEIGHT — MBL({mbl_gross_sum} KG) = Packing List rows sum({pkl_gross_sum} KG)")
        else:
            results.append(f"[!]  GROSS WEIGHT — MBL({mbl_gross_sum} KG) vs Packing List rows sum({pkl_gross_sum} KG)")

    total_bundles = sum(len(c.get("bundles", [])) for c in pkl.get("containers", []))
    if total_bundles:
        results.append(f"[OK] BUNDLES — {total_bundles} bundle row(s) extracted from the Packing List")
    else:
        results.append("[X]  BUNDLES — no bundle rows extracted from the Packing List")

    # Safety net for row-undercount / misread failures on this document's
    # near-identical, multi-page bundle table (the same failure shape as
    # Vinmar's Layout D under-counting) — the Packing List's OWN printed
    # "GRAND TOTAL" line is ground truth, extracted separately from the row
    # list itself, so a mismatch here reliably means rows were skipped or a
    # weight/pieces figure was misread, even when the model's in-prompt
    # self-check missed it.
    pkl_pieces_sum = sum(b.get("pieces", 0) for c in pkl.get("containers", []) for b in c.get("bundles", []))
    grand_total_pieces = num(pkl.get("grand_total_pieces"), 0)
    if grand_total_pieces:
        if pkl_pieces_sum == grand_total_pieces:
            results.append(f"[OK] PIECES — Packing List rows sum({pkl_pieces_sum}) = document's own Grand Total({grand_total_pieces})")
        else:
            results.append(f"[X]  PIECES — Packing List rows sum({pkl_pieces_sum}) vs document's own Grand Total({grand_total_pieces}) "
                            f"— extraction likely skipped or misread rows, re-check the Packing List pages manually")

    pkl_net_sum_mt = sum(b.get("net_weight_mt", 0) for c in pkl.get("containers", []) for b in c.get("bundles", []))
    grand_total_net_mt = num(pkl.get("grand_total_net_mt"), 0)
    if grand_total_net_mt:
        if abs(pkl_net_sum_mt - grand_total_net_mt) < 0.01:
            results.append(f"[OK] NET WEIGHT — Packing List rows sum({pkl_net_sum_mt} MT) = document's own Grand Total({grand_total_net_mt} MT)")
        else:
            results.append(f"[X]  NET WEIGHT — Packing List rows sum({pkl_net_sum_mt} MT) vs document's own Grand Total({grand_total_net_mt} MT) "
                            f"— extraction likely skipped or misread rows, re-check the Packing List pages manually")

    # Safety net for a specific extraction failure seen in practice: the
    # model echoing back one particular batch_no (sometimes even a literal
    # example value from the prompt itself) on several DIFFERENT rows
    # instead of transcribing each row's own "//" line. Within ONE
    # container, every bundle/lot is a distinct batch on both layouts — but
    # ACROSS containers, the same Lot/Batch legitimately supplying multiple
    # containers is normal on the NNRC layout (unlike warehouse 1147, where
    # a batch never repeats at all) — so this check is scoped per-container,
    # never document-wide, to avoid flagging that real, correct case.
    for c in pkl.get("containers", []):
        batch_no_counts: dict = {}
        for b in c.get("bundles", []):
            batch_no = s(b.get("batch_no")).strip()
            if batch_no:
                batch_no_counts[batch_no] = batch_no_counts.get(batch_no, 0) + 1
        for batch_no, count in batch_no_counts.items():
            if count > 1:
                results.append(f"[X]  BATCH NO — \"{batch_no}\" appears on {count} rows within container "
                                f"{c.get('container_no', '?')} — a batch repeating within the SAME container "
                                f"strongly suggests the extraction repeated one row's value instead of reading "
                                f"each row separately; re-check those rows manually")

    # NNRC layout only — every row's "Wt. Unit" is mandatory on the source
    # document; a row where the model didn't return one had its weight
    # defaulted to MT rather than silently left unconverted (see
    # _normalize_weight_unit()) — flagged here so that default is visible
    # rather than trusted blindly.
    unrecognized_unit_count = num(pkl.get("unrecognized_weight_unit_count"), 0)
    if unrecognized_unit_count:
        results.append(f"[!]  WEIGHT UNIT — {unrecognized_unit_count} row(s) had no recognizable Wt. Unit value "
                        f"from the extraction — defaulted to MT (the common case for this layout); verify those "
                        f"rows' Net/Gross Weight manually")

    return results


# ═══════════════════════════════════════════════════════════════════════════
# ROW BUILDER
# ═══════════════════════════════════════════════════════════════════════════

def build_rows(mbl: dict, pkl: dict, reference: str, warehouse: str, eta_date: str = "") -> list[dict]:
    ui_reference = s(reference).strip()
    warehouse = s(warehouse).strip()

    # Origin (Port of Loading), matching Sabic/Vinmar Inbound's convention —
    # NOT destination. E.g. Nhava Sheva, India -> Port of Discharge Antwerp,
    # Belgium should give "IN", not "BE".
    country_code = get_country_code(s(mbl.get("port_of_loading")).strip())

    mbl_map = {c["id"]: c for c in mbl.get("containers", []) if c.get("id")}

    rows = []
    for c in pkl.get("containers", []):
        cid = c.get("container_no", "")
        mbl_entry = mbl_map.get(cid, {})
        container_type = _normalize_container_type_emvia(mbl_entry.get("type", "")) if mbl_entry.get("type") else ""
        seal_no = mbl_entry.get("seal", "")
        # package_type (BUNDLE/BAG, see PACKAGE_TYPE_RULE) is still read and
        # validated (see validate()'s [!] PACKAGE TYPE check) but no longer
        # decides column placement — Bags and Bundles both get the same
        # pieces figure on every row now, regardless of which type the MBL
        # states, per client instruction.

        for b in c.get("bundles", []):
            # A UI-entered Reference is a deliberate manual override and
            # always wins outright when given — same convention as
            # Vinmar's external_id. Only when the UI field is left blank
            # does an Excel-sourced bundle's OWN per-row Ref (read straight
            # off the sheet) get used instead.
            row_reference = ui_reference or s(b.get("reference")).strip()
            receiver = s(b.get("receiver")).strip()
            ref_receiver = f"{row_reference}+{receiver}" if receiver else ""

            pieces = num(b.get("pieces"), 0)

            rows.append({
                "reference":      row_reference,
                "container_no":   cid,
                "container_ref":  f"{cid}/{row_reference}",
                "mbl_no":         s(mbl.get("mbl_no")).strip(),
                "seal_no":        seal_no,
                "container_type": container_type,
                "warehouse":      warehouse,
                "country_code":   country_code,
                "product":        b.get("product", ""),
                "batch_no":       b.get("batch_no", ""),
                "bags_qty":       pieces,
                "net_weight":     num(b.get("net_weight_kg"), 0),
                "gross_weight":   num(b.get("gross_weight_kg"), 0),
                # 0 unless the source document states its own pallet count
                # per row (NNRC's Packing List does; 1147's doesn't and
                # never sets this key on its bundle dicts, so num(None,0)
                # correctly falls back to 0 there).
                "pallet_count":   num(b.get("pallet_count"), 0),
                "ref_receiver":   ref_receiver,
                # Storage Type: no source document states this — left
                # blank for both warehouses until you specify the rule.
                "storage_type":   "",
                "eta_date":       eta_date,
            })

    return rows
