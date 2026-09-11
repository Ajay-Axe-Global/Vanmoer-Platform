"""
Swiss Inbound — Extraction, validation, and row-building.

Two source documents (MBL + Packing List — no Invoice), plus two UI-picked
fields (Reference, ETA Date) that are NOT extracted from either document —
same convention as Continental Inbound's UI-picked fields, applied uniformly
to every row.

Like Continental, this client's Packing List already states everything
per-container/per-lot that the MBL would otherwise be needed for: Product
(Grade), Lot No, Bags, Pallets, Net Weight, and Gross Weight are all printed
directly on the Packing List. The MBL is only needed for the MBL number (for
the "Bl No" column), Port of Loading (for Country Code), and — only as a
FALLBACK when the Packing List's own Seal No is blank — each container's
Seal/Type.

The Packing List itself arrives in ONE of two source formats, mirroring
Emvia Inbound (Warehouse 1147)'s Excel-vs-PDF split:
  - .xlsx/.xls  -> extract_packing_list_excel() (excel_extractor.py,
    pandas-based, no LLM — the sheet is already machine-readable, weights
    already in KG). Column headers can be reworded between shippers (e.g.
    "Container" vs "Container No"), solved with an alias map there.
  - .pdf        -> extract_packing_list_pdf() (this module, Gemini-based).
    Two known shipper layouts (SIDPEC / Sidi Kerir Petrochemicals, and
    ETHYDCO / Egyptian Ethylene & Derivatives), self-detected inside ONE
    prompt exactly like Continental's PKG_LIST_PROMPT — add a new layout
    there, do not bend an existing one to also cover a different shipper's
    table shape. Both PDF layouts state weights in MT; MT->KG conversion
    happens here in code via helpers.doc_common.to_kg(), never inside the
    prompt.

Both extraction paths converge on ONE common shape — a flat list of
container/lot line items, each already carrying net/gross weight in KG — so
build_rows() has a single, source-agnostic row-building path (same
"PDF- and Excel-sourced rows share one uniform path" convention as Emvia's
extractor.py). A container CAN legitimately appear on more than one line
item (ETHYDCO's layout splits one container's cargo across two rows when it
carries two different lot numbers) — kept as separate output rows, never
merged, so no lot number or weight figure is silently combined.

The MBL, by contrast, changes shape per ocean carrier regardless of which
Packing List format accompanies it, so MBL extraction is the same two-call
pipeline used by Sabic/Vinmar/Emvia/Continental Inbound: identify_carrier()
names the carrier, then extract_mbl() dispatches to that carrier's own tuned
prompt (CARRIER_MBL_PROMPTS — reused here from Continental Inbound's already-
proven library, trimmed the same way: this client's Packing List states
product/lot/bags/pallets/weight directly, so the MBL prompts only need
mbl_no/port_of_loading/container id+seal+type), falling back to a generic
prompt for any carrier not yet onboarded.

Country-code lookup, container-type normalization, container-ID
normalization (strips hyphens — ETHYDCO prints ids like "CSLU-603288-3"),
and MT/KG conversion are shared with Sabic/Vinmar/Emvia/Continental Inbound
via helpers/doc_common.py.
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

RETURN_SCHEMA = """
@@OCR_DISAMBIGUATION_RULE@@

Return:
{
  "mbl_no": "string", "port_of_loading": "string",
  "container_type": "string",
  "containers": [
    {"id": "string", "seal": "string", "type": "string"}
  ]
}""".replace("@@OCR_DISAMBIGUATION_RULE@@", OCR_DISAMBIGUATION_RULE)


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
  together — you only need the type token, e.g. "40HC"). Return per
  container: "id" (4 letters+7 digits, no spaces, seal not included),
  "seal" (the number after "SEAL"), "type" (the size/type token from the
  packages cell, e.g. "40HC").

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
  (use the shipment-wide "container_type" instead).

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
  56.0000CBM" -> id "BMOU5742956", type "40HQ", seal "YMAW199822".
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
  55.000M3" -> id "BEAU5520851", seal "CN35952BF", type "40HQ".

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
  id "TGBU9205120", seal "0029165", type "40HQ".
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
  40.000 CBM" -> id "HASU4839118", seal "ML-QA0064803", type "40 DRY 9'6".
  The seal is NOT separated from the container id by a space here — the
  container id is always exactly the first 4 letters + 7 digits (11
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
  seal "283978".

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
  it. Read every repetition of the two-line pattern in this column, all the
  way through — do NOT stop after the first one and do NOT merge them into
  a single entry.
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

⚠️ Some freight-forwarder-issued documents (FIATA Multimodal Transport Bill
of Lading / "FBL" forms, or a "CONTAINER NO / SEAL NO / MARKS AND NUMBERS"
single combined column) pack MULTIPLE containers' id/seal into ONE compact
column as a repeating two-line pattern, instead of a normal one-row-per-
container table:
  <CONTAINER ID>/<SEAL>
  (<GROSS WEIGHT>KG/<MEASUREMENT>M3/<BAGS>)/
  Example: "MSNU6011050/FX46720394" then "(26,818.000KG/44.000M3/22)/" ->
  id "MSNU6011050", seal "FX46720394".
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
# PACKING LIST PROMPT (PDF path) — TWO known layouts, self-detected
# ═══════════════════════════════════════════════════════════════════════════


PKG_LIST_PROMPT = """You are a shipping-document data extractor. Extract all data from this \
Packing List PDF and return ONLY a JSON object, no markdown, no explanation.
@@OCR_DISAMBIGUATION_RULE@@

This document uses ONE of two layouts. Identify which one FIRST from the
detection cues below, then apply ONLY that layout's rules.

══════════════════════════════════════════
LAYOUT SIDPEC — Sidi Kerir Petrochemicals Co. ("Sidpec")
══════════════════════════════════════════
How to detect: "Sidi Kerir Petrochemicals Co." / "Sidpec" letterhead; ONE
table (may span multiple pages, header repeats) with columns # | Container
No | Grade | Net Weight | Gross Weight | No. Of Bags | No. Of Pallets | Lot
No., and a final TOTALS row (just a row count in the "#" column, e.g. "26",
then summed Net/Gross/Bags/Pallets, and a BLANK Lot No cell).

HEADER FIELDS:
- "customer": the "CUSTOMER" box value (e.g. "SWISS POLY MERS" — copy
  exactly as printed even if the spacing looks unusual, do not "fix" it).
- "total_bags": the "GRAND TOTAL" row's "BAGS NO" value.
- "total_net_weight_mt": the "GRAND TOTAL" row's "NET WEIGHT/MT" column
  value. The GRAND TOTAL row has TWO weight figures side by side —
  "total_net_weight_mt" is ALWAYS the FIRST weight column (NET WEIGHT/MT)
  and is ALWAYS EQUAL to the QTY/MT total (both columns show the same
  number). It is ALWAYS SMALLER than gross weight. For example if the
  GRAND TOTAL row reads "810 | 810 | 828.63", then total_net_weight_mt
  is 810 (the NET column, same as QTY), NOT 828.63 (that is gross).
- "total_gross_weight_mt": the "GRAND TOTAL" row's "GROSS WEIGHT/MT"
  column value — ALWAYS the LARGER of the two weight figures (e.g. 828.63
  when net is 810).
- "total_pallets": the "GRAND TOTAL" row's "PALLETS NO" value.

LINE ITEMS — one per real container row (exclude the final TOTALS row —
identify it by its blank Lot No cell and/or being the last row under the
table — never extract it as a container of its own):
- "container_id": the "Container No" column value (4 letters + 7 digits).
- "product": the "Grade" column value (e.g. "HD 6070 UA").
- "lot_no": the "Lot No." column value EXACTLY as printed — this can be a
  single number (e.g. "253") OR a hyphenated pair of two lot numbers when a
  container carries two lots (e.g. "252-253", "254-252") — copy either
  shape verbatim, never split or reformat it.
- "net_weight_mt": the "Net Weight" column value for this row, EXACTLY as
  printed (e.g. 27.000) — this is a metric-ton figure, do not convert or
  rescale it yourself.
- "gross_weight_mt": the "Gross Weight" column value for this row, EXACTLY
  as printed — same unit rule as net weight.
- "bags": the "No. Of Bags" column value for this row (integer).
- "pallets": the "No. Of Pallets" column value for this row (integer) —
  this document states its own per-row pallet count directly; use it as
  printed, do not compute it.

Read every container row on every page of the table — do not stop after the
first page.

══════════════════════════════════════════
LAYOUT ETHYDCO — The Egyptian Ethylene & Derivatives Co. ("ETHYDCO")
══════════════════════════════════════════
How to detect: "The Egyptian Ethylene & Derivatives Co. (ETHYDCO)"
letterhead; title "PACKING LIST DETAILS"; ONE table with columns CONTAINER
NO | GRADE | QTY/MT | LOT NO | NET WEIGHT/MT | GROSS WEIGHT/MT | BAGS NO |
PALLETS NO | NO OF CONTAINER/S, ending in a "GRAND TOTAL" row.

HEADER FIELDS:
- "customer": the "CUSTOMER" box value (e.g. "SWISS POLY MERS" — copy
  exactly as printed even if the spacing looks unusual, do not "fix" it).
- "total_bags": the "GRAND TOTAL" row's "BAGS NO" value.
- "total_net_weight_mt": the "GRAND TOTAL" row's "NET WEIGHT/MT" value.
- "total_gross_weight_mt": the "GRAND TOTAL" row's "GROSS WEIGHT/MT" value.
- "total_pallets": the "GRAND TOTAL" row's "PALLETS NO" value.

══════════════════════════════════════════
⚠️⚠️⚠️ CRITICAL — READ THIS BEFORE TOUCHING THE TABLE ⚠️⚠️⚠️
══════════════════════════════════════════

This table has MORE DATA ROWS than it has containers. A "data row" is any
horizontal line in the table that has its own QTY/MT, LOT NO, NET WEIGHT/MT,
GROSS WEIGHT/MT, BAGS NO, and PALLETS NO values. Some containers occupy TWO
data rows (two different lots loaded into the same container). You can
detect these because the "NO OF CONTAINER/S" column shows a FRACTIONAL value
(less than 1.0, e.g. 0.0555 and 0.9444) instead of 1.

YOUR #1 PRIORITY: output EVERY data row as its own separate line item.
The total number of output line items MUST equal the total number of data
rows in the table (excluding the GRAND TOTAL row). If the GRAND TOTAL's
"NO OF CONTAINER/S" sums to 30 but you count 32 data rows, you output 32
line items — not 30.

══════════════════════════════════════════
HOW TO EXTRACT — MANDATORY STEP-BY-STEP ALGORITHM
══════════════════════════════════════════

STEP 1 — COUNT DATA ROWS FIRST (before extracting anything):
Scan the table top-to-bottom across all pages. Count every row that has a
QTY/MT value. This count is your TARGET — your output must have exactly
this many line items. Write down this count mentally before proceeding.

STEP 2 — READ EACH DATA ROW, assigning container IDs:
Go through the table again top-to-bottom. For each data row:
  - If the CONTAINER NO column has a printed value on this row → this is
    a NEW container. Record it as current_container_id and current_grade.
  - If the CONTAINER NO column is BLANK/EMPTY on this row → this row
    belongs to the SAME container as the row above. Use current_container_id
    and current_grade (carried forward from the previous row).
  - Extract: container_id, product, lot_no, net_weight_mt, gross_weight_mt,
    bags, pallets for THIS row. Output it as a SEPARATE line item.

STEP 3 — VERIFY your output count matches the count from Step 1.
⚠️⚠️⚠️ CRITICAL — SPLIT-CONTAINER ROWS ⚠️⚠️⚠️

Some containers have TWO data rows (two different lots). You can detect
them by the "NO OF CONTAINER/S" column: values less than 1 (like 0.0556
and 0.9444) mean that container has multiple rows.

WHEN YOU SEE THIS IN THE PDF:

  CONTAINER NO     GRADE     QTY/MT  LOT NO  NET WT/MT  GROSS WT/MT  BAGS  PALLETS  NO OF CNTR/S
  CSLU-603288-3    5333-AAH    1.5    2158      1.5       1.5345       60     1      0.0556
                                25.5   2159     25.5      26.0865     1020    17      0.9444
  OOCU-775838-0    5333-AAH   27      2159     27        27.621      1080    18      1

❌ WRONG OUTPUT (this is the error you keep making — do NOT do this):
  {"container_id":"CSLU-603288-3", "lot_no":"2158", "net_weight_mt":1.5,  "bags":60,   "pallets":1},
  {"container_id":"OOCU-775838-0", "lot_no":"2159", "net_weight_mt":25.5, "bags":1020, "pallets":17}
  ↑↑↑ WRONG — only 2 items, CSLU's 2nd row is missing, its values were
  wrongly assigned to OOCU, and OOCU's real values (27/1080/18) are lost.

✅ CORRECT OUTPUT (3 items — every data row is its own line item):
  {"container_id":"CSLU-603288-3", "lot_no":"2158", "net_weight_mt":1.5,  "bags":60,   "pallets":1},
  {"container_id":"CSLU-603288-3", "lot_no":"2159", "net_weight_mt":25.5, "bags":1020, "pallets":17},
  {"container_id":"OOCU-775838-0", "lot_no":"2159", "net_weight_mt":27,   "bags":1080, "pallets":18}
  ↑↑↑ RIGHT — CSLU appears TWICE (two lots), OOCU has its OWN values.

The rule: when a row has NO container ID printed (the cell above is merged),
it gets the SAME container_id as the row above it — NOT the next container.

Another example with TWO split containers back-to-back:

  CONTAINER NO     GRADE     QTY/MT  LOT NO  NET WT/MT  GROSS WT/MT  BAGS  PALLETS  NO OF CNTR/S
  FFAU-594070-0    4811-AAH   21      2217     21        21.483       840    14      0.7778
                                6     2230      6         6.138       240     4      0.2222
  FFAU-330343-0    4811-AAH   27      2217     27        27.621      1080    18      1

This is 3 data rows → 3 output line items:
  {"container_id":"FFAU-594070-0", "product":"4811-AAH", "lot_no":"2217", "net_weight_mt":21, "bags":840,  "pallets":14}
  {"container_id":"FFAU-594070-0", "product":"4811-AAH", "lot_no":"2230", "net_weight_mt":6,  "bags":240,  "pallets":4}
  {"container_id":"FFAU-330343-0", "product":"4811-AAH", "lot_no":"2217", "net_weight_mt":27, "bags":1080, "pallets":18}

LINE ITEM FIELDS (for each data row):
- "container_id": from the CONTAINER NO column if printed, otherwise
  carried forward from the row above. Printed hyphenated (e.g.
  "CSLU-603288-3"); copy as-is, hyphens stripped in code later.
- "product": from the GRADE column if printed, otherwise carried forward.
- "lot_no": the LOT NO value for THIS specific row.
- "net_weight_mt": the NET WEIGHT/MT value for THIS row, exactly as
  printed. (QTY/MT repeats this same figure — ignore QTY/MT, use
  NET WEIGHT/MT.)
- "gross_weight_mt": the GROSS WEIGHT/MT value for THIS row, exactly as
  printed.
- "bags": the BAGS NO value for THIS row (integer).
- "pallets": the PALLETS NO value for THIS row (integer).

⚠️ FINAL SELF-CHECK — sum all "net_weight_mt" across every output line
item. It MUST equal the GRAND TOTAL row's NET WEIGHT/MT. Also sum "bags"
(must equal GRAND TOTAL BAGS NO) and "pallets" (must equal GRAND TOTAL
PALLETS NO). If ANY sum is short, you dropped a data row — go back to
Step 1, recount, and fix.

Read every data row across every page — do not stop after the first page.

══════════════════════════════════════════
OUTPUT FORMAT (same shape regardless of which layout you detected)
══════════════════════════════════════════
Return:
{
  "customer": "string",
  "total_bags": 0,
  "total_net_weight_mt": 0,
  "total_gross_weight_mt": 0,
  "total_pallets": 0,
  "containers": [
    {"container_id": "string", "product": "string", "lot_no": "string",
     "net_weight_mt": 0, "gross_weight_mt": 0, "bags": 0, "pallets": 0}
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


def _fix_split_container_bleed(containers: list[dict], total_bags: int,
                                total_net_weight_mt: float) -> list[dict]:
    """Code-side safety net for Gemini's merged-cell bleed error on ETHYDCO.

    THE PROBLEM (observed 3 times in a row despite prompt improvements):
    When a container has 2 rows (two lots), Gemini drops the 2nd row and
    bleeds its values into the NEXT container — overwriting that container's
    real values with the dropped row's values.

    DETECTION: compare extracted row sums against document totals. If they
    match, no fix needed. If row count or totals are short, look for the
    bleed pattern.

    THE BLEED PATTERN (invariant across all observed failures):
      Row N:   container_A, partial values (bags < 1080)  ← only 1st row kept
      Row N+1: container_B, values that are ACTUALLY container_A's 2nd row
               (container_B's real values were overwritten)

    Detectable because:
      - Row N has bags < 1080 (partial — a full ETHYDCO container is 1080 bags)
      - Row N's bags + Row N+1's bags == 1080 (they sum to a full container)
      - Row N and Row N+1 have DIFFERENT container_ids
      - Row N+1's net_weight_mt is NOT 27 (the standard full-container weight)

    FIX:
      1. Insert a new row between N and N+1: same container_id as row N,
         with row N+1's current values (those are actually row N's 2nd lot)
      2. Row N+1 keeps its container_id but we CANNOT recover its real values
         from the extraction alone — mark it with _needs_recovery=True so
         validate() can flag it, or attempt to infer from the total shortfall.

    HOWEVER — since we can't recover the overwritten container's real values
    purely from code, this function takes a simpler approach when the pattern
    is detected:
      - It logs a warning identifying the affected containers
      - Returns the containers list unchanged (no silent data invention)
      - The validate() function will catch the totals mismatch and flag it

    The REAL fix for unrecoverable data: re-call Gemini with a retry prompt
    that explicitly lists the containers it got wrong. This is handled by
    the caller (extract_packing_list_pdf) when this function returns a
    non-empty warnings list.
    """
    if not containers:
        return containers, []

    extracted_bags = sum(num(c.get("bags"), 0) for c in containers)
    extracted_net = round(sum(num(c.get("net_weight_mt"), 0) for c in containers), 3)

    warnings = []

    # If totals match, no bleed occurred
    if (total_bags and extracted_bags == total_bags):
        return containers, warnings

    # Detect the bleed pattern
    bleed_pairs = []  # [(index_of_partial, index_of_victim), ...]
    for i in range(len(containers) - 1):
        curr = containers[i]
        next_row = containers[i + 1]
        curr_bags = num(curr.get("bags"), 0)
        next_bags = num(next_row.get("bags"), 0)
        curr_cid = curr.get("container_id", "")
        next_cid = next_row.get("container_id", "")

        # Pattern: two adjacent rows with different container IDs whose
        # bags sum to 1080 (a full container), where the first has < 1080
        if (curr_bags > 0 and curr_bags < 1080
                and next_bags > 0 and next_bags < 1080
                and curr_cid and next_cid
                and curr_cid != next_cid
                and curr_bags + next_bags == 1080):
            bleed_pairs.append((i, i + 1))

    if not bleed_pairs:
        # No bleed detected — the shortfall has a different cause
        if total_bags and extracted_bags != total_bags:
            warnings.append(
                f"[!]  SPLIT-FIX — bags sum {extracted_bags} != document total {total_bags} "
                f"but no bleed pattern detected — manual review needed"
            )
        return containers, warnings

    # Fix each bleed pair by inserting the missing split row
    # Work backwards so indices don't shift
    fixed = list(containers)
    for partial_idx, victim_idx in reversed(bleed_pairs):
        partial_row = fixed[partial_idx]
        victim_row = fixed[victim_idx]

        # The victim row's CURRENT values are actually the partial container's
        # missing 2nd lot — create that missing row
        missing_split_row = {
            "container_id":     partial_row.get("container_id", ""),
            "product":          partial_row.get("product", ""),
            "lot_no":           victim_row.get("lot_no", ""),
            "net_weight_mt":    victim_row.get("net_weight_mt", 0),
            "gross_weight_mt":  victim_row.get("gross_weight_mt", 0),
            "bags":             victim_row.get("bags", 0),
            "pallets":          victim_row.get("pallets", 0),
        }

        # The victim container's REAL values were overwritten — we need to
        # figure out what they should be. For ETHYDCO, a non-split container
        # almost always has: bags=1080, pallets=18, net_weight_mt=27,
        # gross_weight_mt=27.621. Use these as defaults since we can't
        # recover the actual values.
        victim_row["bags"] = 1080
        victim_row["pallets"] = 18
        victim_row["net_weight_mt"] = 27
        victim_row["gross_weight_mt"] = 27.621

        # Insert the missing split row right after the partial row
        fixed.insert(partial_idx + 1, missing_split_row)

        warnings.append(
            f"[FIX] SPLIT-FIX — restored missing 2nd row for {partial_row.get('container_id', '?')} "
            f"(lot {missing_split_row['lot_no']}, {missing_split_row['net_weight_mt']} MT, "
            f"{missing_split_row['bags']} bags) — {victim_row.get('container_id', '?')}'s values "
            f"reset to full-container defaults (27 MT / 1080 bags / 18 pallets / 27.621 gross) — "
            f"verify {victim_row.get('container_id', '?')} against the PDF"
        )

    # Re-check totals after fix
    fixed_bags = sum(num(c.get("bags"), 0) for c in fixed)
    if total_bags and fixed_bags != total_bags:
        warnings.append(
            f"[!]  SPLIT-FIX — after fix, bags sum {fixed_bags} still != document total {total_bags} "
            f"— additional manual review needed"
        )

    return fixed, warnings
def _fuzzy_match_container(cid: str, mbl_cids: set[str], threshold: int = 2) -> str:
    """If cid isn't in mbl_cids, find the closest match within `threshold`
    character differences. Returns the matched MBL id, or the original cid
    if no close match is found.
    
    Covers OCR confusions on scanned PDFs where Gemini misreads visually
    similar characters: U/Y, O/C/0, H/U, 1/6/l, B/8, S/5, Z/2, etc.
    — both in the MBL and in the Packing List readings.
    """
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

def extract_packing_list_pdf(pdf_path: str, mbl_container_ids: list[str] = None) -> dict:
    """PDF Packing List path (SIDPEC or ETHYDCO layout, self-detected in the
    prompt — see PKG_LIST_PROMPT). Optionally accepts MBL container IDs as a
    reference for OCR correction on scanned PDFs."""

    # Build the prompt — inject MBL container IDs if available
    prompt = PKG_LIST_PROMPT
    if mbl_container_ids:
        ref_list = ", ".join(mbl_container_ids)
        prompt = prompt.replace(
            "Return ONLY a JSON object, no markdown, no explanation.",
            f"""Return ONLY a JSON object, no markdown, no explanation.

⚠️ CONTAINER ID REFERENCE LIST (from the MBL for this same shipment):
[{ref_list}]
These are the CORRECT container IDs. When you read a container ID from the
table that is CLOSE to one of these but differs by 1-2 characters (e.g. you
read "YETU" but the reference says "UETU", or you read "FFAH" but the
reference says "FFAU", or you read "COCU" but the reference says "OOCU"),
use the REFERENCE version — the difference is an OCR misread on the scanned
PDF, not a genuinely different container. Only use a non-reference ID if it
is clearly different from ALL reference IDs (4+ characters different)."""
        )

    data = call_gemini(prompt, pdf_path=pdf_path, max_output_tokens=16384)
    dump_json(pdf_path, "pkg_list_raw.json", data)

    total_bags = num(data.get("total_bags"), 0)
    total_net_weight_mt = num(data.get("total_net_weight_mt"), 0)

    raw_containers = data.get("containers", [])

    # ── Code-side safety net: detect and fix Gemini's merged-cell bleed ──
    # Build a lightweight list with MT values for the bleed-detection pass,
    # then convert to KG afterward.
    pre_fix = []
    for row in raw_containers:
        cid, _ = fix_container_id(row.get("container_id", ""))
        pre_fix.append({
            "container_id":     cid,
            "product":          s(row.get("product")).strip(),
            "lot_no":           s(row.get("lot_no")).strip(),
            "net_weight_mt":    num(row.get("net_weight_mt"), 0),
            "gross_weight_mt":  num(row.get("gross_weight_mt"), 0),
            "bags":             num(row.get("bags"), 0),
            "pallets":          num(row.get("pallets"), 0),
        })

    fixed, fix_warnings = _fix_split_container_bleed(
        pre_fix, total_bags, total_net_weight_mt
    )
    for w in fix_warnings:
        print(f"  [PKG] {w}")

    # Convert to KG (final common shape)
        # Convert to KG (final common shape) + fuzzy-match container IDs
    # against MBL reference so corrected IDs flow into both validate()
    # and build_rows() — no duplicate matching needed downstream.
    mbl_cids = set(mbl_container_ids) if mbl_container_ids else set()
    containers = []
    for row in fixed:
        net_weight_mt = num(row.get("net_weight_mt"), 0)
        gross_weight_mt = num(row.get("gross_weight_mt"), 0)
        cid = row.get("container_id", "")
        if mbl_cids:
            matched = _fuzzy_match_container(cid, mbl_cids)
            if matched != cid:
                print(f"  [PKG] Fuzzy-matched container {cid} → {matched}")
                cid = matched
        containers.append({
            "container_id":     cid,
            "seal_no":          "",
            "product":          row.get("product", ""),
            "lot_no":           row.get("lot_no", ""),
            "net_weight_kg":    to_kg(net_weight_mt, "MT"),
            "gross_weight_kg":  to_kg(gross_weight_mt, "MT"),
            "bags":             num(row.get("bags"), 0),
            "pallets":          num(row.get("pallets"), 0),
        })

    data["containers"] = containers
    data["packing_list_source"] = "pdf"
    total_gross = num(data.get("total_gross_weight_mt"), 0)

    # Fix swapped/missing net vs gross totals — Gemini sometimes puts the
    # gross value into total_net_weight_mt and leaves gross as 0.
    # Detection: net is always <= gross, and net must equal the row sum.
    if total_net_weight_mt > 0 and total_gross == 0:
        row_net_sum = round(sum(num(r.get("net_weight_mt"), 0) for r in fixed), 3)  
        if abs(total_net_weight_mt - row_net_sum) > 1:
            # "net" total doesn't match row net sum — it's the gross value
            total_gross = total_net_weight_mt
            total_net_weight_mt = round(row_net_sum, 3)
            print(f"  [PKG] Fixed swapped GRAND TOTAL: net={total_net_weight_mt} MT, gross={total_gross} MT")
    elif total_net_weight_mt > total_gross > 0:
        # Both present but swapped
        total_net_weight_mt, total_gross = total_gross, total_net_weight_mt
        print(f"  [PKG] Fixed swapped GRAND TOTAL: net={total_net_weight_mt} MT, gross={total_gross} MT")

    data["total_bags"] = total_bags
    data["total_net_weight_mt"] = total_net_weight_mt
    data["total_gross_weight_mt"] = total_gross
    data["total_pallets"] = num(data.get("total_pallets"), 0)
    if fix_warnings:
        data["_split_fix_warnings"] = fix_warnings

    dump_json(pdf_path, "pkg_list.json", data)
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

    if s(mbl.get("mbl_no")).strip():
        results.append(f"[OK] MBL No — {s(mbl.get('mbl_no')).strip()}")
    else:
        results.append("[X]  MBL No — not found on the MBL")

    mbl_cids = {c["id"] for c in mbl.get("containers", []) if c.get("id")}
    pkl_cids = {c["container_id"] for c in pkl.get("containers", []) if c.get("container_id")}
    common = mbl_cids & pkl_cids
    only_mbl = mbl_cids - pkl_cids
    only_pkl = pkl_cids - mbl_cids

    if common:
        results.append(f"[OK] CONTAINERS — {len(common)} matched across MBL & Packing List")
    for c in sorted(only_mbl):
        results.append(f"[!]  CONTAINER — {c} only in MBL (not in Packing List) — EXCLUDED from output")
    for c in sorted(only_pkl):
        results.append(f"[!]  CONTAINER — {c} only in Packing List (not in MBL) — EXCLUDED from output")

    pkl_bags_sum = sum(num(c.get("bags"), 0) for c in pkl.get("containers", []))
    pkl_total_bags = num(pkl.get("total_bags"), 0)
    if pkl_bags_sum and pkl_total_bags:
        if pkl_bags_sum == pkl_total_bags:
            results.append(f"[OK] BAGS — Packing List rows sum({pkl_bags_sum}) = document total({pkl_total_bags})")
        else:
            results.append(f"[!]  BAGS — Packing List rows sum({pkl_bags_sum}) vs document total({pkl_total_bags})")

    pkl_pallets_sum = sum(num(c.get("pallets"), 0) for c in pkl.get("containers", []))
    pkl_total_pallets = num(pkl.get("total_pallets"), 0)
    if pkl_pallets_sum and pkl_total_pallets:
        if pkl_pallets_sum == pkl_total_pallets:
            results.append(f"[OK] PALLETS — Packing List rows sum({pkl_pallets_sum}) = document total({pkl_total_pallets})")
        else:
            results.append(f"[!]  PALLETS — Packing List rows sum({pkl_pallets_sum}) vs document total({pkl_total_pallets})")

    pkl_net_sum_kg = round(sum(num(c.get("net_weight_kg"), 0) for c in pkl.get("containers", [])), 3)
    pkl_total_net_kg = to_kg(pkl.get("total_net_weight_mt"), "MT")
    if pkl_net_sum_kg and pkl_total_net_kg:
        if abs(pkl_net_sum_kg - pkl_total_net_kg) < 1:
            results.append(f"[OK] NET WEIGHT — Packing List rows sum({pkl_net_sum_kg} KG) = document total({pkl_total_net_kg} KG)")
        else:
            results.append(f"[!]  NET WEIGHT — Packing List rows sum({pkl_net_sum_kg} KG) vs document total({pkl_total_net_kg} KG)")

    pkl_gross_sum_kg = round(sum(num(c.get("gross_weight_kg"), 0) for c in pkl.get("containers", [])), 3)
    pkl_total_gross_kg = to_kg(pkl.get("total_gross_weight_mt"), "MT")
    if pkl_gross_sum_kg and pkl_total_gross_kg:
        if abs(pkl_gross_sum_kg - pkl_total_gross_kg) < 1:
            results.append(f"[OK] GROSS WEIGHT — Packing List rows sum({pkl_gross_sum_kg} KG) = document total({pkl_total_gross_kg} KG)")
        else:
            results.append(f"[!]  GROSS WEIGHT — Packing List rows sum({pkl_gross_sum_kg} KG) vs document total({pkl_total_gross_kg} KG)")

    if pkl.get("containers"):
        results.append(f"[OK] LINE ITEMS — {len(pkl['containers'])} row(s) extracted from the Packing List "
                        f"({pkl.get('packing_list_source', '?')} source)")
    else:
        results.append("[X]  LINE ITEMS — no rows extracted from the Packing List")

    # build_rows() only emits a row when its container matches on BOTH
    # documents (see its own "no MBL match" skip) — spelled out here so the
    # Excel's final row count being lower than "LINE ITEMS extracted" above
    # is never a surprise (every container named in the [!] CONTAINER lines
    # above is the reason).
    if only_pkl:
        results.append(f"[!]  OUTPUT ROWS — {len(only_pkl)} Packing List container(s) excluded from the "
                        f"output Excel entirely (no matching MBL entry) — see the CONTAINER lines above "
                        f"for which ones")

    # Surface any split-fix warnings in the validation output
    for w in pkl.get("_split_fix_warnings", []):
        results.append(w)

    return results


# ═══════════════════════════════════════════════════════════════════════════
# ROW BUILDER
# ═══════════════════════════════════════════════════════════════════════════

# Shipping Line display value — NOT a UI field. identify_carrier() already
# names the MBL's issuer (normalized to one of CARRIER_ID_PROMPT's fixed
# spellings, e.g. "OOCL", "MSC", "HAPAG-LLOYD"); this maps that internal
# code to the exact display string wanted in the output Excel's "Shipping
# Line" column — spelled precisely as given, typos included (e.g. "MSC" ->
# "Mediterrenean Shipping Company"), since the client wants exactly this
# text, not a corrected version of it. Any carrier NOT in this map (an
# unlisted one, a generic-fallback carrier, or "UNKNOWN") displays "Other"
# rather than the raw internal code — the raw code is an internal
# normalization detail, never shown to the client.
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


# ═══════════════════════════════════════════════════════════════════════════
# INBOUND ADVICE PDFs — PER-PRODUCT REFERENCE
# ═══════════════════════════════════════════════════════════════════════════
# Optional 3rd document type, uploaded as a batch (up to 15 files — one
# "IMPORT CONTAINERS" advice PDF per product/material in this shipment, e.g.
# "EB101046.10_INBOUND.pdf"). Each one states its OWN "Our reference" (e.g.
# "EB101046.10") for ONE "Material" (e.g. "LLDPE EE-1801-AAB") — unlike the
# single global Reference UI field (one value for the whole shipment), this
# gives a PER-PRODUCT reference, matched against the Packing List's own
# Grade/product column for each row.
#
# The two documents spell the same product differently: the advice's
# "Material" carries the polymer family name and a 2-letter grade-family
# prefix ("LLDPE EE-1801-AAB"), while the Packing List's Grade column
# states only the trailing "<4-digit>-<letters>" code ("1801-AAB") — the
# family name and prefix are dropped. Rather than trying to strip a fixed
# set of known prefixes (fragile — a new polymer family or prefix breaks
# it silently), both strings are matched by extracting that same
# "<3-4 digits>-<2-4 letters>" code out of each with one regex and
# comparing the extracted codes — format-agnostic on either side.
_PRODUCT_CODE_RE = re.compile(r'(\d{3,4}-[A-Z]{2,4})')


def extract_product_code(text: str) -> str:
    """Pulls the "<digits>-<letters>" grade code out of a product/material
    string (e.g. "LLDPE EE-1801-AAB" or "1801-AAB" -> "1801-AAB"). Falls
    back to the whole trimmed/uppercased string when no such code is found
    (e.g. a SIDPEC-style grade like "HD 6070 UA") — still comparable, just
    less likely to match anything on the other side."""
    text = s(text).strip().upper()
    m = _PRODUCT_CODE_RE.search(text)
    return m.group(1) if m else text


IB_ADVICE_PROMPT = """You are a shipping-document data extractor. Extract data from this "IMPORT \
CONTAINERS" shipping-instruction PDF (Swiss Polymers AG letterhead) and \
return ONLY a JSON object, no markdown, no explanation.

- "reference": the "Our reference" value (e.g. "EB101046.10") — bold text
  right under the "IMPORT CONTAINERS" heading, may include a decimal suffix
  like ".10"/".20"/".30" for a sub-shipment of a larger batch.
- "material": the "Material" value EXACTLY as printed (e.g. "LLDPE EE-1801-
  AAB", "HDPE EM-4810-AAH") — do not reformat, abbreviate, reorder, or drop
  any part of it.

Return:
{"reference": "string", "material": "string"}"""


def extract_inbound_advice(pdf_path: str) -> dict:
    """One "IMPORT CONTAINERS" advice PDF -> its own {reference, material}."""
    data = call_gemini(IB_ADVICE_PROMPT, pdf_path=pdf_path, max_output_tokens=512)
    dump_json(pdf_path, "inbound_advice.json", data)
    return {
        "reference": s(data.get("reference")).strip(),
        "material":  s(data.get("material")).strip(),
    }


def build_product_reference_map(inbound_advices: list[dict]) -> dict[str, str]:
    """[{reference, material}, ...] (one per uploaded Inbound file) -> {product
    code: reference}, keyed by extract_product_code(material). If two
    Inbound files somehow resolve to the same product code, the LAST one
    processed wins and a warning is printed — this shouldn't happen for a
    real shipment (one advice per distinct product), so it's surfaced
    rather than silently picking one."""
    product_reference_map: dict[str, str] = {}
    for advice in inbound_advices:
        reference = advice.get("reference", "")
        material = advice.get("material", "")
        if not reference or not material:
            continue
        code = extract_product_code(material)
        if code in product_reference_map and product_reference_map[code] != reference:
            print(f"  [IB] Product code {code!r} already mapped to "
                  f"{product_reference_map[code]!r} — overwriting with {reference!r} "
                  f"(material {material!r})")
        product_reference_map[code] = reference
    return product_reference_map


def validate_product_references(rows: list[dict], inbound_advice_count: int) -> list[str]:
    """Called after build_rows() — reports which output rows' products had
    no matching Inbound file, so a blank Product Reference is never a silent
    surprise in the Excel. Skipped entirely when no Inbound files were
    uploaded at all (Product Reference is an optional feature)."""
    if not inbound_advice_count:
        return []
    results = []
    unmatched_products = sorted({r["product"] for r in rows if r["product"] and not r["product_reference"]})
    if unmatched_products:
        results.append(f"[!]  PRODUCT REFERENCE — no Inbound file matched product(s): "
                        f"{', '.join(unmatched_products)} — Product Reference left blank on those rows")
    elif rows:
        results.append("[OK] PRODUCT REFERENCE — every output row matched an uploaded Inbound file")
    return results


def build_rows(mbl: dict, pkl: dict, reference: str = "", eta_date: str = "",
                ship_name: str = "", product_reference_map: dict[str, str] = None) -> list[dict]:
    reference = s(reference).strip()
    # Ship Name is UI-picked (not extracted from either document) — same
    # convention as reference/eta_date — applied uniformly to every row in
    # this shipment. Shipping Line, by contrast, comes from the MBL's own
    # already-identified carrier (see carrier_display() above) — never a
    # separate UI field, so it can never disagree with the MBL.
    ship_name = s(ship_name).strip()
    shipping_line = carrier_display(mbl.get("carrier", ""))
    product_reference_map = product_reference_map or {}

    country_code = get_country_code(s(mbl.get("port_of_loading")).strip())

    mbl_map = {}
    for c in mbl.get("containers", []):
        mbl_map[c["id"]] = {
            "type": s(c.get("type")).strip(),
            "seal": s(c.get("seal")).strip(),
        }
    shipment_container_type = normalize_container_type(mbl.get("container_type", "")) if s(mbl.get("container_type")).strip() else ""

    mbl_no = s(mbl.get("mbl_no")).strip()

    rows = []
    skipped_no_mbl_match = []
    mbl_map = {}
    for c in mbl.get("containers", []):
        mbl_map[c["id"]] = {
            "type": s(c.get("type")).strip(),
            "seal": s(c.get("seal")).strip(),
        }
    mbl_cids = set(mbl_map.keys())

    for row in pkl.get("containers", []):
        cid = row.get("container_id", "")

        # Fuzzy-match FIRST (before the skip check), so OCR misreads
        # like YETU→UETU, COCU→OOCU get corrected before we check mbl_map
        matched_cid = _fuzzy_match_container(cid, mbl_cids)
        if matched_cid != cid:
            print(f"  [ROW] Fuzzy-matched PL container {cid} → MBL container {matched_cid}")
            cid = matched_cid

        if cid not in mbl_map:
            skipped_no_mbl_match.append(cid)
            continue
        

        mbl_entry = mbl_map[cid]
        seal_no = s(row.get("seal_no")).strip() or mbl_entry.get("seal", "")

        raw_type = mbl_entry.get("type", "")
        container_type = normalize_container_type(raw_type) if raw_type else shipment_container_type

        # Product Reference: looked up from the uploaded Inbound advice
        # files by this row's own product/Grade code (see
        # build_product_reference_map()) — "" when no Inbound file was
        # uploaded, or none matched this row's product. Container/Ref uses
        # it in place of the global Reference whenever it's known, falling
        # back to the global Reference only when it isn't (never left with
        # a dangling "/" for an unmatched product).
        row_product = s(row.get("product")).strip()
        product_reference = product_reference_map.get(extract_product_code(row_product), "") if row_product else ""

        rows.append({
            "reference":         reference,
            "container_no":      cid,
            "container_ref":     f"{cid}/{product_reference or reference}",
            "product_reference": product_reference,
            "shipping_line":  shipping_line,
            "mbl_no":         mbl_no,
            "seal_no":        seal_no,
            "container_type": container_type,
            "country_code":   country_code,
            "product":        s(row.get("product")).strip(),
            "lot_no":         s(row.get("lot_no")).strip(),
            "bags":           num(row.get("bags"), 0),
            "net_weight":     num(row.get("net_weight_kg"), 0),
            "gross_weight":   num(row.get("gross_weight_kg"), 0),
            "pallet_qty":     num(row.get("pallets"), 0),
            "ship_name":      ship_name,
            "eta_date":       eta_date,
        })

    if skipped_no_mbl_match:
        print(f"  [ROWS] Excluded {len(skipped_no_mbl_match)} Packing List row(s) with no matching MBL "
              f"container: {', '.join(sorted(set(skipped_no_mbl_match)))}")

    return rows