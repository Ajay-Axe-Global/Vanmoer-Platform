"""
Vinmar Inbound (Axia Plastics Europe) — Extraction, validation, and row-building.

Same 3-document flow as Sabic Inbound (MBL + Packing List + Invoice + a
UI-picked ETA date). The MBL's layout changes per ocean carrier (CMA CGM,
Yang Ming, HMM, ONE, OOCL, MSC, Hapag-Lloyd, ...), so instead of one generic
MBL prompt, MBL extraction is a TWO-CALL pipeline:

  1. identify_carrier()  — tiny call, just names the carrier on this MBL.
  2. extract_mbl()        — dispatches to that carrier's own tuned prompt
                             (CARRIER_MBL_PROMPTS), falling back to a generic
                             prompt for any carrier not yet onboarded.

The Packing List itself also comes in more than one layout across
customers/shippers (DL Chemical/Axia-style vs. data-sheet style like
QatarEnergy/Q-Chem) — PKG_LIST_PROMPT self-detects which one it's looking
at, the same way Sabic's packing-list prompt does for its own three layouts.

Country-code lookup, container-type normalization, MT/KG conversion, and
Bags-vs-Big-Bags classification are shared with Sabic Inbound via
helpers/doc_common.py rather than re-implemented here.
"""

import re
from collections import Counter

from helpers.doc_common import (
    determine_pkg_type,
    dump_json,
    fix_container_id,
    get_country_code,
    normalize_container_type,
    num,
    s,
    spaced_container_type,
    to_kg,
)
from helpers.gemini_client import call_gemini

# ═══════════════════════════════════════════════════════════════════════════
# CARRIER IDENTIFICATION (call #1)
# ═══════════════════════════════════════════════════════════════════════════

CARRIER_ID_PROMPT = """You are looking at a Master Bill of Lading / Sea Waybill PDF. Identify which \
ocean carrier issued it — look at the logo, the carrier name in the title \
block, and any "CARRIER:" field. Return ONLY a JSON object, no markdown.

Normalize the name to exactly one of these if it matches (case-sensitive, \
use this exact spelling): "CMA CGM", "MSC", "HAPAG-LLOYD", "OOCL", "MAERSK", \
"COSCO", "ONE", "EVERGREEN", "YANG MING", "ZIM", "HMM", "PIL", "GRIMALDI".

Aliases to watch for: a logo/branding of "ONE" with the text "Ocean Network \
Express" printed nearby -> return "ONE". "HMM CO., LTD." -> "HMM". "Orient \
Overseas Container Line" -> "OOCL". "Grimaldi Deep Sea S.p.A." / "GRIMALDI \
GROUP" -> "GRIMALDI".

If the carrier is real but not in that list, return its name as printed on \
the document. If you cannot tell at all, return "UNKNOWN".

{"carrier": "string"}"""


def identify_carrier(pdf_path: str) -> str:
    data = call_gemini(CARRIER_ID_PROMPT, pdf_path=pdf_path, max_output_tokens=256)
    carrier = s(data.get("carrier", "UNKNOWN")).strip().upper()
    return carrier or "UNKNOWN"


# ═══════════════════════════════════════════════════════════════════════════
# CARRIER-SPECIFIC MBL PROMPTS (call#2)
# ═══════════════════════════════════════════════════════════════════════════

# Shared fragments spliced into every carrier-specific prompt below, so the
# reference/return-schema wording (and the growing list of label variants
# different carriers/shippers use for the same "buyer PO" reference) only
# needs to be maintained in one place.

REF_NOS_GUIDANCE = """- "ref_nos": ALL shipment reference numbers you can find, no matter which
  label introduces them. Label variants seen across carriers/shippers:
  "<BUYER NAME> PO:" (e.g. "AXIA LLC PO:3610482-01"), "VINMAR REF#:"
  (e.g. "VINMAR REF#: 4317801-01"), "VINMAR SO #" (e.g. "VINMAR SO #
  7570079853-2"), "SID" inside an "EXPORT REFERENCES" box (e.g.
  "SID 7570081629-1"), or a bare "PO:" / "PO NO:". Return every one you
  find as an array, exactly as printed (keep the "-01"/"-1"/"-2" suffix and
  any letters, and keep a multi-segment number like
  "250524/80497158/90744676/38730" whole, as ONE string — do not split it).
  Do NOT include booking numbers, B/L numbers, container numbers, or HS
  codes here.
  Some carriers (e.g. Maersk on a QatarEnergy-shipped cargo) print NO
  "<BUYER> PO:" / "VINMAR REF#:" / "SID" label anywhere — in that case, if
  an "Export references" / "Export reference" box is present near the top
  of the document and contains a number that isn't the Booking No. or B/L
  No., capture that number here too (as one whole string, whatever its
  shape) — it is this shipment's only usable reference on this document."""

RETURN_SCHEMA = """
Return:
{
  "mbl_no": "string", "port_of_loading": "string", "port_of_discharge": "string",
  "vessel": "string", "consignee": "string", "product": "string", "grade": "string",
  "ref_nos": ["string"], "pallets_per_container": 0, "hs_code": "string",
  "container_type": "string", "total_bags": 0, "total_gross_weight_kg": 0,
  "containers": [
    {"id": "string", "seal": "string", "type": "string", "bags": 0,
     "gross_weight_kg": 0, "tare_kg": 0, "measurement_cbm": 0}
  ]
}"""

 
# CMA CGM prints (at least) two structurally different templates — carrier
# identification alone can't tell them apart, so this ONE prompt self-detects
# which one it's looking at first (same pattern as Sabic's/our own
# PKG_LIST_PROMPT self-detecting multiple packing-list layouts).
CMA_CGM_MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this CMA CGM \
Waybill / Bill of Lading PDF (it may span multiple sheets — read all of \
them) and return ONLY a JSON object, no markdown, no explanation.

This document uses ONE of two templates. Identify which one FIRST from the
detection cues below, then apply ONLY that template's rules — they use
similar-sounding labels for different things, so do not mix rules across
templates.

══════════════════════════════════════════
TEMPLATE 1 — "WAYBILL NON NEGOTIABLE" (e.g. Korea -> Belgium shipments)
══════════════════════════════════════════
How to detect: title says "WAYBILL" / "NON NEGOTIABLE"; the container table
column header reads "MARKS AND NOS / CONTAINER AND SEALS"; GRADE/PRODUCT/PO
are printed ONCE for the whole shipment, usually on a later sheet titled
"Continued From Previous Sheet".

- "mbl_no": the WAYBILL NUMBER (top right box).
- "port_of_loading" / "port_of_discharge": as labeled.
- "vessel": VESSEL name + VOYAGE NUMBER combined.
- "consignee": the CONSIGNEE company name (not the address lines).
- "product": the value after "PRODUCT:" (e.g. "HDPE").
- "grade": the value after "GRADE :" (e.g. "HDXP9000") — printed ONCE,
  applies to every container.
@@REF_NOS_GUIDANCE@@
- "pallets_per_container": the number in a line like "16 PALLETS PER
  CONTAINER". 0 if absent.
- "hs_code": the value after "H.S. CODE :".
- "container_type" / "total_bags" / "total_gross_weight_kg": leave at their
  default (empty / 0 / 0) — every container already carries its own type,
  bags, and gross weight directly (see below).
- Container table (repeats once per container, across all sheets): each
  row has a "MARKS AND NOS / CONTAINER AND SEALS" cell with the container
  number on one line and "SEAL <number>" on the next line; a "NO AND KIND
  OF PACKAGES" cell like "1 x 40HC   960 BAGS" (type AND bag count
  together); then GROSS WEIGHT CARGO, TARE, MEASUREMENT columns (already
  KGS/KGS/CBM). Return per container: "id" (4 letters+7 digits, no spaces,
  seal not included), "seal" (the number after "SEAL"), "type" (as
  printed), "bags", "gross_weight_kg", "tare_kg", "measurement_cbm".

══════════════════════════════════════════
TEMPLATE 2 — "BILL OF LADING FOR COMBINED TRANSPORT AND PORT TO PORT
SHIPMENT" (e.g. US-origin shipments, numbered field boxes)
══════════════════════════════════════════
How to detect: title says "BILL OF LADING FOR COMBINED TRANSPORT AND PORT
TO PORT SHIPMENT"; field labels carry box numbers like "SHIPPER/EXPORTER
(2)", "CONSIGNEE (3)", "EXPORT REFERENCES (6)", "DESCRIPTION OF GOODS
(18)"; GRADE is printed on EVERY container's own row (not once for the
whole shipment).

- "mbl_no": "DOCUMENT NO (5)" / "BL/No." value (e.g. "NAM9467111").
- "port_of_loading": "PORT OF LOADING (12)".
- "port_of_discharge": "PORT OF DISCHARGE FROM VESSEL (13)".
- "vessel": "VESSEL (11)" name + voyage number combined.
- "consignee": "CONSIGNEE (3)" company name (not the address lines).
- "product": the commodity name repeated on every container row (e.g.
  "HIGH DENSITY POLYETHYLENE").
- "grade": the "GRADE :" value repeated on every container row — identical
  every time, take it from any one row (e.g. "CYNPOL HD0865UV").
- "ref_nos": ONLY the value after "SHIPPER REF:" inside "EXPORT REFERENCES
  (6)" (e.g. "SHIPPER REF: 7570081623-1" -> "7570081623-1"). Never the
  "DOCUMENT NO"/"BL/No.", "AES ITN #" (same number as "DOMESTIC ROUTING/
  EXPORT INSTRUCTIONS (9)"), "S/C #", "HS CODE", "TOTAL BAGS", or any
  container id/"SN#" seal — those look reference-like but aren't.
- "pallets_per_container": 0 (not stated on this template).
- "hs_code": the "HS CODE" value from the shipment-wide totals block near
  the end — document-wide, not per-container.
- "container_type": from a summary line like "21x40HC CONTAINERS:" (e.g.
  "40HC") — applies to every container, rows don't repeat a type..
- "total_bags" / "total_gross_weight_kg": the "TOTAL BAGS ..." and final
  "TOTAL ...KGS" figures from the shipment-wide totals block — cross-check
  values only, every row already carries its own bags/gross weight (below).

Container table — one THREE-line entry per container, spread across every
sheet (read all of them, do not stop after the first sheet's rows):
  <CONTAINER ID> <BAGS COUNT> BAG <GROSS WEIGHT>KGS <MEASUREMENT>CBM
  SN# <SEAL>
  <PRODUCT NAME>
   GRADE : <GRADE>
  Example:
    "SEGU6357340 960 BAG 24454.000KGS 40.000CBM"
    "SN# PX288469 HIGH DENSITY POLYETHYLENE"
    " GRADE : CYNPOL HD0865UV"
  -> id "SEGU6357340", bags 960, gross_weight_kg 24454.000,
  measurement_cbm 40.000, seal "PX288469", type "" (use the shipment-wide
  "container_type" instead), tare_kg 0 (not printed).

A shipment-wide totals block — "QUANTITY ... MTS", "TOTAL BAGS ...",
"FREIGHT PREPAID", "AES ITN #", "HS CODE", and, only on the very last
sheet, a further "TOTAL ...KGS ...CBM" line plus a bag count and "S/C #" —
is printed glued directly after ONE container's own three lines (often the
last one on the page). That block is shipment-wide, belongs only in the
fields above, and is never that container's own bags/gross weight/
measurement, and never a separate container of its own.
@@RETURN_SCHEMA@@"""


# Yang Ming Bill of Lading layout (see YANGMING.pdf): container rows are
# printed as flat description-cell lines, both on the main page AND on a
# following "ATTACHED LIST" continuation page — the continuation page's
# containers belong to this SAME shipment, they are not a separate B/L.
YANG_MING_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
from this Yang Ming Bill of Lading PDF — it usually includes one or more \
"ATTACHED LIST" continuation pages listing MORE containers for this same \
shipment, read every page — and return ONLY a JSON object, no markdown.

HEADER FIELDS:
- "mbl_no": the B/L No.
- "port_of_loading": Port of Loading.
- "port_of_discharge": Port of Discharge.
- "vessel": Vessel name and Voy No combined.
- "consignee": the Consignee company name — strip a leading "TO THE ORDER
  OF" if present (e.g. "TO THE ORDER OF TEGRAL MATERIALS B.V." ->
  "TEGRAL MATERIALS B.V.").

SHIPMENT-LEVEL DESCRIPTION BLOCK (printed once, under the "N/M" marks
column — applies to every container):
- "product": the value after "PRODUCT:".
- "grade": the value after "GRADE:" / "GRADE :".
@@REF_NOS_GUIDANCE@@ On this carrier look specifically for "VINMAR REF#:".
- "pallets_per_container": integer from a line like "18 PALLETS PER
  CONTAINER". 0 if absent.
- "hs_code": value after "H.S. CODE :".
- "container_type" / "total_bags" / "total_gross_weight_kg": leave these at
  their default (empty / 0 / 0) — every container row already carries its
  own type, bags, and gross weight directly (see below).

CONTAINER TABLE — one row per container, repeated identically on the main
page and any "ATTACHED LIST" page(s), all belonging to this one shipment.
Each row is a single line shaped like:
  <CONTAINER ID> <TYPE like 40HQ> FCL/FCL <SEAL, an alphanumeric code like
  YMAW199822> <BAGS COUNT> BAGS <GROSS WEIGHT>KGS <MEASUREMENT>CBM
  Example: "BMOU5742956 40HQ FCL/FCL YMAW199822 1080 BAGS 27540.000KGS
  56.0000CBM" -> id "BMOU5742956", type "40HQ", seal "YMAW199822",
  bags 1080, gross_weight_kg 27540.000, measurement_cbm 56.0000.
Read every such row on every page — do not stop after the main page's rows,
the attached list continues the SAME container table. Leave "tare_kg" 0
(not printed on this carrier's document).
@@RETURN_SCHEMA@@"""


# HMM Sea Waybill layout (see hmm.pdf): unlike CMA CGM/Yang Ming, this
# carrier's per-container rows carry NO weight or bag count at all — only
# shipment-wide totals are printed, in the shipment-level description block.
HMM_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
from this HMM Sea Waybill PDF — it spans multiple pages, and the container \
list is usually split across them, read every page — and return ONLY a \
JSON object, no markdown.

HEADER FIELDS:
- "mbl_no": the B/L No.
- "port_of_loading": Port of Loading.
- "port_of_discharge": Port of Discharge.
- "vessel": Ocean Vessel / Voyage name and number combined.
- "consignee": the Consignee company name.

SHIPMENT-LEVEL DESCRIPTION BLOCK (printed once, under "Description of
Packages and Goods" — applies to the WHOLE shipment, not any one container):
- "container_type": the container type/size from a line like
  "11 X 40'H DC CONTAINERS" (return just the size token, e.g. "40'H DC" —
  it will be normalized afterward). This carrier does NOT repeat a type per
  container row, so this one shipment-wide value applies to every container.
- "total_bags": the shipment-wide bag count (e.g. from "10,560 BAGS"
  extract 10560). This is the WHOLE shipment's bag count, not any one
  container's — leave each row's own "bags" field at 0.
- "total_gross_weight_kg": the document's total Gross Weight figure
  (already in KGS, e.g. "265,584.000 KGS" -> 265584.000). This is the
  ENTIRE shipment's weight, not any one container's — leave each row's own
  "gross_weight_kg" field at 0.
- "product": the value after "PRODUCT:".
- "grade": the value after "GRADE :".
@@REF_NOS_GUIDANCE@@
- "pallets_per_container": integer from "16 PALLETS PER CONTAINER". 0 if
  absent.
- "hs_code": value after "H.S. CODE:".

CONTAINER LIST — this carrier prints ONLY the container id, seal, and a
short type code per row, with NO weight or bag count on the row itself.
Rows look like:
  <CONTAINER ID> / <SEAL CODE>   <TYPE CODE>   CY / CY
  Example: "HMMU4077422 / 26H1503589 DC 4H CY / CY" -> id "HMMU4077422",
  seal "26H1503589". Ignore the "DC 4H" / "CY / CY" tokens on the row
  itself — leave each row's own "type" empty, "bags" 0, "gross_weight_kg"
  0, and "tare_kg" 0; Python fills these in from the shipment-wide totals
  above.
Read every such row across every page — this carrier commonly splits the
container list across 2+ pages, and the total container count is usually
stated somewhere as "ELEVEN (11) CONTAINERS ONLY" or similar — make sure
the number of rows you return matches that stated total.
@@RETURN_SCHEMA@@"""


# ONE / Ocean Network Express Bill of Lading layout (see one.pdf): every
# container row already carries its own bags/type/weight — no shipment-wide
# fallback needed here, unlike HMM.
ONE_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
from this ONE (Ocean Network Express) Bill of Lading PDF and return ONLY a \
JSON object, no markdown.

HEADER FIELDS:
- "mbl_no": the BILL OF LADING NO.
- "port_of_loading": Port of Loading.
- "port_of_discharge": Port of Discharge.
- "vessel": Ocean Vessel Voyage No + Flag combined.
- "consignee": the CONSIGNEE company name.

SHIPMENT-LEVEL DESCRIPTION BLOCK (printed once, in the "DESCRIPTION OF
GOODS" cell under the "N/M" marks row — applies to the whole shipment):
- "product": the general commodity description (e.g. from "75.900 MTS OF
  POLYVINYL CHLORIDE RESIN (PVC) HS-1000R" extract "POLYVINYL CHLORIDE
  RESIN (PVC) HS-1000R" — this carrier doesn't separate a distinct grade
  field, so keep the grade/type code attached to product).
- "grade": leave empty "" unless a separate GRADE label is printed
  elsewhere on this document.
@@REF_NOS_GUIDANCE@@ On this carrier look specifically for "VINMAR SO #".
- "hs_code": value after "HS CODE:".
- "pallets_per_container": 0 (not stated on this carrier's document).
- "container_type" / "total_bags" / "total_gross_weight_kg": leave these at
  their default (empty / 0 / 0) — every container row already carries its
  own type, bags, and gross weight directly (see below).

CONTAINER TABLE — one row per container, ABOVE the shipment-level summary
row (do not confuse the two — see warning below). Each row is shaped like:
  <CONTAINER ID> / <SEAL, alphanumeric like CN35952BF>   <BAGS COUNT> BAGS
  /FCL / FCL/<TYPE like 40HQ>/<GROSS WEIGHT>KGS/<MEASUREMENT>M3
  Example: "BEAU5520851 / CN35952BF   22 BAGS  /FCL / FCL/40HQ/25700.000KGS/
  55.000M3" -> id "BEAU5520851", seal "CN35952BF", bags 22, type "40HQ",
  gross_weight_kg 25700.000, measurement_cbm 55.000.

⚠️ Do NOT treat the "N/M ... BAGS IN TOTAL ... CONTAINER(S) SAID TO
CONTAIN" line below the container rows as a container row itself — that
line is a shipment-wide summary/total (it repeats the sum of all the real
rows above it), not an additional container.
@@RETURN_SCHEMA@@"""


# OOCL Sea Waybill layout (see OOCL.pdf): the FIRST page's "description of
# goods" row is often a shipment-wide summary under a non-container-shaped
# code, not a real container — the actual per-container table is on a later
# "attached list" page.
OOCL_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
from this OOCL Sea Waybill PDF — it spans multiple pages, and the REAL \
per-container table is often on a LATER page under "TO BE CONTINUED ON \
ATTACHED LIST", not the first page — read every page — and return ONLY a \
JSON object, no markdown.

HEADER FIELDS:
- "mbl_no": the SEA WAYBILL NO.
- "port_of_loading": Port of Loading.
- "port_of_discharge": Port of Discharge.
- "vessel": Vessel/Voyage combined.
- "consignee": the CONSIGNEE company name.

SHIPMENT-LEVEL DESCRIPTION BLOCK (in the EXPORT REFERENCES box and the
first DESCRIPTION OF GOODS cell — applies to the whole shipment):
@@REF_NOS_GUIDANCE@@ On this carrier look specifically for a line labeled
  "SID" inside the EXPORT REFERENCES box (e.g. "SID 7570081629-1").
- "product": the COMMODITY name (e.g. "POLYPROPYLENE").
- "grade": a following material/grade code on its own line right after
  COMMODITY, if present (e.g. "CYNPOL PPR02"), else "".
- "hs_code": the value after "HS CODE".
- "pallets_per_container": 0 (not stated on this carrier's document).
- "container_type" / "total_bags" / "total_gross_weight_kg": leave these at
  their default (empty / 0 / 0) — every REAL container row (see below)
  already carries its own type, bags, and gross weight directly.

⚠️ CRITICAL — do not confuse the first page's summary row with a real
container: the first "DESCRIPTION OF GOODS" row on page 1 often shows a
non-standard code in the "CNTR. NOS." column (e.g. "X20260710127480") next
to a "TOTAL BAGS: n" line and a shipment-wide gross weight — that row is a
SUMMARY, not a container (its code does NOT match the 4-letter+7-digit
container ID pattern, e.g. it may start with a digit or be an unusual
length). The REAL container-by-container table is on a later page (after
"TO BE CONTINUED ON ATTACHED LIST"), with rows shaped like:
  <CONTAINER ID> /<SEAL, numeric> / <BAGS COUNT> BAGS /FCL/FCL /<TYPE like
  40HQ>/<GROSS WEIGHT>KGS
  Example: "TGBU9205120 /0029165 / 780 BAGS /FCL/FCL /40HQ/19919.000KGS" ->
  id "TGBU9205120", seal "0029165", bags 780, type "40HQ",
  gross_weight_kg 19919.000.
Only rows matching this real-container shape belong in "containers" — the
first-page summary row is for your own cross-check only (its bags/weight
should equal the sum of the real container rows, e.g. 780+658=1438 should
match "TOTAL BAGS: 1438"), never extract it as a container itself.
@@RETURN_SCHEMA@@"""


# Maersk "Bill of Lading for Ocean Transport" layout (see MAERSK.pdf): no
# "PRODUCT:"/"GRADE:"/"<BUYER> PO:" labels at all on this carrier's document
# (unlike CMA CGM/Yang Ming/HMM) — the goods description is one free-text
# block, and the only reference is a bare number in the "Export references"
# box (no "VINMAR REF#"/"SID" label attached to it).
MAERSK_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
from this Maersk Bill of Lading PDF and return ONLY a JSON object, no \
markdown.

HEADER FIELDS:
- "mbl_no": the B/L No. (top right).
- "port_of_loading": Port of Loading.
- "port_of_discharge": Port of Discharge.
- "vessel": Vessel name and Voyage No combined.
- "consignee": the Consignee company name — strip a leading "TO THE ORDER
  OF" if present (e.g. "TO THE ORDER OF Tegral Materials B.V." -> "Tegral
  Materials B.V.").
@@REF_NOS_GUIDANCE@@ On this carrier there is usually no PO/REF#/SID label
  at all — the "Export references" box near the top (e.g.
  "250524/80497158/90744676/38730") is the only reference on the document,
  capture that whole number.

GOODS DESCRIPTION BLOCK (free text under "PARTICULARS FURNISHED BY
SHIPPER" — applies to the whole shipment, this carrier does not use
"PRODUCT:"/"GRADE:" labels):
- "product": the goods description line (e.g. "HIGH DENSITY POLYETHYLENE
  (HDPE) \"LOTRENE\" Q TR-144" — drop a trailing "- NN.NNN MT" weight
  figure if it's glued onto the same line, that's a weight not part of the
  product name).
- "grade": leave empty "" — this carrier doesn't print a separate grade
  field, "product" above already carries the full description.
- "hs_code": leave empty "" if not printed.
- "pallets_per_container": 0 (not stated on this carrier's document).
- "container_type": leave empty "" — every container row states its own
  type directly (see below).
- "total_bags": the total bag count from a line like "2 containers said to
  contain 1680 BAGS" (extract 1680). Leave each row's own "bags" filled
  from its own row instead when the table gives one (see below) — only
  fall back to this total if a row's bags is genuinely not printed.
- "total_gross_weight_kg": the "TOTAL GROSS WEIGHT" figure in MT, converted
  to KG (multiply by 1000) — e.g. "43.008 MT" -> 43008. Leave each row's
  own "gross_weight_kg" filled from its own row when given (see below).

CONTAINER TABLE — one row per container, printed as a single line each,
directly under "VESSEL / VOYAGE NO:", shaped like:
  <CONTAINER ID><SEAL, alphanumeric with dashes like ML-QA0064803> <TYPE
  like "40 DRY 9'6"> <BAGS COUNT> BAGS <GROSS WEIGHT>KGS <MEASUREMENT>CBM
  Example: "HASU4839118 ML-QA0064803 40 DRY 9'6 840 BAGS 21504.00 KGS
  40.000 CBM" -> id "HASU4839118", seal "ML-QA0064803", type "40 DRY 9'6",
  bags 840, gross_weight_kg 21504.00, measurement_cbm 40.000. The seal is
  NOT separated from the container id by a space here — the container id
  is always exactly the first 4 letters + 7 digits (11 characters) of that
  token, everything after it on the same "word" is the seal.
Leave "tare_kg" 0 (not printed on this carrier's document).
@@RETURN_SCHEMA@@"""


# Grimaldi Deep Sea "COMBINED TRANSPORT BILL OF LADING" layout (see the
# Grimaldi sample): the "Kind of packages; description of goods" column
# holds ONE shipment-wide free-text paragraph (goods description, HS code,
# the real reference, freight terms, invoice/shipment numbers, totals) that
# is only printed once, at the top of that column — but because it's taller
# than a single container row, it visually spans alongside the first
# several containers' rows in the linear text, which makes it LOOK like it
# repeats per container. It does not: there is only one such paragraph for
# the whole document. The "Marks and Nos" column, separately, has one
# repeating block per container (id / seal / tare), and the "Weight kg."
# and "Measurement CBM" columns give each container its own gross weight
# and measurement, aligned to that container's own row.
GRIMALDI_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
from this Grimaldi Deep Sea Combined Transport Bill of Lading PDF — it \
spans multiple pages, with the container list continuing across all of \
them (later pages don't repeat the header, just more container rows), \
read every page — and return ONLY a JSON object, no markdown.

HEADER FIELDS:
- "mbl_no": the "Bl. No." value (same as "Booking No." on this carrier,
  e.g. "S329985073").
- "port_of_loading": "Port of loading" (page 1) / "POL:" (later pages).
- "port_of_discharge": "Port of discharge" (page 1) / "POD:" (later pages).
- "vessel": "Ocean vessel" name + the voyage code shown next to it (e.g.
  "GRANDE GUINEA / GGU0426").
- "consignee": the "Consignee" box company name (not the address lines).

SHIPMENT-WIDE FREE-TEXT PARAGRAPH (printed ONCE, inside the "Kind of
packages; description of goods" column, near the top — do NOT expect it to
repeat for every container, see the note above):
- "product" / "grade": look for a line like "500.000 MT PVC RESIN S66".
  This has NO separate "GRADE :" label — the grade code is glued directly
  onto the product name. So leave "grade" EMPTY "" and put the description
  minus only the leading quantity into "product" (e.g. "PVC RESIN S66").
  If a document you're reading DOES have a distinct "GRADE :" label
  instead, follow that label normally (grade = the labeled value, product
  = the rest) — this rule only covers the no-label case.
- "ref_nos": ONLY a value labeled "VINMAR SO" inside this paragraph (e.g.
  "VINMAR SO 7570081253-3" -> "7570081253-3"). ⚠️ Do NOT use the "Ref. No."
  box near the top of the document — on this carrier it's usually just a
  duplicate of the Booking No./Bl. No., not a real shipment reference. Also
  do NOT use "INVOICE: <value>" (e.g. "INVOICE: EXP 011_26") or "SHIPMENT:
  <value>" (e.g. "SHIPMENT: EM-04112-26") from this same paragraph — those
  are invoice/shipment tracking numbers, not the PO/SO reference.
- "hs_code": the "HS CODE:" value from this paragraph (e.g. "3904.10").
  Ignore the separate "NCM:" code right next to it — that's a Brazilian
  customs classification, not the HS code.
- "pallets_per_container": 0 — a line like "400 PALLETS CONTAINING:" is a
  document-WIDE pallet total, not a per-container figure; do not divide it
  yourself, leave this 0.
- "container_type": the container size from a line like "20 40 ft. High
  Cube" or "20 HC CONTAINERS 40'" near the top of the goods description
  (e.g. "40HC") — applies to every container, since individual container
  rows don't repeat a type.
- "total_bags": 0 — this carrier's documents describe cargo in pallets, not
  bags; a bag count comes from the Packing List instead.
- "total_gross_weight_kg": the "GROSS WEIGHT TOTAL:" figure from this same
  paragraph (already KGS, e.g. "509,390.000 KGS" -> 509390.000) — a
  cross-check value only, every container already has its own gross weight
  on its own row (see below).

CONTAINER TABLE — the "Marks and Nos" column repeats this THREE-line block
once per container, each one paired with that SAME row's own value in the
"Weight kg." and "Measurement CBM" columns:
  <CONTAINER ID>
  Seal #(s):
  <SEAL>
  TARE WEIGHT:
  <TARE> Kgs
  Example:
    "ACLU9795946" (paired on its row with "25,420.000 KGS" and "6,006.000 CBM")
    "Seal #(s):"
    "SA515350"
    "TARE WEIGHT:"
    "3790 Kgs"
  -> id "ACLU9795946", seal "SA515350", tare_kg 3790, gross_weight_kg
  25420.000, measurement_cbm 6006.000 (as printed — do not rescale it).
  Leave "type" empty (use the
  shipment-wide "container_type" above instead) and "bags" 0 (this carrier
  never prints a per-container bag count — it comes from the Packing List).
Read every container block on every page — the total container count is
usually confirmed near the end of page 1 as "Total No. of Containers: N".
@@RETURN_SCHEMA@@"""


# Fallback for any carrier not yet onboarded (MSC / Hapag-Lloyd / etc. will
# each get their own tuned prompt as those samples come in) — same
# field shape as every carrier-specific prompt so build_rows() never needs
# to know which prompt actually ran.
GENERIC_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
from this Master Bill of Lading / Sea Waybill PDF (it may span multiple \
pages/sheets — read all of them) and return ONLY a JSON object, no markdown.

- "mbl_no": the Bill of Lading / Waybill number.
- "port_of_loading": Port of Loading.
- "port_of_discharge": Port of Discharge.
- "vessel": Vessel name and voyage number combined.
- "consignee": the Consignee company name.
- "product": the general product/commodity description exactly as printed.
- "grade": the specific product grade, but ONLY if it is printed under a
  separate label distinct from the product name (e.g. a line reading
  "GRADE : HDXP9000" next to a "PRODUCT: HDPE" line — there, "grade" is
  "HDXP9000"). Leave "grade" EMPTY "" whenever the grade code is just part
  of one combined description with no separate label — in that case put
  the WHOLE description into "product" instead, do not split it yourself.
  Example: "500.000 MT PVC RESIN S66" has no separate grade label, so
  "product" = "PVC RESIN S66" and "grade" = "" — do NOT set product="PVC
  RESIN" and grade="S66", that throws away "RESIN" and "MT" is not part of
  either field.
@@REF_NOS_GUIDANCE@@
- "pallets_per_container": if a line states a flat pallet count per
  container (e.g. "16 PALLETS PER CONTAINER"), the integer part of that
  (16). Otherwise 0.
- "hs_code": the H.S. Code, if present.
- "container_type": if the document states ONE container type/size for the
  whole shipment instead of repeating it per container (e.g. "11 X 40'H DC
  CONTAINERS"), put that here and leave each row's own "type" empty.
  Otherwise leave this "".
- "total_bags": if the document only gives a shipment-wide bag count with
  no per-container breakdown, put that total here and leave each row's own
  "bags" at 0. Otherwise leave this 0.
- "total_gross_weight_kg": if the document only gives a shipment-wide gross
  weight with no per-container breakdown, put that total here (convert
  from MT if needed — multiply by 1000) and leave each row's own
  "gross_weight_kg" at 0. Otherwise leave this 0.
- "containers": array of every container, each with:
  - "id": container number, exactly 4 letters + 7 digits, no spaces.
  - "seal": seal number.
  - "type": container type as printed (e.g. "40' High Cube"), empty if
    only a shipment-wide "container_type" above applies instead.
  - "bags": bag/package count for this container, if printed on the MBL.
  - "gross_weight_kg": gross weight for this container in KG (convert from
    MT if the document states it in MT — multiply by 1000).
  - "tare_kg": tare weight in KG, if printed.
  - "measurement_cbm": measurement in CBM, if printed.
@@RETURN_SCHEMA@@"""


for _name in (
    "CMA_CGM_MBL_PROMPT", "YANG_MING_MBL_PROMPT", "HMM_MBL_PROMPT",
    "ONE_MBL_PROMPT", "OOCL_MBL_PROMPT", "MAERSK_MBL_PROMPT", "GRIMALDI_MBL_PROMPT",
    "GENERIC_MBL_PROMPT",
):
    globals()[_name] = (
        globals()[_name]
        .replace("@@REF_NOS_GUIDANCE@@", REF_NOS_GUIDANCE)
        .replace("@@RETURN_SCHEMA@@", RETURN_SCHEMA)
    )


CARRIER_MBL_PROMPTS = {
    "CMA CGM":    CMA_CGM_MBL_PROMPT,
    "YANG MING":  YANG_MING_MBL_PROMPT,
    "HMM":        HMM_MBL_PROMPT,
    "ONE":        ONE_MBL_PROMPT,
    "OOCL":       OOCL_MBL_PROMPT,
    "MAERSK":     MAERSK_MBL_PROMPT,
    "GRIMALDI":   GRIMALDI_MBL_PROMPT,
}


# ═══════════════════════════════════════════════════════════════════════════
# PACKING LIST / INVOICE PROMPTS — single fixed layout for this client
# ═══════════════════════════════════════════════════════════════════════════

PKG_LIST_PROMPT = """You are a shipping-document data extractor. Extract all data from this \
Packing List PDF and return ONLY a JSON object, no markdown, no explanation.

This document uses ONE of three layouts. Identify which one FIRST from the
detection cues below, then apply that layout's rules.

══════════════════════════════════════════
LAYOUT A — DL Chemical / Axia style
══════════════════════════════════════════
How to detect: has numbered header boxes like "1)EXPORT/SHIPPER",
"2)CONSIGNEE", a "PRODUCT:" / "GRADE :" / "<BUYER> PO:" description block,
and a bottom table with just three columns: CONTAINER NO. | LOT NO. |
WEIGHT(MT).

- "consignee": the "2)CONSIGNEE" company name — not the address lines.
- "product": the value after "PRODUCT:" (e.g. "HDPE").
- "grade": the value after "GRADE :" (e.g. "XP9000"). More specific than
  "product".
@@REF_NOS_GUIDANCE@@
- "packing_description": the value after "PACKING :" (e.g. "25KG BAGS ON
  PALLETS IN 40FT CONTAINER").
- "pallets_per_container": the integer in a line like "16 PALLETS PER
  CONTAINER". If no such flat-rate line exists, use 0.
- "hs_code": the value after "H.S. CODE :".
- "total_bags": the total package count from "NO OF PKGS" (e.g. from
  "7,680 BAGS" extract 7680).
- "total_net_weight_mt": the total NET-WEIGHT number in MT (e.g. from
  "192 MT" extract 192).
- "total_gross_weight_mt": the total GROSS WEIGHT number in MT, if present
  anywhere on this document. 0 if this document only states net weight
  (common — gross weight then comes from the MBL instead).
- "net_weight_per_bag_kg" / "gross_weight_per_bag_kg": leave at 0 — this
  layout doesn't print a per-bag weight.
- Per-container table (CONTAINER NO. | LOT NO. | WEIGHT(MT)): one entry per
  row, with "container_id" (4 letters + 7 digits), "lot_no", and
  "net_weight_mt" (this row's NET weight in MT). Leave "bags", "seal_no",
  and "container_type" empty/0 for every row — this layout doesn't print
  any of these in the packing list (bags/seal/type come from the MBL
  instead).

══════════════════════════════════════════
LAYOUT B — data-sheet style (e.g. QatarEnergy/Q-Chem)
══════════════════════════════════════════
How to detect: has a "SOLD TO:" / "SHIP TO:" pair of boxes, label:value
summary rows like "NET WEIGHT / QTY", "NET WEIGHT/QTY OF EACH BAG", "TOTAL
NO. OF BAGS", and a bottom table with columns MATERIAL CODE | CONTAINER
NUMBER | CONTAINER TYPE | SEAL NUMBER | BATCH NUMBER | QUANTITY/NET WGT.

- "consignee": the "SHIP TO:" company name — NOT "SOLD TO:". "SOLD TO" is
  the trading company that bought the goods (e.g. Vinmar), it is NEVER the
  receiving customer; "SHIP TO" is the actual destination/customer and is
  the one that belongs in this field.
- "product": the "DESCRIPTION OF GOODS" / "GOODS DESCRIPTION" text (e.g.
  "HIGH DENSITY POLYETHYLENE (HDPE) \"LOTRENE\" Q TR-144").
- "grade": leave empty "" unless a distinct grade code is printed separately
  from the product description.
- "ref_nos": prefer a "VINMAR REF#:" / "<BUYER> PO:" / "PO:" label if one is
  separately present on this document. If NOT — which is the normal case
  for this layout — capture the "Ref.No.:" value instead (e.g.
  "250524/80497158/90744676/38730", keep it whole, do not split on the "/"
  characters). This is a shipper-internal multi-segment number rather than
  a Vinmar-style PO, but it is this shipment's only usable reference when
  no PO/REF# label exists anywhere (confirm it also matches the "Export
  references" box on the matching MBL/waybill, when you can see both).
  Do NOT use "Invoice No." for this field — that's a different number.
- "packing_description": the "PACKING CONDITIONS" text, if present.
- "pallets_per_container": 0 (not stated on this layout).
- "hs_code": leave empty "" if not printed.
- "total_bags": the "TOTAL NO. OF BAGS" value (e.g. from "1,680" extract 1680).
- "total_net_weight_mt": the "NET WEIGHT" / "NET WEIGHT / QTY" value in MT.
- "total_gross_weight_mt": the "GROSS WEIGHT" / "GROSS WEIGHT / QTY" value
  in MT.
- "net_weight_per_bag_kg": the "NET WEIGHT/QTY OF EACH BAG" value in KG
  (e.g. "25.000 KGS" -> 25.0). 0 if not printed.
- "gross_weight_per_bag_kg": the "GROSS WEIGHT/QTY OF EACH BAG" value in KG.
  0 if not printed.
- Per-container table (MATERIAL CODE | CONTAINER NUMBER | CONTAINER TYPE |
  SEAL NUMBER | BATCH NUMBER | QUANTITY/NET WGT PER MT): one entry per row,
  with "container_id" (the CONTAINER NUMBER column), "lot_no" (the BATCH
  NUMBER column), "net_weight_mt" (the QUANTITY/NET WGT column, in MT),
  "seal_no" (the SEAL NUMBER column — empty string if that cell is blank on
  this row, do not guess one), and "container_type" (the CONTAINER TYPE
  column, e.g. "40FT" — return as printed, it will be normalized
  afterward). Leave "bags" 0 for every row — this layout doesn't print a
  bag count.

══════════════════════════════════════════
LAYOUT C — Vinmar-direct style (shipper letterhead is "VINMAR INTERNATIONAL
LLC" itself, no numbered boxes, no SOLD TO/SHIP TO pair)
══════════════════════════════════════════
How to detect: header is a plain label:value list — "INVOICE NUMBER",
"VESSEL", "VOYAGE NO.", "NO OF PACKAGES", "QUANTITY", "TOTAL GROSS WEIGHT",
"TOTAL NET WEIGHT" (⚠️ these two weight totals are in KGS on this layout,
NOT MT, unlike Layout A/B — see conversion note below), "PACKING", and a
"DESCRIPTION OF GOODS" free-text line combining product+grade in one
sentence (e.g. "504 MTS OF HIGH DENSITY POLYETHYLENE GRADE : CYNPOL
HD0865UV"). The bottom table has FOUR columns: CONTAINER NO. | BAGS | NW
(KGS) | LOT NO. — note the BAGS column exists directly on this layout,
unlike Layout A.

- "consignee": this layout has NO consignee/buyer field at all (the
  letterhead names Vinmar itself, the shipper, not the customer) — leave
  this "" and let the matching MBL's own Consignee field supply it instead.
- "product" / "grade": look at the "DESCRIPTION OF GOODS" sentence. IF it
  contains a separate "GRADE :" label, "grade" is the value after it and
  "product" is the rest of the sentence with that "GRADE : ..." part
  removed (e.g. "504 MTS OF HIGH DENSITY POLYETHYLENE GRADE : CYNPOL
  HD0865UV" -> product "HIGH DENSITY POLYETHYLENE", grade "CYNPOL
  HD0865UV"). IF there is NO "GRADE :" label — the grade code is just
  glued onto the product name with no separating label — leave "grade"
  EMPTY "" and put the WHOLE description (minus only the leading
  quantity, e.g. "500.000 MT"/"504 MTS OF") into "product" instead. Do NOT
  guess where a product/grade split belongs when no label marks it.
  Example: "500.000 MT PVC RESIN S66" has no "GRADE :" label, so
  "product" = "PVC RESIN S66" and "grade" = "" — never "product"="PVC
  RESIN" + "grade"="S66", that silently drops "RESIN" from the output.
- "ref_nos": this layout has no PO/REF#/SID label at all — leave [] and let
  the matching MBL's own reference field (e.g. a "SHIPPER REF:" label)
  supply it instead.
- "packing_description": the "PACKING" value (e.g. "IN 25KG BAGS").
- "pallets_per_container": 0 (not stated on this layout).
- "hs_code": leave empty "" if not printed.
- "total_bags": the "NO OF PACKAGES" value (e.g. from "20160 BAGS" extract
  20160).
- "total_net_weight_mt": the "TOTAL NET WEIGHT" value, CONVERTED FROM KG TO
  MT (divide by 1000) — e.g. "504000.000 KGS" -> 504.0. Do not return the
  raw KG number here, this field is always MT regardless of layout.
- "total_gross_weight_mt": the "TOTAL GROSS WEIGHT" value, likewise
  converted from KG to MT (divide by 1000) — e.g. "513534.000 KGS" ->
  513.534.
- "net_weight_per_bag_kg" / "gross_weight_per_bag_kg": leave at 0 — this
  layout doesn't print a per-bag weight (it prints per-bag count directly
  per row instead, see the table below).
- Per-container table (CONTAINER NO. | BAGS | NW (KGS) | LOT NO.): one
  entry per row — the SAME container number legitimately repeats on
  multiple consecutive rows with DIFFERENT bag counts, weights, and lot
  numbers when that physical container was loaded with more than one lot;
  return every such row separately, do NOT merge or deduplicate rows that
  share a container number. For each row: "container_id" (4 letters + 7
  digits), "bags" (the BAGS column value for this row, as printed — this
  is the reliable, most granular bag count), "net_weight_mt" (the NW (KGS)
  column value CONVERTED TO MT — divide by 1000, e.g. "24000" -> 24.0,
  "3000" -> 3.0), and "lot_no" (the LOT NO. column). Leave "seal_no" and
  "container_type" empty for every row — this layout doesn't print either
  in the packing list (they come from the MBL instead).

══════════════════════════════════════════
OUTPUT FORMAT (same shape regardless of which layout you detected — leave
any field the layout doesn't have at its default shown below)
══════════════════════════════════════════
{
  "consignee": "string",
  "product": "string",
  "grade": "string",
  "ref_nos": ["string"],
  "packing_description": "string",
  "pallets_per_container": 0,
  "hs_code": "string",
  "total_bags": 0,
  "total_net_weight_mt": 0,
  "total_gross_weight_mt": 0,
  "net_weight_per_bag_kg": 0,
  "gross_weight_per_bag_kg": 0,
  "containers": [
    {"container_id": "string", "lot_no": "string", "net_weight_mt": 0,
     "bags": 0, "seal_no": "string", "container_type": "string"}
  ]
}"""

PKG_LIST_PROMPT = PKG_LIST_PROMPT.replace("@@REF_NOS_GUIDANCE@@", REF_NOS_GUIDANCE)


INVOICE_PROMPT = """You are a shipping-document data extractor. Extract fields from this \
Commercial Invoice PDF. Return ONLY JSON, no markdown.

- "invoice_no": Invoice Number.
- "consignee": the CONSIGNEE company name.
- "product": the value after "PRODUCT:" (e.g. "HDPE").
- "grade": the value after "GRADE :" (e.g. "XP9000").
@@REF_NOS_GUIDANCE@@
- "qty_mt": the total Quantity, in MT (number).
- "unit_price": the Unit Price (number).
- "amount": the total Amount (number).

{
  "invoice_no": "string",
  "consignee": "string",
  "product": "string",
  "grade": "string",
  "ref_nos": ["string"],
  "qty_mt": 0,
  "unit_price": 0,
  "amount": 0
}"""

INVOICE_PROMPT = INVOICE_PROMPT.replace("@@REF_NOS_GUIDANCE@@", REF_NOS_GUIDANCE)


# ═══════════════════════════════════════════════════════════════════════════
# REFERENCE COLLAPSING — "3610963-01" + "3610963-02" -> "3610963-01+02"
# ═══════════════════════════════════════════════════════════════════════════

_REF_RE = re.compile(r'^(.*?)-(\d+)$')


def build_ref(ref_nos: list) -> str:
    """
    Multiple PO refs sharing the same prefix (everything before the final
    "-NN" suffix) collapse into one string: "3610963-01" + "3610963-02" ->
    "3610963-01+02" — same prefix printed once, suffixes joined with "+".
    A ref with no "-NN" suffix, or refs with different prefixes, are kept
    as their own group and space-joined (mirrors Sabic's _build_ref for the
    rare case of an unrelated second reference on the same shipment).

    Unlike Sabic's version, this does NOT split on "/" first — some
    shippers (e.g. QatarEnergy) print a genuine multi-segment reference like
    "250524/80497158/90744676/38730" that must survive whole, not get
    truncated to its first segment.
    """
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for r in ref_nos or []:
        if not r:
            continue
        base = s(r).strip()
        if not base:
            continue
        m = _REF_RE.match(base)
        if m:
            prefix, suffix = m.group(1), m.group(2)
        else:
            prefix, suffix = base, None
        if prefix not in groups:
            groups[prefix] = []
            order.append(prefix)
        if suffix is not None and suffix not in groups[prefix]:
            groups[prefix].append(suffix)
        elif suffix is None:
            groups[prefix] = None  # marker: whole ref has no suffix to merge

    parts = []
    for prefix in order:
        suffixes = groups[prefix]
        if not suffixes:
            parts.append(prefix)
        elif len(suffixes) == 1:
            parts.append(f"{prefix}-{suffixes[0]}")
        else:
            parts.append(f"{prefix}-" + "+".join(suffixes))
    return " ".join(dict.fromkeys(parts))


# ═══════════════════════════════════════════════════════════════════════════
# EXTRACTION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def _repair_total_contaminated_container(containers: list) -> list:
    """
    Safety net for a recurring extraction failure on some MBL templates
    (e.g. CMA CGM's numbered-box layout): a shipment-wide TOTAL printed
    directly after one container's own row gets attached to that container
    instead of its real per-container bags/gross weight — even after the
    prompt explicitly warns against it, since an LLM instruction is not a
    hard guarantee.

    On the shipments where this happens, every container legitimately
    carries the SAME bags/gross weight (uniform FCL loading), so if exactly
    one container's value towers over the combined total of every OTHER
    container — a physical impossibility for one container among several —
    it has almost certainly absorbed the shipment-wide total. Replace it
    with the value shared by the other containers (the most common one)
    rather than just flagging it, since the correct value is right there,
    unambiguous, printed on every sibling row.

    Deliberately conservative: only acts when there are at least 3
    containers to compare against, only touches a value that is bigger
    than everything else COMBINED (not just "larger than average" — a
    real, if unusual, heavier/fuller container should never trip this),
    and only when the other containers agree on one common value to
    replace it with.
    """
    if len(containers) < 3:
        return containers

    for field in ("bags", "gross_weight_kg"):
        values = [num(c.get(field), 0) for c in containers]
        total = sum(values)
        for i, v in enumerate(values):
            others_sum = total - v
            if v and others_sum and v > others_sum:
                other_vals = [values[j] for j in range(len(values)) if j != i and values[j]]
                if not other_vals:
                    continue
                common_value, count = Counter(other_vals).most_common(1)[0]
                if count >= len(other_vals) / 2:  # the others actually agree
                    print(f"  [MBL REPAIR] Container {containers[i].get('id')} {field}={v} looks like it "
                          f"absorbed a shipment-wide total (every other container agrees on {common_value}) "
                          f"— replaced with {common_value}")
                    containers[i][field] = common_value
    return containers


def extract_mbl(pdf_path: str) -> dict:
    carrier = identify_carrier(pdf_path)
    prompt = CARRIER_MBL_PROMPTS.get(carrier, GENERIC_MBL_PROMPT)

    data = call_gemini(prompt, pdf_path=pdf_path, max_output_tokens=16384)
    dump_json(pdf_path, "mbl_raw.json", data)

    for c in data.get("containers", []):
        cid, seal = fix_container_id(c.get("id", ""), c.get("seal", ""))
        c["id"] = cid
        c["seal"] = seal

    data["containers"] = _repair_total_contaminated_container(data.get("containers", []))

    data["carrier"] = carrier
    dump_json(pdf_path, "mbl.json", data)
    print(f"  [MBL] Carrier identified as {carrier} — "
          f"{'carrier-specific' if carrier in CARRIER_MBL_PROMPTS else 'generic fallback'} prompt used")
    return data


def extract_packing_list(pdf_path: str) -> dict:
    data = call_gemini(PKG_LIST_PROMPT, pdf_path=pdf_path, max_output_tokens=16384)
    dump_json(pdf_path, "pkg_list_raw.json", data)

    containers = []
    for row in data.get("containers", []):
        cid, seal = fix_container_id(row.get("container_id", ""), row.get("seal_no", ""))
        containers.append({
            "container_id":   cid,
            "lot_no":         s(row.get("lot_no")).strip(),
            "net_weight_mt":  num(row.get("net_weight_mt"), 0),
            "bags":           num(row.get("bags"), 0),
            "seal_no":        s(seal).strip(),
            "container_type": s(row.get("container_type")).strip(),
        })
    data["containers"] = containers

    dump_json(pdf_path, "pkg_list.json", data)
    return data


def extract_invoice(pdf_path: str) -> dict:
    data = call_gemini(INVOICE_PROMPT, pdf_path=pdf_path)
    dump_json(pdf_path, "invoice.json", data)
    return data


# ═══════════════════════════════════════════════════════════════════════════
# CROSS-DOCUMENT VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

def validate(mbl: dict, pkl: dict, inv: dict) -> list[str]:
    results = []

    mbl_ref = build_ref(mbl.get("ref_nos", []))
    pkl_ref = build_ref(pkl.get("ref_nos", []))
    inv_ref = build_ref(inv.get("ref_nos", []))

    if mbl_ref and pkl_ref:
        if mbl_ref == pkl_ref:
            results.append(f"[OK] REF — MBL({mbl_ref}) = Packing List({pkl_ref})")
        else:
            results.append(f"[X]  REF MISMATCH — MBL({mbl_ref}) vs Packing List({pkl_ref})")

    if pkl_ref and inv_ref:
        if pkl_ref == inv_ref:
            results.append(f"[OK] REF — Packing List({pkl_ref}) = Invoice({inv_ref})")
        else:
            results.append(f"[X]  REF MISMATCH — Packing List({pkl_ref}) vs Invoice({inv_ref})")

    mbl_grade = s(mbl.get("grade")).strip().upper()
    pkl_grade = s(pkl.get("grade")).strip().upper()
    inv_grade = s(inv.get("grade")).strip().upper()
    if pkl_grade and mbl_grade:
        if pkl_grade == mbl_grade:
            results.append(f"[OK] GRADE — Packing List({pkl_grade}) = MBL({mbl_grade})")
        else:
            results.append(f"[!]  GRADE — Packing List({pkl_grade}) vs MBL({mbl_grade}) — carriers/plants "
                            f"sometimes print a shorthand grade name; verify which is correct")
    if pkl_grade and inv_grade and pkl_grade != inv_grade:
        results.append(f"[!]  GRADE — Packing List({pkl_grade}) vs Invoice({inv_grade})")

    # Safety net for a specific extraction failure mode seen on some MBL
    # templates (e.g. CMA CGM's numbered-box layout): a shipment-wide TOTAL
    # printed right after the last container's own row gets attached to
    # that container instead of its real per-container figures. A single
    # container legitimately holding over half the WHOLE shipment's bags/
    # weight essentially never happens on a normal multi-container FCL
    # shipment — flag it instead of silently trusting it.
    mbl_containers = mbl.get("containers", [])
    if len(mbl_containers) > 2:
        total_mbl_bags = sum(num(c.get("bags"), 0) for c in mbl_containers)
        total_mbl_gross = sum(num(c.get("gross_weight_kg"), 0) for c in mbl_containers)
        for c in mbl_containers:
            cid = c.get("id", "?")
            bags = num(c.get("bags"), 0)
            gross = num(c.get("gross_weight_kg"), 0)
            if total_mbl_bags and bags > 0.5 * total_mbl_bags:
                results.append(f"[!]  SUSPECT — MBL container {cid} bags ({bags}) is over half the "
                                f"shipment's total ({total_mbl_bags}) — likely absorbed a shipment-wide "
                                f"total instead of its own row's value; verify this container manually")
            if total_mbl_gross and gross > 0.5 * total_mbl_gross:
                results.append(f"[!]  SUSPECT — MBL container {cid} gross weight ({gross} KGS) is over half "
                                f"the shipment's total ({total_mbl_gross} KGS) — likely absorbed a "
                                f"shipment-wide total instead of its own row's value; verify this container "
                                f"manually")

    mbl_cids = {c["id"] for c in mbl.get("containers", []) if c.get("id")}
    pkl_cids = {c["container_id"] for c in pkl.get("containers", []) if c.get("container_id")}
    common = mbl_cids & pkl_cids
    only_mbl = mbl_cids - pkl_cids
    only_pkl = pkl_cids - mbl_cids

    if common:
        results.append(f"[OK] CONTAINERS — {len(common)} matched across MBL & Packing List")
    for c in sorted(only_mbl):
        results.append(f"[!]  CONTAINER — {c} only in MBL (not in Packing List)")
    for c in sorted(only_pkl):
        results.append(f"[!]  CONTAINER — {c} only in Packing List (not in MBL)")

    mbl_bags_sum = sum(num(c.get("bags"), 0) for c in mbl.get("containers", []))
    pkl_total_bags = num(pkl.get("total_bags"), 0)
    if mbl_bags_sum and pkl_total_bags:
        if mbl_bags_sum == pkl_total_bags:
            results.append(f"[OK] BAGS — MBL containers sum({mbl_bags_sum}) = Packing List total({pkl_total_bags})")
        else:
            results.append(f"[!]  BAGS — MBL containers sum({mbl_bags_sum}) vs Packing List total({pkl_total_bags})")

    pkl_net_sum_mt = sum(num(c.get("net_weight_mt"), 0) for c in pkl.get("containers", []))
    pkl_total_net_mt = num(pkl.get("total_net_weight_mt"), 0)
    if pkl_net_sum_mt and pkl_total_net_mt:
        if abs(pkl_net_sum_mt - pkl_total_net_mt) < 0.01:
            results.append(f"[OK] NET WEIGHT — Packing List rows sum({pkl_net_sum_mt} MT) = document total({pkl_total_net_mt} MT)")
        else:
            results.append(f"[!]  NET WEIGHT — Packing List rows sum({pkl_net_sum_mt} MT) vs document total({pkl_total_net_mt} MT)")

    return results


# ═══════════════════════════════════════════════════════════════════════════
# CUSTOMER LOOKUP
# ═══════════════════════════════════════════════════════════════════════════

# Only two customers ship through this Vinmar platform. The documents' own
# Consignee ("SHIP TO" on data-sheet-style packing lists) field reliably
# names one of the two across every carrier seen so far — including
# carriers that print a "VINMAR SO #" / "SID" reference instead of a
# "<BUYER> PO:" reference, where the PO-prefix rule below doesn't apply (a
# 757xxxxxxx-style Vinmar SO# has been seen on shipments for BOTH
# customers, so it cannot be used to tell them apart). Name matching is
# therefore tried first; the PO-reference-prefix rule is kept only as a
# last-resort fallback for the rare case a document's consignee field is
# missing or unrecognizable.
# TODO: "Tegral" is a placeholder — swap in the real client name once given.
CUSTOMER_NAME_ALIASES = {
    "AXIA":   "Axia Plastics Europe LLC",
    "TEGRAL": "Tegral",
}

# Fallback only — the leading digit of a "<BUYER> PO:" / "VINMAR REF#:"
# style reference (NOT a "VINMAR SO #" / "SID" style reference, see above).
REF_PREFIX_CUSTOMER_MAP = {
    "3": "Axia Plastics Europe LLC",
    "4": "Tegral",
}


def normalize_customer_name(raw: str) -> str:
    upper = s(raw).upper()
    for keyword, canonical in CUSTOMER_NAME_ALIASES.items():
        if keyword in upper:
            return canonical
    return ""


def get_customer_by_ref_prefix(ref_no: str) -> str:
    ref_no = (ref_no or "").strip()
    if not ref_no:
        return ""
    return REF_PREFIX_CUSTOMER_MAP.get(ref_no[0], "")


# ═══════════════════════════════════════════════════════════════════════════
# ROW BUILDER
# ═══════════════════════════════════════════════════════════════════════════

def build_rows(mbl: dict, pkl: dict, inv: dict, eta_date: str = "", external_id: str = "") -> list[dict]:
    # A UI-entered External ID always wins over whatever reference the
    # documents themselves carry — it's a deliberate manual override, not
    # a fallback, so it's applied unconditionally when given, not just when
    # extraction failed to find one.
    ref_no = s(external_id).strip() or build_ref(mbl.get("ref_nos", [])) or build_ref(pkl.get("ref_nos", [])) or build_ref(inv.get("ref_nos", []))

    customer = (
        normalize_customer_name(pkl.get("consignee"))
        or normalize_customer_name(mbl.get("consignee"))
        or normalize_customer_name(inv.get("consignee"))
        or get_customer_by_ref_prefix(ref_no)
    )

    # Grade is the specific product identifier this client wants in the
    # "Product" column (not the generic "PRODUCT:" line, e.g. "HDPE"). The
    # MBL's grade is the full/correct name (e.g. "HDXP9000") — the Packing
    # List sometimes prints a shorthand missing the "HD" prefix (e.g.
    # "XP9000" for the same grade), so MBL wins whenever both are present.
    # Some carriers (ONE, OOCL, Grimaldi) never print a separate grade at
    # all — fall back to the plain "product" field itself in that case.
    grade = s(mbl.get("grade")).strip() or s(pkl.get("grade")).strip() or s(inv.get("grade")).strip()
    product_field = s(mbl.get("product")).strip() or s(pkl.get("product")).strip() or s(inv.get("product")).strip()

    if grade and product_field and grade.upper() in product_field.upper() and grade.upper() != product_field.upper():
        # Safety net for when the prompt's "no separate grade label ->
        # leave grade empty" instruction gets ignored anyway (LLMs don't
        # always follow instructions) and "grade" comes back as just a
        # short fragment already contained in the fuller "product" string
        # (e.g. grade="S66" when product="PVC RESIN S66") — the fuller
        # string is always the correct output, a bare fragment is never
        # more informative than the string it was cut from.
        product = product_field
    else:
        product = grade or product_field

    country_code = get_country_code(mbl.get("port_of_loading", ""))

    # Straight passthrough only — deliberately NOT derived from bags/weight
    # when a document doesn't state it. 0 means "not given", full stop.
    pallets_per_container = num(pkl.get("pallets_per_container"), 0) or num(mbl.get("pallets_per_container"), 0)

    # Some carriers (e.g. HMM) print one container type for the WHOLE
    # shipment instead of repeating it per container — used as a fallback
    # below whenever a given container's own "type" comes back empty.
    shipment_container_type = normalize_container_type(mbl.get("container_type", "")) if s(mbl.get("container_type")).strip() else ""

    mbl_map = {}
    for c in mbl.get("containers", []):
        mbl_map[c["id"]] = {
            "type":            c.get("type", ""),
            "seal":            c.get("seal", ""),
            "bags":            num(c.get("bags"), 0),
            "gross_weight_kg": num(c.get("gross_weight_kg"), 0),
        }

    # Packing-list rows are kept as a FLAT LIST, not deduped into a dict
    # keyed by container_id — one physical container commonly appears on
    # MULTIPLE packing-list rows (the same container split across two or
    # more lots/batches, a real and common shape, not an error/duplicate).
    # Collapsing those into a dict would silently drop every row but the
    # last for that container. One output row is built per packing-list
    # row below; any container that exists ONLY on the MBL (no packing-list
    # row at all) gets exactly one row of its own, appended after.
    pkl_lines = [
        {
            "container_id":   c.get("container_id", ""),
            "lot_no":         c.get("lot_no", ""),
            "net_weight_mt":  num(c.get("net_weight_mt"), 0),
            "bags":           num(c.get("bags"), 0),
            "seal_no":        c.get("seal_no", ""),
            "container_type": c.get("container_type", ""),
        }
        for c in pkl.get("containers", [])
    ]

    n_containers = len(mbl_map) or len({ln["container_id"] for ln in pkl_lines}) or 1

    # Sum each container's total packing-list net weight up front — needed
    # to split an MBL container-level total (bags, gross weight) across
    # that container's several packing-list rows in proportion to each
    # row's share of the container's weight, whenever a row doesn't already
    # carry its own bags directly. Mirrors Sabic's _effective_bags/
    # container_weight_totals pattern for the same "one container, many
    # packing-list rows" shape.
    container_net_weight_totals: dict = {}
    for ln in pkl_lines:
        container_net_weight_totals[ln["container_id"]] = (
            container_net_weight_totals.get(ln["container_id"], 0) + to_kg(ln["net_weight_mt"], "MT")
        )

    # Fallbacks for the (uncommon) case a document only gives a shipment-wide
    # total instead of any per-container breakdown at all — split evenly
    # across every container. Bags/gross weight can come from either the
    # MBL's own shipment-wide totals (e.g. HMM, which never gives a
    # per-container breakdown) or the packing list's totals — MBL wins
    # whenever both happen to state one.
    mbl_total_bags = num(mbl.get("total_bags"), 0)
    pkl_total_bags = num(pkl.get("total_bags"), 0)
    total_bags_fallback = (mbl_total_bags or pkl_total_bags) / n_containers

    mbl_total_gross_kg = num(mbl.get("total_gross_weight_kg"), 0)
    pkl_total_gross_mt = num(pkl.get("total_gross_weight_mt"), 0)
    total_gross_fallback_kg = mbl_total_gross_kg / n_containers if mbl_total_gross_kg else to_kg(pkl_total_gross_mt, "MT") / n_containers

    total_net_fallback_mt = num(pkl.get("total_net_weight_mt"), 0) / n_containers

    # Weight-per-bag fallback (seen on data-sheet-style packing lists that
    # state "NET WEIGHT/QTY OF EACH BAG" instead of a bag count at all) —
    # used only when nothing else gives a bag count for a row.
    net_weight_per_bag_kg = num(pkl.get("net_weight_per_bag_kg"), 0)

    def _build_row(cid: str, lot_no: str, net_weight_mt, row_bags, row_seal: str, row_type: str) -> dict:
        mbl_entry = mbl_map.get(cid, {})

        seal_no = mbl_entry.get("seal", "") or row_seal

        raw_type = mbl_entry.get("type", "") or row_type
        container_type = normalize_container_type(raw_type) if raw_type else shipment_container_type

        net_weight_kg = to_kg(net_weight_mt or total_net_fallback_mt, "MT")

        container_total_net_kg = container_net_weight_totals.get(cid, 0)
        weight_share = (net_weight_kg / container_total_net_kg) if container_total_net_kg else 1.0

        # Bags: prefer THIS row's own packing-list bags (the most granular,
        # most trustworthy source whenever the packing list gives one at
        # all) — fall back to the MBL's container-level total split
        # proportionally by this row's weight share (needed when one
        # container has several packing-list rows but the MBL only ever
        # states one bag count for the whole container), then a per-bag
        # weight conversion, then an even shipment-wide split as the last
        # resort.
        if row_bags:
            bags = row_bags
        elif mbl_entry.get("bags"):
            bags = round(mbl_entry["bags"] * weight_share)
        elif net_weight_per_bag_kg:
            bags = round(net_weight_kg / net_weight_per_bag_kg)
        else:
            bags = round(total_bags_fallback)

        # Gross weight: this client's packing lists essentially never state
        # gross weight per row, so it comes from the MBL's container-level
        # total — split proportionally by weight share across that
        # container's several packing-list rows, same reasoning as bags.
        if mbl_entry.get("gross_weight_kg"):
            gross_weight_kg = round(mbl_entry["gross_weight_kg"] * weight_share, 3)
        else:
            gross_weight_kg = round(total_gross_fallback_kg, 3)

        pallet_count = pallets_per_container
        pkg_type = determine_pkg_type(net_weight_kg, bags)

        return {
            "reference":      ref_no,
            "container_no":   cid,
            "customer":       customer,
            "container_ref":  f"{cid}/{ref_no}",
            "container_type": container_type,
            "container_type2": spaced_container_type(container_type),
            "seal_no":        seal_no,
            "country_code":   country_code,
            "product":        product,
            "lot_no":         lot_no,
            "bags_count":     bags,
            "pkg_type":       pkg_type,
            "pallet_count":   pallet_count,
            "net_weight":     net_weight_kg,
            "gross_weight":   gross_weight_kg,
            "eta_date":       eta_date,
        }

    rows = [
        _build_row(ln["container_id"], ln["lot_no"], ln["net_weight_mt"], ln["bags"], ln["seal_no"], ln["container_type"])
        for ln in pkl_lines
    ]

    # Containers that exist ONLY on the MBL, with no packing-list row at
    # all — one row each, net weight falls back to an even shipment-wide
    # split since there's no packing-list figure for them specifically.
    pkl_cids = {ln["container_id"] for ln in pkl_lines}
    for cid in mbl_map:
        if cid not in pkl_cids:
            rows.append(_build_row(cid, "", 0, 0, "", ""))

    return rows
