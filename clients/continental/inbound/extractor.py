"""
Continental Inbound (GS Caltex Corporation -> Continental Industries Group)
— Extraction, validation, and row-building.

Only TWO source documents (no Invoice, unlike Vinmar): MBL + Packing List,
plus two UI-picked fields (Reference, ETA Date) that are NOT extracted from
any document — GS Caltex's paperwork carries no PO/reference number
anywhere, so Reference is always a manual entry, applied uniformly to every
row (same convention as Vinmar/Emvia's UI-picked fields).

Unlike Vinmar/Emvia, this client's Packing List already states everything
per-container that those two need help from the MBL for: Product (its
"GRADE NAME" column), Lot No, Bags, Net Weight, Gross Weight, and Seal No
are all printed directly in the "BREAKDOWN FOR CONTAINER" table. So the MBL
here is needed for far less: just the MBL number (for the "Mbl No" column),
Port of Loading (for Country Code), and — only as a FALLBACK when the
Packing List's own Seal No is blank — each container's Seal/Type. The MBL's
layout changes per ocean carrier exactly like Vinmar/Emvia, so MBL
extraction is the same two-call pipeline:

  1. identify_carrier()  — tiny call, just names the carrier on this MBL.
  2. extract_mbl()        — dispatches to that carrier's own tuned prompt
                             (CARRIER_MBL_PROMPTS), falling back to a generic
                             prompt for any carrier not yet onboarded.

The carrier-specific prompts below are trimmed-down descendants of the ones
already proven in clients/vinmar/inbound/extractor.py and
clients/emvia/inbound/extractor.py — same container-table detection logic
per carrier (id/seal/type), with every field this client doesn't need
(ref_nos, product, grade, pallets, bags, gross weight, tare, measurement)
stripped out, since none of that is asked of the MBL here.

Pallet Count is NEVER asked of Gemini — it's a straight code-side division
(bags ÷ "NUMBER OF BAGS PER PALLET" — a number Gemini only has to *copy*,
not compute), so a hallucinated total can never propagate into an output
row. See build_rows()'s pallet_count line.

Country-code lookup, container-type normalization, and MT/KG conversion are
shared with Sabic/Vinmar/Emvia Inbound via helpers/doc_common.py.
"""

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
# Every prompt below only asks for what this client's output columns need:
# "mbl_no", "port_of_loading", a shipment-wide "container_type" fallback,
# and per-container "id"/"seal"/"type" — no ref_nos, product, grade,
# pallets, bags, or weight, unlike Vinmar/Emvia's MBL prompts (this client's
# Packing List already states all of those directly, see PKG_LIST_PROMPT
# below).

RETURN_SCHEMA = """
Return:
{
  "mbl_no": "string", "port_of_loading": "string",
  "container_type": "string",
  "containers": [
    {"id": "string", "seal": "string", "type": "string"}
  ]
}"""


# CMA CGM prints (at least) two structurally different templates — carrier
# identification alone can't tell them apart, so this ONE prompt self-detects
# which one it's looking at first.
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


# Yang Ming Bill of Lading: container rows are printed as flat description-
# cell lines, both on the main page AND on a following "ATTACHED LIST"
# continuation page — the continuation page's containers belong to this
# SAME shipment.
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


# HMM Sea Waybill / Bill of Lading: a compact single-line-per-container
# format with NO per-container weight/bag breakdown at all — only a
# shipment-wide container size/type statement. The container row's own
# terse ISO type code (e.g. "DC 4H") must NOT be returned as-is — "DC" means
# Dry Container (standard, never High Cube) regardless of any digit+letter
# code glued after it; the actual SIZE comes from a separate "Total Number
# of Containers (in words)" line (e.g. "11 X 40'H DC CONTAINERS" -> "40FT").
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


# ONE / Ocean Network Express Bill of Lading: every container row already
# carries its own type — no shipment-wide fallback needed.
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


# OOCL Sea Waybill: the FIRST page's "description of goods" row is often a
# shipment-wide summary under a non-container-shaped code, not a real
# container — the actual per-container table is on a later "attached list"
# page.
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


# Maersk: this carrier prints TWO structurally different layouts — a table
# right on the front page (seal glued to the container id, no separate
# label) OR a front page with NO container ids at all (just a shipment-wide
# summary) whose real table is on a later continuation page (seal on its
# own "Shipper Seal :" line). Self-detect which one this document is.
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


# Grimaldi Deep Sea Combined Transport Bill of Lading: the "Marks and Nos"
# column repeats a per-container id/seal block, separate from a shipment-
# wide free-text description that states the container size once.
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


# LX Pantos "BILL OF LADING" — a FIATA Multimodal Transport Bill of Lading
# (FBL) issued by a freight forwarder. Its single most distinctive trait:
# MULTIPLE containers' id/seal/weight/measurement/bags are packed into ONE
# compact column as a repeating two-line pattern, not a normal
# one-row-per-container table.
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


# Hapag-Lloyd "Multimodal Transport or Port to Port Shipment" Bill of Lading.
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


# MSC "Sea Waybill" — main page just refers to an attached "RIDER PAGE" for
# the actual container/goods details.
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


# Borchard Lines "Bill of Lading" — the "Marks and Nos; Container No:"
# column lists every container with its own type + seal.
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


# Fallback for any carrier not yet onboarded (COSCO / EVERGREEN / ZIM / PIL /
# etc. will each get their own tuned prompt as those samples come in) — same
# field shape as every carrier-specific prompt so build_rows() never needs
# to know which prompt actually ran.
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
# PACKING LIST PROMPT — TWO known layouts, self-detected
# ═══════════════════════════════════════════════════════════════════════════
# LAYOUT A (e.g. GS Caltex Corporation): a two-part PDF — page 1 is the
# "PACKING LIST" header, a separate page is the "BREAKDOWN FOR CONTAINER"
# table, which states Seal No. and Lot No. per container but only a flat
# "NUMBER OF BAGS PER PALLET" figure, not a per-container pallet count —
# Pallet Count is computed in code from that figure (see build_rows()).
#
# LAYOUT B (e.g. Egyptian Propylene & Polypropylene Company / "EPP"): ONE
# single-page table with a "No. of Pallets" column stated directly per
# container — no code-side division needed or wanted here, and this layout
# has NO Seal Number column and NO Lot Number column anywhere at all.
#
# Whichever layout is detected, "seal_no" and "lot_no" must be left "" for
# any row where that information genuinely isn't printed anywhere on the
# document — never invented, guessed, or filled in from an unrelated field
# (e.g. an EPP/invoice reference number is NOT a lot number).

PKG_LIST_PROMPT = """You are a shipping-document data extractor. Extract all data from this \
Packing List PDF and return ONLY a JSON object, no markdown, no explanation.

This document uses ONE of two layouts. Identify which one FIRST from the
detection cues below, then apply ONLY that layout's rules.

══════════════════════════════════════════
LAYOUT A — "BREAKDOWN FOR CONTAINER" two-part style (e.g. GS Caltex
Corporation)
══════════════════════════════════════════
How to detect: a "PACKING LIST" page with numbered boxes like "(8) No. &
date of invoice", followed by a SEPARATE page titled "BREAKDOWN FOR
CONTAINER" with columns CNTR NO. | SEAL NO. | GRADE NAME | LOT NO. | NET
WEIGHTS (MT) | GROSS WEIGHTS (MT) | EACH PACKAGE (BAGS), and a "NUMBER OF
BAGS PER PALLET:" line below that table.

- "product": the goods description from box (13), e.g. from "48.00 MT
  POLYPROPYLENE H710" extract "POLYPROPYLENE" (drop the leading quantity —
  a cross-check value only, the authoritative per-container product name
  comes from the "GRADE NAME" column below).
- "total_bags": the total "(14) Quantity/Unit" bag count printed as
  "N BAGS", e.g. from "1,920 BAGS" extract 1920 — a cross-check value only.
- "total_net_weight_mt": the shipment TOTAL row's net-weight figure, in MT.
- "total_gross_weight_mt": the shipment TOTAL row's gross-weight figure, in
  MT.
- "total_pallets": leave 0 — this layout has no shipment-wide pallet total
  anywhere.
- "bags_per_pallet": the integer in the "NUMBER OF BAGS PER PALLET:" line
  on the "BREAKDOWN FOR CONTAINER" page (e.g. "40BAGS" -> 40). This is a
  PLAIN NUMBER printed on the document — copy it exactly as printed, do NOT
  compute or derive it yourself from any other figure (pallet counts per
  container are calculated in code from this number, never by you).
- "net_weight_per_bag_kg": the "NET WEIGHT OF EACH BAG:" value in KG (e.g.
  "25KG" -> 25), if printed. 0 if not.
- "gross_weight_per_bag_kg": the "GROSS WEIGHT OF EACH BAG:" value in KG,
  if printed. 0 if not.
- "containers" (the "BREAKDOWN FOR CONTAINER" table, one entry per real
  container row — exclude the "TOTAL" row at the bottom, that's a
  cross-check only, never a container of its own):
  - "container_id": the "CNTR NO." column value (4 letters + 7 digits).
  - "seal_no": the "SEAL NO." column value.
  - "product": the "GRADE NAME" column value for this row (e.g. "H710").
  - "lot_no": the "LOT NO." column value.
  - "net_weight_mt": the "NET WEIGHTS (MT)" column value for this row.
  - "gross_weight_mt": the "GROSS WEIGHTS (MT)" column value for this row.
  - "bags": the "EACH PACKAGE (BAGS)" column value for this row (integer).
  - "container_pallets": leave 0 for every row — this layout has no
    per-container pallet column, only the flat "bags_per_pallet" figure
    above.

══════════════════════════════════════════
LAYOUT B — single-page grade/pallet table (e.g. Egyptian Propylene &
Polypropylene Company / "EPP")
══════════════════════════════════════════
How to detect: ONE table, on the same page as the "Description of goods:"
line, with columns Container Number | Grade | No. of Pallets | No. of Bags
| Net Weight (MT) | Gross Weight (MT), followed by a totals row (blank
first two cells) and separate "Total Pallets:" / "Total Bags:" / "Net
Weight:" / "Gross Weight:" lines. ⚠️ This layout has NO Seal Number column
and NO Lot Number column ANYWHERE on the document.

- "product": from the "Description of goods:" line, e.g. "48 MT
  Polypropylene Homopolymer FM525J" -> "Polypropylene Homopolymer FM525J"
  (drop the leading quantity) — a cross-check value only, the authoritative
  per-container product/grade comes from the "Grade" column below.
- "total_bags": the "Total Bags:" value.
- "total_net_weight_mt": the "Net Weight:" value (already MT).
- "total_gross_weight_mt": the "Gross Weight:" value (already MT).
- "total_pallets": the "Total Pallets:" value.
- "bags_per_pallet": leave 0 — not stated on this layout (this layout gives
  pallets directly per container instead, see "container_pallets" below —
  never compute one from the other).
- "net_weight_per_bag_kg" / "gross_weight_per_bag_kg": leave 0 — not
  printed on this layout.
- "containers" (the one table on this page, one entry per real container
  row — exclude the totals row, that's a cross-check only, never a
  container of its own):
  - "container_id": the "Container Number" column value.
  - "seal_no": leave "" — this layout never prints a seal number anywhere,
    do not guess or invent one.
  - "product": the "Grade" column value for this row (e.g. "FM525J").
  - "lot_no": leave "" — this layout never prints a lot number anywhere, do
    not guess one and do NOT substitute an unrelated number (e.g. the "EPP
    ref.number" / invoice number is NOT a lot number, leave this "" even
    though a number is visible elsewhere on the page).
  - "net_weight_mt": the "Net Weight (MT)" column value for this row.
  - "gross_weight_mt": the "Gross Weight (MT)" column value for this row.
  - "bags": the "No. of Bags" column value for this row (integer).
  - "container_pallets": the "No. of Pallets" column value for this row — a
    REAL printed number, copy it exactly as printed; this IS the
    authoritative pallet count for this row on this layout (no bags-per-
    pallet division applies here, the column already gives the answer
    directly).

Return:
{
  "product": "string",
  "total_bags": 0,
  "total_net_weight_mt": 0,
  "total_gross_weight_mt": 0,
  "total_pallets": 0,
  "net_weight_per_bag_kg": 0,
  "gross_weight_per_bag_kg": 0,
  "bags_per_pallet": 0,
  "containers": [
    {"container_id": "string", "seal_no": "string", "product": "string",
     "lot_no": "string", "net_weight_mt": 0, "gross_weight_mt": 0, "bags": 0,
     "container_pallets": 0}
  ]
}"""


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


def extract_packing_list(pdf_path: str) -> dict:
    data = call_gemini(PKG_LIST_PROMPT, pdf_path=pdf_path, max_output_tokens=16384)
    dump_json(pdf_path, "pkg_list_raw.json", data)

    containers = []
    for row in data.get("containers", []):
        cid, seal = fix_container_id(row.get("container_id", ""), row.get("seal_no", ""))
        containers.append({
            "container_id":   cid,
            "seal_no":        s(seal).strip(),
            "product":        s(row.get("product")).strip(),
            "lot_no":         s(row.get("lot_no")).strip(),
            "net_weight_mt":  num(row.get("net_weight_mt"), 0),
            "gross_weight_mt": num(row.get("gross_weight_mt"), 0),
            "bags":           num(row.get("bags"), 0),
            "container_pallets": num(row.get("container_pallets"), 0),
        })
    data["containers"] = containers

    dump_json(pdf_path, "pkg_list.json", data)
    return data


# ═══════════════════════════════════════════════════════════════════════════
# CROSS-DOCUMENT VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

def validate(mbl: dict, pkl: dict) -> list[str]:
    results = []

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

    pkl_bags_sum = sum(num(c.get("bags"), 0) for c in pkl.get("containers", []))
    pkl_total_bags = num(pkl.get("total_bags"), 0)
    if pkl_bags_sum and pkl_total_bags:
        if pkl_bags_sum == pkl_total_bags:
            results.append(f"[OK] BAGS — Packing List rows sum({pkl_bags_sum}) = document total({pkl_total_bags})")
        else:
            results.append(f"[!]  BAGS — Packing List rows sum({pkl_bags_sum}) vs document total({pkl_total_bags})")

    pkl_net_sum_mt = round(sum(num(c.get("net_weight_mt"), 0) for c in pkl.get("containers", [])), 4)
    pkl_total_net_mt = num(pkl.get("total_net_weight_mt"), 0)
    if pkl_net_sum_mt and pkl_total_net_mt:
        if abs(pkl_net_sum_mt - pkl_total_net_mt) < 0.01:
            results.append(f"[OK] NET WEIGHT — Packing List rows sum({pkl_net_sum_mt} MT) = document total({pkl_total_net_mt} MT)")
        else:
            results.append(f"[!]  NET WEIGHT — Packing List rows sum({pkl_net_sum_mt} MT) vs document total({pkl_total_net_mt} MT)")

    pkl_gross_sum_mt = round(sum(num(c.get("gross_weight_mt"), 0) for c in pkl.get("containers", [])), 4)
    pkl_total_gross_mt = num(pkl.get("total_gross_weight_mt"), 0)
    if pkl_gross_sum_mt and pkl_total_gross_mt:
        if abs(pkl_gross_sum_mt - pkl_total_gross_mt) < 0.01:
            results.append(f"[OK] GROSS WEIGHT — Packing List rows sum({pkl_gross_sum_mt} MT) = document total({pkl_total_gross_mt} MT)")
        else:
            results.append(f"[!]  GROSS WEIGHT — Packing List rows sum({pkl_gross_sum_mt} MT) vs document total({pkl_total_gross_mt} MT)")

    # Pallet Count comes from ONE of two sources depending on which layout
    # was detected (see PKG_LIST_PROMPT) — either a per-row "container_pallets"
    # figure (Layout B, e.g. EPP) or a flat "bags_per_pallet" divisor applied
    # in code (Layout A, e.g. GS Caltex). Only warn if NEITHER is present.
    pkl_containers = pkl.get("containers", [])
    has_row_pallets = any(num(c.get("container_pallets"), 0) for c in pkl_containers)
    if not has_row_pallets and not pkl.get("bags_per_pallet"):
        results.append("[!]  PALLET — Packing List gave neither a per-container pallet count nor a "
                        "\"bags per pallet\" figure; Pallet column will be 0 for every row")

    if has_row_pallets:
        pkl_pallets_sum = sum(num(c.get("container_pallets"), 0) for c in pkl_containers)
        pkl_total_pallets = num(pkl.get("total_pallets"), 0)
        if pkl_pallets_sum and pkl_total_pallets:
            if pkl_pallets_sum == pkl_total_pallets:
                results.append(f"[OK] PALLETS — Packing List rows sum({pkl_pallets_sum}) = document total({pkl_total_pallets})")
            else:
                results.append(f"[!]  PALLETS — Packing List rows sum({pkl_pallets_sum}) vs document total({pkl_total_pallets})")

    return results


# ═══════════════════════════════════════════════════════════════════════════
# ROW BUILDER
# ═══════════════════════════════════════════════════════════════════════════

def build_rows(mbl: dict, pkl: dict, reference: str = "", eta_date: str = "") -> list[dict]:
    mbl_map = {}
    for c in mbl.get("containers", []):
        mbl_map[c["id"]] = {
            "type": s(c.get("type")).strip(),
            "seal": s(c.get("seal")).strip(),
        }

    mbl_no = s(mbl.get("mbl_no")).strip()
    country_code = get_country_code(mbl.get("port_of_loading", ""))

    # Some carriers (e.g. HMM) print one container type for the WHOLE
    # shipment instead of repeating it per container.
    shipment_container_type = (
        normalize_container_type(mbl.get("container_type", ""))
        if s(mbl.get("container_type")).strip() else ""
    )

    # Pallet Count — NEVER asked of Gemini to compute. Two layouts, two
    # sources (see PKG_LIST_PROMPT):
    #   - Layout B (e.g. EPP): each row already carries its own printed
    #     "container_pallets" figure — a direct copy, not a calculation —
    #     used as-is, preferred whenever present.
    #   - Layout A (e.g. GS Caltex): no per-row figure exists, only a flat
    #     "bags_per_pallet" divisor — Pallet Count is then bags ÷ that
    #     divisor, a straight code-side division.
    bags_per_pallet = num(pkl.get("bags_per_pallet"), 0)

    def _build_row(cid: str, seal_no: str, product: str, lot_no: str, net_weight_mt, gross_weight_mt, bags,
                   row_container_pallets=0) -> dict:
        mbl_entry = mbl_map.get(cid, {})

        # Packing List wins for Seal (GS Caltex's own certified breakdown
        # table is the authoritative per-container source here, unlike
        # Vinmar/Emvia where the ocean carrier's MBL is treated as
        # authoritative) — MBL is only a fallback for a blank row.
        seal = s(seal_no).strip() or mbl_entry.get("seal", "")

        raw_type = mbl_entry.get("type", "")
        container_type = normalize_container_type(raw_type) if raw_type else shipment_container_type

        bags = num(bags, 0)
        row_container_pallets = num(row_container_pallets, 0)
        if row_container_pallets:
            pallet_count = row_container_pallets
        elif bags_per_pallet:
            pallet_count = round(bags / bags_per_pallet)
        else:
            pallet_count = 0

        return {
            "reference":      reference,
            "container_no":   cid,
            "container_ref":  f"{cid}/{reference}",
            "mbl_no":         mbl_no,
            "seal_no":        seal,
            "container_type": container_type,
            "country_code":   country_code,
            "product":        s(product).strip(),
            "lot_no":         s(lot_no).strip(),
            "pallet_count":   pallet_count,
            "bags_count":     bags,
            "net_weight":     to_kg(net_weight_mt, "MT"),
            "gross_weight":   to_kg(gross_weight_mt, "MT"),
            "eta_date":       eta_date,
        }

    rows = [
        _build_row(c["container_id"], c["seal_no"], c["product"], c["lot_no"],
                   c["net_weight_mt"], c["gross_weight_mt"], c["bags"], c["container_pallets"])
        for c in pkl.get("containers", [])
    ]

    # Containers that exist ONLY on the MBL, with no Packing List row at all
    # — one row each, everything else left at its default.
    pkl_cids = {c["container_id"] for c in pkl.get("containers", [])}
    for cid in mbl_map:
        if cid not in pkl_cids:
            rows.append(_build_row(cid, "", "", "", 0, 0, 0))

    return rows
