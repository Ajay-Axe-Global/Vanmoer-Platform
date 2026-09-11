"""
Hanwha Inbound (Hanwha TotalEnergies Petrochemical Co., Ltd., Seoul, Korea)
— Extraction, validation, and row-building.

Two source documents (MBL + Container Report — no Invoice, no Packing
List), plus three UI-picked fields (Reference, Ship Name, ETA Date) that are
NOT extracted from either document — same convention as Swiss/Continental
Inbound's UI-picked fields, applied uniformly to every row. There is no
optional Inbound-advice batch upload here (unlike Swiss) — no Product
Reference column, no per-product matching.

The "Container Report" is this client's name for what every other onboarded
client calls a Packing List — same role (per-container/per-lot line items),
different title on the document itself. It states, per line: Container No,
Seal No, Material, Batch No. (sometimes labeled "Lot No." instead — both
seen across samples, self-detected inside ONE prompt), Quantity (MT), Net
weight (KGS), Gross weight (KGS), Pallet Q'ty. Net/Gross weight are ALREADY
in KGS on this document (the header itself says "KGS") — no MT->KG
conversion needed for them, unlike Swiss's PDF Packing List layouts. The
"Quantity" column (in MT) always restates the same figure as Net weight (KG
÷ 1000) and is not separately extracted.

Unlike Swiss/Continental's Packing List, the Container Report never states
a container's Type (20FT/40HC/etc.) or its Bags count anywhere — both are
pulled from the MBL instead: Container Type via the same MBL
shipment-wide-fallback-or-per-row pattern already proven on every other
client, and Bags via a NEW per-container "bags" field added to the MBL
schema below (not needed by Swiss/Continental, since their Packing Lists
already state bags directly). Carrier prompts inherited from Swiss's already
-proven library state a per-container bag count in their own row text for
most carriers (CMA CGM, YANG MING, ONE, OOCL, MAERSK, LX PANTOS) — those
just needed a "bags" field added to their existing schema. A few carriers'
proven layouts (HMM, GRIMALDI, HAPAG-LLOYD, MSC, BORCHARD LINES) don't state
a bag count anywhere on the document at all, per prior tuning on other
clients — those are left to report 0 rather than invent a figure, flagged
in validate().

A container CAN legitimately appear on more than one Container Report line
(e.g. HASU4649463 split across two Batch Nos in the same shipment) — kept as
separate output rows, never merged, same convention as every other client's
split-lot handling.

The MBL, as on every other client, changes shape per ocean carrier
regardless of the Container Report, so MBL extraction is the same two-call
pipeline used by Sabic/Vinmar/Emvia/Continental/Swiss Inbound:
identify_carrier() names the carrier, then extract_mbl() dispatches to that
carrier's own tuned prompt (CARRIER_MBL_PROMPTS — reused here from Swiss
Inbound's already-proven library, with "bags" added to the schema), falling
back to a generic prompt for any carrier not yet onboarded.

Country-code lookup, container-type normalization, container-ID
normalization, and MT/KG conversion are shared with every other client via
helpers/doc_common.py.
"""

import re

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
# MBL CARRIER IDENTIFICATION (call #1)
# ═══════════════════════════════════════════════════════════════════════════

CARRIER_ID_PROMPT = """You are looking at a Master Bill of Lading / Sea Waybill PDF. Identify who \
ISSUED it — the company whose logo/name is in the title block and who \
signs it "AS A CARRIER" (or as forwarding agent) at the bottom, and any \
"CARRIER:" field. Return ONLY a JSON object, no markdown.

⚠️ Some documents are issued by a FREIGHT FORWARDER (e.g. "LX Pantos") \
acting as carrier under a FIATA Multimodal Transport Bill of Lading, while \
the actual ocean VESSEL is operated by a different, separately-named \
shipping line. In that case identify the ISSUER (the forwarder whose name \
is in the title block and who signs the document), NOT the vessel operator \
named elsewhere on the page — they are frequently different companies.

Normalize the name to exactly one of these if it matches (case-sensitive, \
use this exact spelling): "CMA CGM", "MSC", "HAPAG-LLOYD", "OOCL", "MAERSK", \
"COSCO", "ONE", "EVERGREEN", "YANG MING", "ZIM", "HMM", "PIL", "GRIMALDI", \
"LX PANTOS", "BORCHARD LINES".

Aliases to watch for: a logo/branding of "ONE" with the text "Ocean Network \
Express" printed nearby -> return "ONE". "HMM CO., LTD." -> "HMM". "Orient \
Overseas Container Line" -> "OOCL". "Mediterranean Shipping Company" / "MSC \
Mediterranean Shipping Company S.A." -> "MSC". "Maersk A/S" / "Maersk Line" \
-> "MAERSK". "Grimaldi Deep Sea S.p.A." / "GRIMALDI GROUP" -> "GRIMALDI". \
"LX Pantos Logistics" (any branch) -> "LX PANTOS". "Borchard Lines Limited" \
-> "BORCHARD LINES".

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
# Same library as Swiss Inbound, with ONE addition: a per-container "bags"
# field, needed here because (unlike Swiss/Continental's Packing List) the
# Container Report never states bags anywhere — it has to come from the MBL.

OCR_DISAMBIGUATION_RULE = """
⚠️ CHARACTER DISAMBIGUATION — a container ID is ALWAYS exactly 4 LETTERS
followed by 7 DIGITS: a character you're unsure is "O" (letter) or "0"
(digit) is the LETTER "O" if it falls in the first 4 characters, and the
DIGIT "0" if it falls in the remaining 7 — never the other way round.
Seal numbers and lot/batch numbers have no fixed letter/digit pattern to
resolve ambiguity that way, so for THOSE fields look especially carefully
at the actual glyph shape before deciding between visually similar pairs:
"O"/"0", "I"/"1", "S"/"5", "B"/"8", "Z"/"2" — transcribe exactly what is
printed, do not default to whichever reads more like a "normal" number."""

BAGS_RULE = """
⚠️ BAGS — if this document states a bag count for a container (most
carriers print it directly in the container's own row/block, e.g. "960
BAGS" — see that carrier's row pattern above), capture it as "bags"
(integer). If no bag count is printed anywhere for that container on this
document, leave "bags": 0 — never guess or compute one."""

RETURN_SCHEMA = """
@@OCR_DISAMBIGUATION_RULE@@
@@BAGS_RULE@@

Return:
{
  "mbl_no": "string", "port_of_loading": "string",
  "container_type": "string",
  "containers": [
    {"id": "string", "seal": "string", "type": "string", "bags": 0}
  ]
}""".replace("@@OCR_DISAMBIGUATION_RULE@@", OCR_DISAMBIGUATION_RULE).replace("@@BAGS_RULE@@", BAGS_RULE)


CMA_CGM_MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this CMA CGM \
Waybill / Bill of Lading PDF (it may span multiple sheets — read all of \
them) and return ONLY a JSON object, no markdown, no explanation.

This document uses ONE of two templates. Identify which one FIRST from the
detection cues below, then apply ONLY that template's rules.

══════════════════════════════════════════
TEMPLATE 1 — "WAYBILL NON NEGOTIABLE" (e.g. Korea -> Belgium shipments)
══════════════════════════════════════════
How to detect: title says "WAYBILL" / "NON NEGOTIABLE"; the container table
column header reads "MARKS AND NOS / CONTAINER AND SEALS".

- "mbl_no": the WAYBILL NUMBER (top right box).
- "port_of_loading": as labeled.
- "container_type" / "containers[].type": leave BOTH empty — every
  container's own "NO AND KIND OF PACKAGES" cell states its type directly
  (see below), there's no separate shipment-wide fallback needed here.
- Container table (repeats once per container, across all sheets): each
  row has a "MARKS AND NOS / CONTAINER AND SEALS" cell with the container
  number on one line and "SEAL <number>" on the next line; a "NO AND KIND
  OF PACKAGES" cell like "1 x 40HC   960 BAGS" (type AND bag count
  together). Return per container: "id" (4 letters+7 digits, no spaces,
  seal not included), "seal" (the number after "SEAL"), "type" (the
  size/type token from the packages cell, e.g. "40HC"), "bags" (the bag
  count from that same cell, e.g. 960).

══════════════════════════════════════════
TEMPLATE 2 — numbered field boxes (e.g. "SHIPPER/EXPORTER (2)",
"DOCUMENT NO (5)", "DESCRIPTION OF GOODS (18)")
══════════════════════════════════════════
How to detect: field labels carry box numbers.

- "mbl_no": "DOCUMENT NO (5)" / "BL/No." value.
- "port_of_loading": "PORT OF LOADING (12)".
- "container_type": from a summary line like "21x40HC CONTAINERS:" (e.g.
  "40HC") — applies to every container, rows don't repeat a type of their
  own on this template.

⚠️ CRITICAL — the FIRST "MARKS AND NUMBERS"/"DESCRIPTION OF GOODS" entry on
sheet 1 (usually plain "N/M" marks with a free-text goods description and
its own "TOTAL ...KGS"/"TOTAL BAGS" lines) is a shipment-WIDE summary block,
never a real container — do not extract it as one.

REAL CONTAINER ROWS — one THREE-line entry per container, spread across
every sheet (read all of them):
  <CONTAINER ID> <BAGS COUNT> BAG <GROSS WEIGHT>KGS <MEASUREMENT>CBM
  SN# <SEAL>
  <PRODUCT NAME / GRADE line(s) — ignore, not needed>
  Example: "SEGU6357340 960 BAG 24454.000KGS 40.000CBM" then "SN# PX288469
  HIGH DENSITY POLYETHYLENE" -> id "SEGU6357340", seal "PX288469", type ""
  (use the shipment-wide "container_type" instead), bags 960.

A further shipment-wide totals block (QUANTITY/TOTAL BAGS/HS CODE/etc.) is
often glued directly after one container's own three lines — that block
belongs only in the fields above, never in that container's own row, and is
never a separate container of its own.
@@RETURN_SCHEMA@@"""


YANG_MING_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
from this Yang Ming Bill of Lading PDF — it usually includes one or more \
"ATTACHED LIST" continuation pages listing MORE containers for this same \
shipment, read every page — and return ONLY a JSON object, no markdown.

- "mbl_no": the B/L No.
- "port_of_loading": Port of Loading.
- "container_type" / "containers[].type": leave "container_type" empty —
  every container row already carries its own type directly (see below).

CONTAINER TABLE — one row per container, repeated identically on the main
page and any "ATTACHED LIST" page(s), all belonging to this one shipment.
Each row is a single line shaped like:
  <CONTAINER ID> <TYPE like 40HQ> FCL/FCL <SEAL, an alphanumeric code like
  YMAW199822> <BAGS COUNT> BAGS <GROSS WEIGHT>KGS <MEASUREMENT>CBM
  Example: "BMOU5742956 40HQ FCL/FCL YMAW199822 1080 BAGS 27540.000KGS
  56.0000CBM" -> id "BMOU5742956", type "40HQ", seal "YMAW199822", bags 1080.
Read every such row on every page — do not stop after the main page's rows.
@@RETURN_SCHEMA@@"""


HMM_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
from this HMM Sea Waybill / Bill of Lading PDF — it may span multiple \
pages, and the container list is sometimes split across them, read every \
page — and return ONLY a JSON object, no markdown.

- "mbl_no": the B/L No. (may show a carrier prefix immediately before the
  booking number, e.g. "HDMU MAAE69596301" — return it exactly as printed,
  prefix included, if present).
- "port_of_loading": Port of Loading.

⚠️ CONTAINER TYPE — do not take the container row's own terse type code
literally. Find the container SIZE from a line like "11 X 40'H DC
CONTAINERS" or "1 X 40'H DC CONTAINER" (in the shipment-wide description
block) and set "container_type" to size + "FT" (e.g. "40FT") whenever that
line's own code contains "DC" — "DC" = Dry Container, a standard/general-
purpose container, NEVER High Cube, no matter what code follows it (e.g. a
trailing "4H" is an internal ISO size/type code, not a "High Cube"
indicator, even though it contains the letter "H"). Only use "HC" instead
of "FT" if that same line, or the goods description, explicitly says "High
Cube" / "HC" / "9'6". This one "container_type" value applies to every
container in the shipment — leave every row's own "type" field empty.

CONTAINER LIST — one row (sometimes two lines) per container, shaped like
either:
  <CONTAINER ID> / <SEAL CODE>   <TYPE CODE>   CY / CY
  (id and seal separated by "/" on one line, type code on the same or next
  line) — e.g. "HMMU4077422 / 26H1503589 DC 4H CY / CY" -> id
  "HMMU4077422", seal "26H1503589".
Read every such row across every page — the total container count is
usually stated somewhere as "ELEVEN (11) CONTAINERS ONLY" or similar; make
sure the number of rows you return matches that stated total.

⚠️ BAGS — this document's container rows do NOT normally state a bag count
per container. If you find one printed anywhere for a container (e.g. in a
goods-description block tied to that specific container), capture it;
otherwise leave "bags": 0 for every container.
@@RETURN_SCHEMA@@"""


ONE_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
from this ONE (Ocean Network Express) Bill of Lading PDF and return ONLY a \
JSON object, no markdown.

- "mbl_no": the BILL OF LADING NO.
- "port_of_loading": Port of Loading.
- "container_type": leave empty — every container row states its own type
  directly (see below).

CONTAINER TABLE — one row per container, ABOVE the shipment-level summary
row (do not confuse the two — see warning below). Each row is shaped like:
  <CONTAINER ID> / <SEAL, alphanumeric like CN35952BF>   <BAGS COUNT> BAGS
  /FCL / FCL/<TYPE like 40HQ>/<GROSS WEIGHT>KGS/<MEASUREMENT>M3
  Example: "BEAU5520851 / CN35952BF   22 BAGS  /FCL / FCL/40HQ/25700.000KGS/
  55.000M3" -> id "BEAU5520851", seal "CN35952BF", type "40HQ", bags 22.

⚠️ Do NOT treat the "N/M ... BAGS IN TOTAL ... CONTAINER(S) SAID TO
CONTAIN" line below the container rows as a container row itself — that
line is a shipment-wide summary/total, not an additional container.
@@RETURN_SCHEMA@@"""


OOCL_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
from this OOCL Sea Waybill PDF — it spans multiple pages, and the REAL \
per-container table is often on a LATER page under "TO BE CONTINUED ON \
ATTACHED LIST" (or "** TO BE CONTINUED ON ATTACHED LIST **"), not the first \
page — read every page — and return ONLY a JSON object, no markdown.

- "mbl_no": the SEA WAYBILL NO.
- "port_of_loading": Port of Loading.
- "container_type": leave empty — every REAL container row (see below)
  already carries its own type directly.

⚠️ CRITICAL — do not confuse the first page's summary row with a real
container: the first "DESCRIPTION OF GOODS" row on page 1 often shows a
non-standard code in the "CNTR. NOS." column (e.g. "X20260710127480" or
"ITN : X20260622025380") next to a "TOTAL BAGS: n" line and a shipment-wide
gross weight — that row is a SUMMARY, not a container (its code does NOT
match the 4-letter+7-digit container ID pattern). The REAL container-by-
container table is on a later page, with rows shaped like:
  <CONTAINER ID> /<SEAL, numeric> / <BAGS COUNT> BAGS /FCL/FCL /<TYPE like
  40HQ>/<GROSS WEIGHT>KGS
  Example: "TGBU9205120 /0029165 / 780 BAGS /FCL/FCL /40HQ/19919.000KGS" ->
  id "TGBU9205120", seal "0029165", type "40HQ", bags 780.
Only rows matching this real-container shape belong in "containers" — the
first-page summary row is never extracted as a container itself, and do
not stop reading just because a page says "DELIBERATELY LEFT BLANK AND
CONTINUE ON NEXT PAGE" — keep reading the following page.
@@RETURN_SCHEMA@@"""


MAERSK_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
from this Maersk Bill of Lading / Non-Negotiable Waybill PDF — it may span \
multiple pages, read all of them — and return ONLY a JSON object, no \
markdown.

- "mbl_no": the B/L No. (top right).
- "port_of_loading": Port of Loading.
- "container_type": leave empty — every container row states its own type
  directly (see below), on whichever layout this document uses.

This document uses ONE of two layouts:

══════════════════════════════════════════
LAYOUT A — container table directly on the front/goods-description page
══════════════════════════════════════════
Row shaped like:
  <CONTAINER ID><SEAL, alphanumeric with dashes like ML-QA0064803> <TYPE
  like "40 DRY 9'6"> <BAGS COUNT> BAGS <GROSS WEIGHT>KGS <MEASUREMENT>CBM
  Example: "HASU4839118 ML-QA0064803 40 DRY 9'6 840 BAGS 21504.00 KGS
  40.000 CBM" -> id "HASU4839118", seal "ML-QA0064803", type "40 DRY 9'6",
  bags 840. The seal is NOT separated from the container id by a space here
  — the container id is always exactly the first 4 letters + 7 digits (11
  characters) of that token, everything after it on the same "word" is the
  seal.

══════════════════════════════════════════
LAYOUT B — front page is a shipment-wide summary only (NO container ids at
all, just "N containers said to contain..." and totals); the REAL table is
on a later/continuation page (labeled "Page : 2" or similar)
══════════════════════════════════════════
Row + following line shaped like:
  <CONTAINER ID> <TYPE, e.g. "40 DRY 8'6"> <BAGS COUNT> BAG <GROSS
  WEIGHT> KGS <MEASUREMENT> CBM
  Shipper Seal : <SEAL>
  Example: "MRKU0510841 40 DRY 8'6 990 BAG 25245.000 KGS 51.7030 CBM" then
  next line "Shipper Seal : 283978" -> id "MRKU0510841", type "40 DRY 8'6",
  seal "283978", bags 990.

Read every such row/block wherever the real table turns out to be — a
shipment can have more than one container, each its own entry; do not stop
after the first one.
@@RETURN_SCHEMA@@"""


GRIMALDI_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
from this Grimaldi Deep Sea Combined Transport Bill of Lading PDF — it \
spans multiple pages, with the container list continuing across all of \
them (later pages don't repeat the header, just more container rows), \
read every page — and return ONLY a JSON object, no markdown.

- "mbl_no": the "Bl. No." value (same as "Booking No." on this carrier).
- "port_of_loading": "Port of loading" (page 1) / "POL:" (later pages).
- "container_type": the container size from a line like "20 40 ft. High
  Cube" or "20 HC CONTAINERS 40'" near the top of the goods description
  (e.g. "40HC") — applies to every container, since individual container
  rows don't repeat a type.

CONTAINER TABLE — the "Marks and Nos" column repeats this block once per
container:
  <CONTAINER ID>
  Seal #(s):
  <SEAL>
  Example: "ACLU9795946" then "Seal #(s):" then "SA515350" -> id
  "ACLU9795946", seal "SA515350". Leave "type" empty (use the shipment-wide
  "container_type" above instead).
Read every container block on every page — the total container count is
usually confirmed near the end of page 1 as "Total No. of Containers: N".

⚠️ BAGS — this document's container blocks do NOT normally state a bag
count per container. If you find one printed anywhere for a container,
capture it; otherwise leave "bags": 0 for every container.
@@RETURN_SCHEMA@@"""


LX_PANTOS_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
from this LX Pantos Bill of Lading PDF (a freight-forwarder-issued FIATA \
Multimodal Transport Bill of Lading) and return ONLY a JSON object, no \
markdown.

- "mbl_no": the "BL NO." value.
- "port_of_loading": "PORT OF LOADING".
- "container_type": the container size/type from a summary line like
  "4X40 HC" or "FOUR (40HCX4) CONTAINERS ONLY" (e.g. "40HC") — applies to
  every container, since the per-container entries (below) don't repeat a
  type of their own.

⚠️ CONTAINER LIST — this is the part most likely to be misread. The
"CONTAINER NO / SEAL NO / MARKS AND NUMBERS" column packs EVERY container
into ONE compact list, as a repeating TWO-LINE pattern stacked vertically —
NOT a normal one-row-per-container table:
  <CONTAINER ID>/<SEAL>
  (<GROSS WEIGHT>KG/<MEASUREMENT>M3/<BAGS>)/
  Example — this exact shipment has FOUR containers, all in this one list:
    "MSNU6011050/FX46720394"
    "(26,818.000KG/44.000M3/22)/"
    "CAIU4754042/FX46720397"
    "(26,818.000KG/44.000M3/22)/"
    "CAAU7536118/FX46720398"
    "(26,818.000KG/44.000M3/22)/"
    "MSMU5691010/FX46720393"
    "(26,818.000KG/44.000M3/22)/"
  -> FOUR separate container entries: ids "MSNU6011050", "CAIU4754042",
  "CAAU7536118", "MSMU5691010", each with its OWN seal, read right next to
  it, and its OWN bags count (the last number inside the parentheses, e.g.
  22). Read every repetition of the two-line pattern in this column, all
  the way through — do NOT stop after the first one and do NOT merge them
  into a single entry.
Leave "type" empty for every container — not printed per-container on this
document (container_type above covers the type).
@@RETURN_SCHEMA@@"""


HAPAG_LLOYD_MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this Hapag-Lloyd \
Bill of Lading PDF (it may span multiple pages — read all of them) and \
return ONLY a JSON object, no markdown, no explanation.

- "mbl_no": the "B/L-No." value (top right) — NOT the "Carrier's
  Reference" number printed right next to it, that's a different number.
- "port_of_loading": "Port of Loading" value.
- "container_type": leave empty — every container's own block states its
  size/type directly (see below).

CONTAINER TABLE — one block per container, in the "Container Nos., Seal
Nos., Marks and Nos." column. Each block is shaped like:
  <CONTAINER ID>
  SEALS : <SEAL NUMBER>
  <a following unrelated line, e.g. a bare number — see warning below>
  ...
  <container size/type description, e.g. "1 CONT. 20'X8'6" GENERAL PURPOSE
   CONT. SLAC*">
  Example:
    "UACU 3944378"
    "SEALS : HLG6350667"
    "033949"
    "1 CONT. 20'X8'6" GENERAL PURPOSE CONT. SLAC*"
  -> id "UACU3944378" (strip the space), seal "HLG6350667" (ONLY the token
  printed immediately after "SEALS :" on that same line), type "20'X8'6"
  GENERAL PURPOSE CONT." (drop the trailing "SLAC*" stowage-plan marker).

⚠️ A bare number on its OWN line right after the "SEALS :" line (e.g.
"033949" in the example above) is NOT a second seal and is NOT part of the
seal value — leave it out of "seal" entirely. The seal is only ever the
single token that appears directly on the same line as the "SEALS :" label.

Read every container block on every page — do not stop after the first one.

⚠️ BAGS — this document's container blocks do NOT normally state a bag
count per container. If you find one printed anywhere for a container,
capture it; otherwise leave "bags": 0 for every container.
@@RETURN_SCHEMA@@"""


MSC_MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this MSC \
(Mediterranean Shipping Company) Sea Waybill PDF — the front page states \
header fields but refers to an attached "RIDER PAGE" for the actual \
container/cargo table, read every page including the rider page(s) — and \
return ONLY a JSON object, no markdown, no explanation.

- "mbl_no": the "SEA WAYBILL No." value (top right).
- "port_of_loading": "PORT OF LOADING" value.
- "container_type": leave empty — every container's own block states its
  size/type directly (see below).

CONTAINER TABLE — on the "RIDER PAGE", in the "Container Numbers, Seal
Numbers and Marks" column, one block per container shaped like:
  <CONTAINER ID>
  <TYPE, e.g. "40' HIGH CUBE">
  SEAL NUMBER:<SEAL NUMBER>
  Example: "MSDU8106323" then "40' HIGH CUBE" then "SEAL NUMBER:353408" ->
  id "MSDU8106323", type "40' HIGH CUBE", seal "353408".
Read every container block on every rider page — a shipment can have more
than one container, each with its own repeating block; do not stop after
the first one.

⚠️ BAGS — this document's container blocks do NOT normally state a bag
count per container. If you find one printed anywhere for a container,
capture it; otherwise leave "bags": 0 for every container.
@@RETURN_SCHEMA@@"""


BORCHARD_LINES_MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this \
Borchard Lines Bill of Lading PDF and return ONLY a JSON object, no \
markdown, no explanation.

- "mbl_no": the "B/L No." value (top right).
- "port_of_loading": "Port of loading" value.
- "container_type": leave empty — every container's own block states its
  size/type directly (see below).

CONTAINER LIST — in the "Marks and Nos; Container No:" column, one block
per container shaped like:
  <CONTAINER ID>  <TYPE, e.g. "40 HC">
  Seal no  <SEAL NUMBER>
  Example: "BORU7010780   40 HC" then "Seal no   00500126" -> id
  "BORU7010780", type "40 HC", seal "00500126". Read every such block —
  this carrier typically lists SEVERAL containers this way; do not stop
  after the first one.

⚠️ BAGS — this document's container blocks do NOT normally state a bag
count per container. If you find one printed anywhere for a container,
capture it; otherwise leave "bags": 0 for every container.
@@RETURN_SCHEMA@@"""


GENERIC_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
from this Master Bill of Lading / Sea Waybill PDF (it may span multiple \
pages/sheets, and container details may be on an attached rider/
continuation page — read all of them) and return ONLY a JSON object, no \
markdown, no explanation.

- "mbl_no": the Bill of Lading / Waybill number.
- "port_of_loading": Port of Loading.
- "container_type": if the document states ONE container type/size for the
  whole shipment instead of repeating it per container (e.g. "11 X 40'H DC
  CONTAINERS"), put that here and leave each row's own "type" empty.
  Otherwise leave this "".
- "containers": array of every container, each with:
  - "id": container number, exactly 4 letters + 7 digits, no spaces.
  - "seal": seal number.
  - "type": container type as printed (e.g. "40' High Cube"), empty if
    only a shipment-wide "container_type" above applies instead.
  - "bags": bag count for this container if printed anywhere, else 0.

⚠️ Some freight-forwarder-issued documents (FIATA Multimodal Transport Bill
of Lading / "FBL" forms, or a "CONTAINER NO / SEAL NO / MARKS AND NUMBERS"
single combined column) pack MULTIPLE containers' id/seal into ONE compact
column as a repeating two-line pattern, instead of a normal one-row-per-
container table:
  <CONTAINER ID>/<SEAL>
  (<GROSS WEIGHT>KG/<MEASUREMENT>M3/<BAGS>)/
  Example: "MSNU6011050/FX46720394" then "(26,818.000KG/44.000M3/22)/" ->
  id "MSNU6011050", seal "FX46720394", bags 22.
This TWO-LINE pattern repeats once per container, stacked vertically in
that same column/cell — read EVERY repetition of it, do not stop after the
first pair. A separate summary line elsewhere on the page like "4X40 HC" is
the shipment-wide total (container count × type) — route it into
"container_type", it is NOT itself one more container to add to the list.

⚠️ Some documents (e.g. OOCL-style) put a shipment-wide SUMMARY row on the
first page under a non-container-shaped code (doesn't match 4 letters + 7
digits) next to "TOTAL BAGS"/"TOTAL ...KGS" figures, with the REAL
per-container table on a LATER page after a "TO BE CONTINUED ON ATTACHED
LIST" notice — read every page and only extract rows matching the real
4-letter+7-digit container ID shape.
@@RETURN_SCHEMA@@"""


for _name in (
    "CMA_CGM_MBL_PROMPT", "YANG_MING_MBL_PROMPT", "HMM_MBL_PROMPT", "ONE_MBL_PROMPT",
    "OOCL_MBL_PROMPT", "MAERSK_MBL_PROMPT", "GRIMALDI_MBL_PROMPT", "LX_PANTOS_MBL_PROMPT",
    "HAPAG_LLOYD_MBL_PROMPT", "MSC_MBL_PROMPT", "BORCHARD_LINES_MBL_PROMPT", "GENERIC_MBL_PROMPT",
):
    globals()[_name] = globals()[_name].replace("@@RETURN_SCHEMA@@", RETURN_SCHEMA)


CARRIER_MBL_PROMPTS = {
    "CMA CGM":        CMA_CGM_MBL_PROMPT,
    "YANG MING":      YANG_MING_MBL_PROMPT,
    "HMM":            HMM_MBL_PROMPT,
    "ONE":            ONE_MBL_PROMPT,
    "OOCL":           OOCL_MBL_PROMPT,
    "MAERSK":         MAERSK_MBL_PROMPT,
    "GRIMALDI":       GRIMALDI_MBL_PROMPT,
    "LX PANTOS":      LX_PANTOS_MBL_PROMPT,
    "HAPAG-LLOYD":    HAPAG_LLOYD_MBL_PROMPT,
    "MSC":            MSC_MBL_PROMPT,
    "BORCHARD LINES": BORCHARD_LINES_MBL_PROMPT,
}

# ═══════════════════════════════════════════════════════════════════════════
# CONTAINER REPORT PROMPT (PDF path) — one known layout so far
# ═══════════════════════════════════════════════════════════════════════════
# All samples seen so far (multiple vessels/dates) share ONE simple table
# layout: Container No. | Seal No. | Material | Batch No. (sometimes labeled
# "Lot No." instead — both seen) | Quantity | Unit | Net weight | Unit |
# Gross weight | Unit | Pallet Q'ty. Net/Gross weight are already in KGS —
# no MT conversion. "Quantity" (in MT) always restates Net weight ÷ 1000 and
# is not separately extracted.

CONTAINER_REPORT_PROMPT = """You are a shipping-document data extractor. Extract all data from this \
"CONTAINER REPORT" PDF (Hanwha TotalEnergies Petrochemical Co., Ltd. \
letterhead — it may span multiple pages, the table header repeats, read all \
of them) and return ONLY a JSON object, no markdown, no explanation.
@@OCR_DISAMBIGUATION_RULE@@

ONE table with columns: Container No. | Seal No. | Material | Batch No. (on
some shipments this same column is instead labeled "Lot No." — treat either
spelling identically) | Quantity | Unit | Net weight | Unit | Gross weight |
Unit | Pallet Q'ty.

⚠️ A single container CAN legitimately appear on TWO separate rows when its
cargo splits across two different Batch/Lot numbers (e.g. container
"HASU4649463" appearing once with Batch No "P4260407" and again with Batch
No "P4260408", each its own Quantity/Net/Gross weight) — output BOTH rows
separately, never merge them into one.

LINE ITEMS — one per row of the table:
- "container_id": the "Container No." column value (4 letters + 7 digits).
- "seal_no": the "Seal No." column value, exactly as printed.
- "product": the "Material" column value (e.g. "RJ870Z", "BI710", "BI980").
- "lot_no": the "Batch No." (or "Lot No.") column value, exactly as printed.
- "net_weight_kg": the "Net weight" column value — this column's own unit is
  already KGS, copy the number exactly as printed, do NOT convert it.
- "gross_weight_kg": the "Gross weight" column value — same unit rule as net
  weight, already KGS.
- "pallet_qty": the "Pallet Q'ty" column value (integer).

Ignore the "Quantity" (MT) column entirely — it always restates the Net
weight figure in a different unit, nothing new to extract from it.

Read every row on every page of the table — do not stop after the first
page.

Return:
{
  "containers": [
    {"container_id": "string", "seal_no": "string", "product": "string",
     "lot_no": "string", "net_weight_kg": 0, "gross_weight_kg": 0,
     "pallet_qty": 0}
  ]
}""".replace("@@OCR_DISAMBIGUATION_RULE@@", OCR_DISAMBIGUATION_RULE)


# ═══════════════════════════════════════════════════════════════════════════
# EXTRACTION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def extract_mbl(pdf_path: str) -> dict:
    carrier = identify_carrier(pdf_path)
    prompt = CARRIER_MBL_PROMPTS.get(carrier, GENERIC_MBL_PROMPT)

    data = call_gemini(prompt, pdf_path=pdf_path, max_output_tokens=16384)
    dump_json(pdf_path, "mbl_raw.json", data)

    for c in data.get("containers", []):
        cid, seal = fix_container_id(c.get("id", ""), c.get("seal", ""))
        c["id"] = cid
        c["seal"] = seal

    data["carrier"] = carrier
    dump_json(pdf_path, "mbl.json", data)
    print(f"  [MBL] Carrier identified as {carrier} — "
          f"{'carrier-specific' if carrier in CARRIER_MBL_PROMPTS else 'generic fallback'} prompt used")
    return data


def _fuzzy_match_container(cid: str, mbl_cids: set[str], threshold: int = 2) -> str:
    """If cid isn't in mbl_cids, find the closest match within `threshold`
    character differences. Returns the matched MBL id, or the original cid
    if no close match is found — same OCR-confusion safety net as Swiss
    Inbound's extractor.py."""
    if cid in mbl_cids or not cid:
        return cid

    best_match = None
    best_dist = threshold + 1

    for mbl_cid in mbl_cids:
        if len(mbl_cid) != len(cid):
            continue
        dist = sum(1 for a, b in zip(cid, mbl_cid) if a != b)
        if dist < best_dist:
            best_dist = dist
            best_match = mbl_cid

    if best_match and best_dist <= threshold:
        return best_match
    return cid


def extract_container_report(pdf_path: str) -> dict:
    data = call_gemini(CONTAINER_REPORT_PROMPT, pdf_path=pdf_path, max_output_tokens=16384)
    dump_json(pdf_path, "container_report_raw.json", data)

    containers = []
    for row in data.get("containers", []):
        cid, _ = fix_container_id(row.get("container_id", ""))
        containers.append({
            "container_id":    cid,
            "seal_no":         s(row.get("seal_no")).strip(),
            "product":         s(row.get("product")).strip(),
            "lot_no":          s(row.get("lot_no")).strip(),
            # Already KG on this document (header itself says "KGS") — no MT
            # conversion, unlike Swiss/Continental's PDF Packing Lists.
            "net_weight_kg":   num(row.get("net_weight_kg"), 0),
            "gross_weight_kg": num(row.get("gross_weight_kg"), 0),
            "pallet_qty":      num(row.get("pallet_qty"), 0),
        })

    result = {"containers": containers}
    dump_json(pdf_path, "container_report.json", result)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# CROSS-DOCUMENT VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

def validate(mbl: dict, report: dict) -> list[str]:
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
    report_cids = {c["container_id"] for c in report.get("containers", []) if c.get("container_id")}
    common = mbl_cids & report_cids
    only_mbl = mbl_cids - report_cids
    only_report = report_cids - mbl_cids

    if common:
        results.append(f"[OK] CONTAINERS — {len(common)} matched across MBL & Container Report")
    for c in sorted(only_mbl):
        results.append(f"[!]  CONTAINER — {c} only in MBL (not in Container Report) — EXCLUDED from output")
    for c in sorted(only_report):
        results.append(f"[!]  CONTAINER — {c} only in Container Report (not in MBL) — EXCLUDED from output")

    if report.get("containers"):
        results.append(f"[OK] LINE ITEMS — {len(report['containers'])} row(s) extracted from the Container Report")
    else:
        results.append("[X]  LINE ITEMS — no rows extracted from the Container Report")

    if only_report:
        results.append(f"[!]  OUTPUT ROWS — {len(only_report)} Container Report container(s) excluded from the "
                        f"output Excel entirely (no matching MBL entry) — see the CONTAINER lines above "
                        f"for which ones")

    mbl_bags_total = sum(num(c.get("bags"), 0) for c in mbl.get("containers", []))
    if mbl_bags_total == 0 and mbl.get("containers"):
        results.append("[!]  BAGS — the MBL did not state a bag count for any container on this carrier's "
                        "layout — Bags column will be 0 for every row")

    return results


# ═══════════════════════════════════════════════════════════════════════════
# ROW BUILDER
# ═══════════════════════════════════════════════════════════════════════════

# Shipping Line display value — NOT a UI field, same convention as Swiss
# Inbound: derived from the MBL's own already-identified carrier so it can
# never disagree with the MBL. Any carrier NOT in this map displays "Other".
CARRIER_DISPLAY_MAP = {
    "OOCL":            "Orient Overseas Container Line [OOCL]",
    "MSC":             "Mediterrenean Shipping Company [MSC]",
    "HAPAG-LLOYD":     "Hapag Lloyd [HPG]",
    "BORCHARD LINES":  "BORCHARD LINES LTD. [BORCHARD]",
    "MAERSK":          "Maersk [MSK]",
    "EVERGREEN":       "Evergreen [EVG]",
    "ZIM":             "ZIM lines [ZIM]",
    "ONE":             "ONE [ONE]",
    "COSCO":           "Cosco Container Line [COSCO]",
    "YANG MING":       "Yang Ming [YML]",
    "HMM":             "Hyundai Merchant Marine [HMM]",
    "CMA CGM":         "CMA CGM [CMA]",
}


def carrier_display(carrier: str) -> str:
    return CARRIER_DISPLAY_MAP.get(s(carrier).strip().upper(), "Other")


def build_rows(mbl: dict, report: dict, reference: str = "", eta_date: str = "",
                ship_name: str = "") -> list[dict]:
    reference = s(reference).strip()
    ship_name = s(ship_name).strip()
    shipping_line = carrier_display(mbl.get("carrier", ""))

    country_code = get_country_code(s(mbl.get("port_of_loading")).strip())

    mbl_map = {}
    for c in mbl.get("containers", []):
        mbl_map[c["id"]] = {
            "type": s(c.get("type")).strip(),
            "seal": s(c.get("seal")).strip(),
            "bags": num(c.get("bags"), 0),
        }
    mbl_cids = set(mbl_map.keys())

    shipment_container_type = normalize_container_type(mbl.get("container_type", "")) if s(mbl.get("container_type")).strip() else ""

    mbl_no = s(mbl.get("mbl_no")).strip()

    # ── Resolve fuzzy-matched container IDs FIRST, before any weight math ──
    # A container CAN legitimately split across N Container Report rows
    # (different Batch/Lot numbers) — the MBL's "bags" figure is for the
    # WHOLE container, so it must be split across those rows proportional to
    # each row's share of that container's total net weight, never copied in
    # full onto every row (which would double-/N-count it). Same pattern
    # already proven in clients/sabic/inbound/extractor.py's
    # _effective_bags(): bags_for_row = round(mbl_bags * row_wt / total_wt).
    matched_rows = []
    skipped_no_mbl_match = []
    for row in report.get("containers", []):
        cid = row.get("container_id", "")
        matched_cid = _fuzzy_match_container(cid, mbl_cids)
        if matched_cid != cid:
            print(f"  [ROW] Fuzzy-matched Container Report container {cid} → MBL container {matched_cid}")
            cid = matched_cid

        if cid not in mbl_map:
            skipped_no_mbl_match.append(cid)
            continue

        matched_rows.append((cid, row))

    container_weight_totals: dict[str, float] = {}
    for cid, row in matched_rows:
        container_weight_totals[cid] = container_weight_totals.get(cid, 0) + num(row.get("net_weight_kg"), 0)

    rows = []
    for cid, row in matched_rows:
        mbl_entry = mbl_map[cid]
        seal_no = s(row.get("seal_no")).strip() or mbl_entry.get("seal", "")

        raw_type = mbl_entry.get("type", "")
        container_type = normalize_container_type(raw_type) if raw_type else shipment_container_type

        row_net_weight = num(row.get("net_weight_kg"), 0)
        mbl_bags = mbl_entry.get("bags", 0)
        total_wt = container_weight_totals.get(cid, 0)
        if mbl_bags and total_wt:
            bags = round(mbl_bags * (row_net_weight / total_wt))
        else:
            bags = mbl_bags

        # Container/Ref is "reference/container" here — reversed from Swiss
        # Inbound's "container/reference" — per client convention, and there
        # is no Product Reference to fall back on (no Inbound-advice batch
        # feature for this client).
        rows.append({
            "reference":      reference,
            "container_no":   cid,
            "container_ref":  f"{reference}/{cid}",
            "shipping_line":  shipping_line,
            "mbl_no":         mbl_no,
            "seal_no":        seal_no,
            "container_type": container_type,
            "country_code":   country_code,
            "product":        s(row.get("product")).strip(),
            "lot_no":         s(row.get("lot_no")).strip(),
            "bags":           bags,
            "net_weight":     row_net_weight,
            "gross_weight":   num(row.get("gross_weight_kg"), 0),
            "pallet_qty":     num(row.get("pallet_qty"), 0),
            "ship_name":      ship_name,
            "eta_date":       eta_date,
        })

    if skipped_no_mbl_match:
        print(f"  [ROWS] Excluded {len(skipped_no_mbl_match)} Container Report row(s) with no matching MBL "
              f"container: {', '.join(sorted(set(skipped_no_mbl_match)))}")

    return rows
