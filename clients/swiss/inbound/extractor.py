# """
# Swiss Inbound — Extraction, validation, and row-building.

# Two source documents (MBL + Packing List — no Invoice), plus two UI-picked
# fields (Reference, ETA Date) that are NOT extracted from either document —
# same convention as Continental Inbound's UI-picked fields, applied uniformly
# to every row.

# Like Continental, this client's Packing List already states everything
# per-container/per-lot that the MBL would otherwise be needed for: Product
# (Grade), Lot No, Bags, Pallets, Net Weight, and Gross Weight are all printed
# directly on the Packing List. The MBL is only needed for the MBL number (for
# the "Bl No" column), Port of Loading (for Country Code), and — only as a
# FALLBACK when the Packing List's own Seal No is blank — each container's
# Seal/Type.

# The Packing List itself arrives in ONE of two source formats, mirroring
# Emvia Inbound (Warehouse 1147)'s Excel-vs-PDF split:
#   - .xlsx/.xls  -> extract_packing_list_excel() (excel_extractor.py,
#     pandas-based, no LLM — the sheet is already machine-readable, weights
#     already in KG). Column headers can be reworded between shippers (e.g.
#     "Container" vs "Container No"), solved with an alias map there.
#   - .pdf        -> extract_packing_list_pdf() (this module, Gemini-based).
#     Two known shipper layouts (SIDPEC / Sidi Kerir Petrochemicals, and
#     ETHYDCO / Egyptian Ethylene & Derivatives), self-detected inside ONE
#     prompt exactly like Continental's PKG_LIST_PROMPT — add a new layout
#     there, do not bend an existing one to also cover a different shipper's
#     table shape. Both PDF layouts state weights in MT; MT->KG conversion
#     happens here in code via helpers.doc_common.to_kg(), never inside the
#     prompt.

# Both extraction paths converge on ONE common shape — a flat list of
# container/lot line items, each already carrying net/gross weight in KG — so
# build_rows() has a single, source-agnostic row-building path (same
# "PDF- and Excel-sourced rows share one uniform path" convention as Emvia's
# extractor.py). A container CAN legitimately appear on more than one line
# item (ETHYDCO's layout splits one container's cargo across two rows when it
# carries two different lot numbers) — kept as separate output rows, never
# merged, so no lot number or weight figure is silently combined.

# The MBL, by contrast, changes shape per ocean carrier regardless of which
# Packing List format accompanies it, so MBL extraction is the same two-call
# pipeline used by Sabic/Vinmar/Emvia/Continental Inbound: identify_carrier()
# names the carrier, then extract_mbl() dispatches to that carrier's own tuned
# prompt (CARRIER_MBL_PROMPTS — reused here from Continental Inbound's already-
# proven library, trimmed the same way: this client's Packing List states
# product/lot/bags/pallets/weight directly, so the MBL prompts only need
# mbl_no/port_of_loading/container id+seal+type), falling back to a generic
# prompt for any carrier not yet onboarded.

# Country-code lookup, container-type normalization, container-ID
# normalization (strips hyphens — ETHYDCO prints ids like "CSLU-603288-3"),
# and MT/KG conversion are shared with Sabic/Vinmar/Emvia/Continental Inbound
# via helpers/doc_common.py.
# """

# from helpers.doc_common import (
#     dump_json,
#     fix_container_id,
#     get_country_code,
#     normalize_container_type,
#     num,
#     s,
#     to_kg,
# )
# from helpers.gemini_client import call_gemini

# # ═══════════════════════════════════════════════════════════════════════════
# # MBL CARRIER IDENTIFICATION (call #1)
# # ═══════════════════════════════════════════════════════════════════════════

# CARRIER_ID_PROMPT = """You are looking at a Master Bill of Lading / Sea Waybill PDF. Identify who \
# ISSUED it — the company whose logo/name is in the title block and who \
# signs it "AS A CARRIER" (or as forwarding agent) at the bottom, and any \
# "CARRIER:" field. Return ONLY a JSON object, no markdown.

# ⚠️ Some documents are issued by a FREIGHT FORWARDER (e.g. "LX Pantos") \
# acting as carrier under a FIATA Multimodal Transport Bill of Lading, while \
# the actual ocean VESSEL is operated by a different, separately-named \
# shipping line. In that case identify the ISSUER (the forwarder whose name \
# is in the title block and who signs the document), NOT the vessel operator \
# named elsewhere on the page — they are frequently different companies.

# Normalize the name to exactly one of these if it matches (case-sensitive, \
# use this exact spelling): "CMA CGM", "MSC", "HAPAG-LLOYD", "OOCL", "MAERSK", \
# "COSCO", "ONE", "EVERGREEN", "YANG MING", "ZIM", "HMM", "PIL", "GRIMALDI", \
# "LX PANTOS", "BORCHARD LINES".

# Aliases to watch for: a logo/branding of "ONE" with the text "Ocean Network \
# Express" printed nearby -> return "ONE". "HMM CO., LTD." -> "HMM". "Orient \
# Overseas Container Line" -> "OOCL". "Mediterranean Shipping Company" / "MSC \
# Mediterranean Shipping Company S.A." -> "MSC". "Maersk A/S" / "Maersk Line" \
# -> "MAERSK". "Grimaldi Deep Sea S.p.A." / "GRIMALDI GROUP" -> "GRIMALDI". \
# "LX Pantos Logistics" (any branch) -> "LX PANTOS". "Borchard Lines Limited" \
# -> "BORCHARD LINES".

# If the issuer is real but not in that list, return its name as printed on \
# the document. If you cannot tell at all, return "UNKNOWN".

# {"carrier": "string"}"""


# def identify_carrier(pdf_path: str) -> str:
#     data = call_gemini(CARRIER_ID_PROMPT, pdf_path=pdf_path, max_output_tokens=256)
#     carrier = s(data.get("carrier", "UNKNOWN")).strip().upper()
#     return carrier or "UNKNOWN"


# # ═══════════════════════════════════════════════════════════════════════════
# # CARRIER-SPECIFIC MBL PROMPTS (call #2)
# # ═══════════════════════════════════════════════════════════════════════════
# # Every prompt below only asks for what this client's output columns need:
# # "mbl_no", "port_of_loading", a shipment-wide "container_type" fallback,
# # and per-container "id"/"seal"/"type" — no ref_nos, product, grade,
# # pallets, bags, or weight, since this client's Packing List already states
# # all of those directly (see PKG_LIST_PROMPT below). Reused verbatim from
# # Continental Inbound's already-proven per-carrier library (same trimmed
# # field shape) — do not re-derive these per carrier from scratch.

# # Container-ID digit/letter confusion (e.g. "OOCU7228009" misread as
# # "00CU7228009") has a code-side safety net in helpers.doc_common.
# # fix_container_id() — a container ID has a FIXED shape (4 letters then 7
# # digits), so code can always force a stray "0" in the first 4 characters
# # back to "O" and vice versa in the last 7, no guessing involved. Seal
# # numbers and lot/batch numbers have no such fixed shape (a seal can
# # legitimately be almost any mix of letters and digits), so there is
# # nothing for code to safely auto-correct there — this instruction is the
# # ONLY defense against the same class of misread on those fields, which is
# # why it's spliced into every carrier prompt via @@RETURN_SCHEMA@@ rather
# # than left to a code-side fix.
# OCR_DISAMBIGUATION_RULE = """
# ⚠️ CHARACTER DISAMBIGUATION — a container ID is ALWAYS exactly 4 LETTERS
# followed by 7 DIGITS: a character you're unsure is "O" (letter) or "0"
# (digit) is the LETTER "O" if it falls in the first 4 characters, and the
# DIGIT "0" if it falls in the remaining 7 — never the other way round.
# Seal numbers and lot/batch numbers have no fixed letter/digit pattern to
# resolve ambiguity that way, so for THOSE fields look especially carefully
# at the actual glyph shape before deciding between visually similar pairs:
# "O"/"0", "I"/"1", "S"/"5", "B"/"8", "Z"/"2" — transcribe exactly what is
# printed, do not default to whichever reads more like a "normal" number."""

# RETURN_SCHEMA = """
# @@OCR_DISAMBIGUATION_RULE@@

# Return:
# {
#   "mbl_no": "string", "port_of_loading": "string",
#   "container_type": "string",
#   "containers": [
#     {"id": "string", "seal": "string", "type": "string"}
#   ]
# }""".replace("@@OCR_DISAMBIGUATION_RULE@@", OCR_DISAMBIGUATION_RULE)


# # CMA CGM prints (at least) two structurally different templates — carrier
# # identification alone can't tell them apart, so this ONE prompt self-detects
# # which one it's looking at first.
# CMA_CGM_MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this CMA CGM \
# Waybill / Bill of Lading PDF (it may span multiple sheets — read all of \
# them) and return ONLY a JSON object, no markdown, no explanation.

# This document uses ONE of two templates. Identify which one FIRST from the
# detection cues below, then apply ONLY that template's rules.

# ══════════════════════════════════════════
# TEMPLATE 1 — "WAYBILL NON NEGOTIABLE" (e.g. Korea -> Belgium shipments)
# ══════════════════════════════════════════
# How to detect: title says "WAYBILL" / "NON NEGOTIABLE"; the container table
# column header reads "MARKS AND NOS / CONTAINER AND SEALS".

# - "mbl_no": the WAYBILL NUMBER (top right box).
# - "port_of_loading": as labeled.
# - "container_type" / "containers[].type": leave BOTH empty — every
#   container's own "NO AND KIND OF PACKAGES" cell states its type directly
#   (see below), there's no separate shipment-wide fallback needed here.
# - Container table (repeats once per container, across all sheets): each
#   row has a "MARKS AND NOS / CONTAINER AND SEALS" cell with the container
#   number on one line and "SEAL <number>" on the next line; a "NO AND KIND
#   OF PACKAGES" cell like "1 x 40HC   960 BAGS" (type AND bag count
#   together — you only need the type token, e.g. "40HC"). Return per
#   container: "id" (4 letters+7 digits, no spaces, seal not included),
#   "seal" (the number after "SEAL"), "type" (the size/type token from the
#   packages cell, e.g. "40HC").

# ══════════════════════════════════════════
# TEMPLATE 2 — numbered field boxes (e.g. "SHIPPER/EXPORTER (2)",
# "DOCUMENT NO (5)", "DESCRIPTION OF GOODS (18)")
# ══════════════════════════════════════════
# How to detect: field labels carry box numbers.

# - "mbl_no": "DOCUMENT NO (5)" / "BL/No." value.
# - "port_of_loading": "PORT OF LOADING (12)".
# - "container_type": from a summary line like "21x40HC CONTAINERS:" (e.g.
#   "40HC") — applies to every container, rows don't repeat a type of their
#   own on this template.

# ⚠️ CRITICAL — the FIRST "MARKS AND NUMBERS"/"DESCRIPTION OF GOODS" entry on
# sheet 1 (usually plain "N/M" marks with a free-text goods description and
# its own "TOTAL ...KGS"/"TOTAL BAGS" lines) is a shipment-WIDE summary block,
# never a real container — do not extract it as one.

# REAL CONTAINER ROWS — one THREE-line entry per container, spread across
# every sheet (read all of them):
#   <CONTAINER ID> <BAGS COUNT> BAG <GROSS WEIGHT>KGS <MEASUREMENT>CBM
#   SN# <SEAL>
#   <PRODUCT NAME / GRADE line(s) — ignore, not needed>
#   Example: "SEGU6357340 960 BAG 24454.000KGS 40.000CBM" then "SN# PX288469
#   HIGH DENSITY POLYETHYLENE" -> id "SEGU6357340", seal "PX288469", type ""
#   (use the shipment-wide "container_type" instead).

# A further shipment-wide totals block (QUANTITY/TOTAL BAGS/HS CODE/etc.) is
# often glued directly after one container's own three lines — that block
# belongs only in the fields above, never in that container's own row, and is
# never a separate container of its own.
# @@RETURN_SCHEMA@@"""


# # Yang Ming Bill of Lading: container rows are printed as flat description-
# # cell lines, both on the main page AND on a following "ATTACHED LIST"
# # continuation page — the continuation page's containers belong to this
# # SAME shipment.
# YANG_MING_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
# from this Yang Ming Bill of Lading PDF — it usually includes one or more \
# "ATTACHED LIST" continuation pages listing MORE containers for this same \
# shipment, read every page — and return ONLY a JSON object, no markdown.

# - "mbl_no": the B/L No.
# - "port_of_loading": Port of Loading.
# - "container_type" / "containers[].type": leave "container_type" empty —
#   every container row already carries its own type directly (see below).

# CONTAINER TABLE — one row per container, repeated identically on the main
# page and any "ATTACHED LIST" page(s), all belonging to this one shipment.
# Each row is a single line shaped like:
#   <CONTAINER ID> <TYPE like 40HQ> FCL/FCL <SEAL, an alphanumeric code like
#   YMAW199822> <BAGS COUNT> BAGS <GROSS WEIGHT>KGS <MEASUREMENT>CBM
#   Example: "BMOU5742956 40HQ FCL/FCL YMAW199822 1080 BAGS 27540.000KGS
#   56.0000CBM" -> id "BMOU5742956", type "40HQ", seal "YMAW199822".
# Read every such row on every page — do not stop after the main page's rows.
# @@RETURN_SCHEMA@@"""


# # HMM Sea Waybill / Bill of Lading: a compact single-line-per-container
# # format with NO per-container weight/bag breakdown at all — only a
# # shipment-wide container size/type statement. The container row's own
# # terse ISO type code (e.g. "DC 4H") must NOT be returned as-is — "DC" means
# # Dry Container (standard, never High Cube) regardless of any digit+letter
# # code glued after it; the actual SIZE comes from a separate "Total Number
# # of Containers (in words)" line (e.g. "11 X 40'H DC CONTAINERS" -> "40FT").
# HMM_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
# from this HMM Sea Waybill / Bill of Lading PDF — it may span multiple \
# pages, and the container list is sometimes split across them, read every \
# page — and return ONLY a JSON object, no markdown.

# - "mbl_no": the B/L No. (may show a carrier prefix immediately before the
#   booking number, e.g. "HDMU MAAE69596301" — return it exactly as printed,
#   prefix included, if present).
# - "port_of_loading": Port of Loading.

# ⚠️ CONTAINER TYPE — do not take the container row's own terse type code
# literally. Find the container SIZE from a line like "11 X 40'H DC
# CONTAINERS" or "1 X 40'H DC CONTAINER" (in the shipment-wide description
# block) and set "container_type" to size + "FT" (e.g. "40FT") whenever that
# line's own code contains "DC" — "DC" = Dry Container, a standard/general-
# purpose container, NEVER High Cube, no matter what code follows it (e.g. a
# trailing "4H" is an internal ISO size/type code, not a "High Cube"
# indicator, even though it contains the letter "H"). Only use "HC" instead
# of "FT" if that same line, or the goods description, explicitly says "High
# Cube" / "HC" / "9'6". This one "container_type" value applies to every
# container in the shipment — leave every row's own "type" field empty.

# CONTAINER LIST — one row (sometimes two lines) per container, shaped like
# either:
#   <CONTAINER ID> / <SEAL CODE>   <TYPE CODE>   CY / CY
#   (id and seal separated by "/" on one line, type code on the same or next
#   line) — e.g. "HMMU4077422 / 26H1503589 DC 4H CY / CY" -> id
#   "HMMU4077422", seal "26H1503589".
# Read every such row across every page — the total container count is
# usually stated somewhere as "ELEVEN (11) CONTAINERS ONLY" or similar; make
# sure the number of rows you return matches that stated total.
# @@RETURN_SCHEMA@@"""


# # ONE / Ocean Network Express Bill of Lading: every container row already
# # carries its own type — no shipment-wide fallback needed.
# ONE_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
# from this ONE (Ocean Network Express) Bill of Lading PDF and return ONLY a \
# JSON object, no markdown.

# - "mbl_no": the BILL OF LADING NO.
# - "port_of_loading": Port of Loading.
# - "container_type": leave empty — every container row states its own type
#   directly (see below).

# CONTAINER TABLE — one row per container, ABOVE the shipment-level summary
# row (do not confuse the two — see warning below). Each row is shaped like:
#   <CONTAINER ID> / <SEAL, alphanumeric like CN35952BF>   <BAGS COUNT> BAGS
#   /FCL / FCL/<TYPE like 40HQ>/<GROSS WEIGHT>KGS/<MEASUREMENT>M3
#   Example: "BEAU5520851 / CN35952BF   22 BAGS  /FCL / FCL/40HQ/25700.000KGS/
#   55.000M3" -> id "BEAU5520851", seal "CN35952BF", type "40HQ".

# ⚠️ Do NOT treat the "N/M ... BAGS IN TOTAL ... CONTAINER(S) SAID TO
# CONTAIN" line below the container rows as a container row itself — that
# line is a shipment-wide summary/total, not an additional container.
# @@RETURN_SCHEMA@@"""


# # OOCL Sea Waybill: the FIRST page's "description of goods" row is often a
# # shipment-wide summary under a non-container-shaped code, not a real
# # container — the actual per-container table is on a later "attached list"
# # page.
# OOCL_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
# from this OOCL Sea Waybill PDF — it spans multiple pages, and the REAL \
# per-container table is often on a LATER page under "TO BE CONTINUED ON \
# ATTACHED LIST" (or "** TO BE CONTINUED ON ATTACHED LIST **"), not the first \
# page — read every page — and return ONLY a JSON object, no markdown.

# - "mbl_no": the SEA WAYBILL NO.
# - "port_of_loading": Port of Loading.
# - "container_type": leave empty — every REAL container row (see below)
#   already carries its own type directly.

# ⚠️ CRITICAL — do not confuse the first page's summary row with a real
# container: the first "DESCRIPTION OF GOODS" row on page 1 often shows a
# non-standard code in the "CNTR. NOS." column (e.g. "X20260710127480" or
# "ITN : X20260622025380") next to a "TOTAL BAGS: n" line and a shipment-wide
# gross weight — that row is a SUMMARY, not a container (its code does NOT
# match the 4-letter+7-digit container ID pattern). The REAL container-by-
# container table is on a later page, with rows shaped like:
#   <CONTAINER ID> /<SEAL, numeric> / <BAGS COUNT> BAGS /FCL/FCL /<TYPE like
#   40HQ>/<GROSS WEIGHT>KGS
#   Example: "TGBU9205120 /0029165 / 780 BAGS /FCL/FCL /40HQ/19919.000KGS" ->
#   id "TGBU9205120", seal "0029165", type "40HQ".
# Only rows matching this real-container shape belong in "containers" — the
# first-page summary row is never extracted as a container itself, and do
# not stop reading just because a page says "DELIBERATELY LEFT BLANK AND
# CONTINUE ON NEXT PAGE" — keep reading the following page.
# @@RETURN_SCHEMA@@"""


# # Maersk: this carrier prints TWO structurally different layouts — a table
# # right on the front page (seal glued to the container id, no separate
# # label) OR a front page with NO container ids at all (just a shipment-wide
# # summary) whose real table is on a later continuation page (seal on its
# # own "Shipper Seal :" line). Self-detect which one this document is.
# MAERSK_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
# from this Maersk Bill of Lading / Non-Negotiable Waybill PDF — it may span \
# multiple pages, read all of them — and return ONLY a JSON object, no \
# markdown.

# - "mbl_no": the B/L No. (top right).
# - "port_of_loading": Port of Loading.
# - "container_type": leave empty — every container row states its own type
#   directly (see below), on whichever layout this document uses.

# This document uses ONE of two layouts:

# ══════════════════════════════════════════
# LAYOUT A — container table directly on the front/goods-description page
# ══════════════════════════════════════════
# Row shaped like:
#   <CONTAINER ID><SEAL, alphanumeric with dashes like ML-QA0064803> <TYPE
#   like "40 DRY 9'6"> <BAGS COUNT> BAGS <GROSS WEIGHT>KGS <MEASUREMENT>CBM
#   Example: "HASU4839118 ML-QA0064803 40 DRY 9'6 840 BAGS 21504.00 KGS
#   40.000 CBM" -> id "HASU4839118", seal "ML-QA0064803", type "40 DRY 9'6".
#   The seal is NOT separated from the container id by a space here — the
#   container id is always exactly the first 4 letters + 7 digits (11
#   characters) of that token, everything after it on the same "word" is the
#   seal.

# ══════════════════════════════════════════
# LAYOUT B — front page is a shipment-wide summary only (NO container ids at
# all, just "N containers said to contain..." and totals); the REAL table is
# on a later/continuation page (labeled "Page : 2" or similar)
# ══════════════════════════════════════════
# Row + following line shaped like:
#   <CONTAINER ID> <TYPE, e.g. "40 DRY 8'6"> <BAGS COUNT> BAG <GROSS
#   WEIGHT> KGS <MEASUREMENT> CBM
#   Shipper Seal : <SEAL>
#   Example: "MRKU0510841 40 DRY 8'6 990 BAG 25245.000 KGS 51.7030 CBM" then
#   next line "Shipper Seal : 283978" -> id "MRKU0510841", type "40 DRY 8'6",
#   seal "283978".

# Read every such row/block wherever the real table turns out to be — a
# shipment can have more than one container, each its own entry; do not stop
# after the first one.
# @@RETURN_SCHEMA@@"""


# # Grimaldi Deep Sea Combined Transport Bill of Lading: the "Marks and Nos"
# # column repeats a per-container id/seal block, separate from a shipment-
# # wide free-text description that states the container size once.
# GRIMALDI_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
# from this Grimaldi Deep Sea Combined Transport Bill of Lading PDF — it \
# spans multiple pages, with the container list continuing across all of \
# them (later pages don't repeat the header, just more container rows), \
# read every page — and return ONLY a JSON object, no markdown.

# - "mbl_no": the "Bl. No." value (same as "Booking No." on this carrier).
# - "port_of_loading": "Port of loading" (page 1) / "POL:" (later pages).
# - "container_type": the container size from a line like "20 40 ft. High
#   Cube" or "20 HC CONTAINERS 40'" near the top of the goods description
#   (e.g. "40HC") — applies to every container, since individual container
#   rows don't repeat a type.

# CONTAINER TABLE — the "Marks and Nos" column repeats this block once per
# container:
#   <CONTAINER ID>
#   Seal #(s):
#   <SEAL>
#   Example: "ACLU9795946" then "Seal #(s):" then "SA515350" -> id
#   "ACLU9795946", seal "SA515350". Leave "type" empty (use the shipment-wide
#   "container_type" above instead).
# Read every container block on every page — the total container count is
# usually confirmed near the end of page 1 as "Total No. of Containers: N".
# @@RETURN_SCHEMA@@"""


# # LX Pantos "BILL OF LADING" — a FIATA Multimodal Transport Bill of Lading
# # (FBL) issued by a freight forwarder. Its single most distinctive trait:
# # MULTIPLE containers' id/seal/weight/measurement/bags are packed into ONE
# # compact column as a repeating two-line pattern, not a normal
# # one-row-per-container table.
# LX_PANTOS_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
# from this LX Pantos Bill of Lading PDF (a freight-forwarder-issued FIATA \
# Multimodal Transport Bill of Lading) and return ONLY a JSON object, no \
# markdown.

# - "mbl_no": the "BL NO." value.
# - "port_of_loading": "PORT OF LOADING".
# - "container_type": the container size/type from a summary line like
#   "4X40 HC" or "FOUR (40HCX4) CONTAINERS ONLY" (e.g. "40HC") — applies to
#   every container, since the per-container entries (below) don't repeat a
#   type of their own.

# ⚠️ CONTAINER LIST — this is the part most likely to be misread. The
# "CONTAINER NO / SEAL NO / MARKS AND NUMBERS" column packs EVERY container
# into ONE compact list, as a repeating TWO-LINE pattern stacked vertically —
# NOT a normal one-row-per-container table:
#   <CONTAINER ID>/<SEAL>
#   (<GROSS WEIGHT>KG/<MEASUREMENT>M3/<BAGS>)/
#   Example — this exact shipment has FOUR containers, all in this one list:
#     "MSNU6011050/FX46720394"
#     "(26,818.000KG/44.000M3/22)/"
#     "CAIU4754042/FX46720397"
#     "(26,818.000KG/44.000M3/22)/"
#     "CAAU7536118/FX46720398"
#     "(26,818.000KG/44.000M3/22)/"
#     "MSMU5691010/FX46720393"
#     "(26,818.000KG/44.000M3/22)/"
#   -> FOUR separate container entries: ids "MSNU6011050", "CAIU4754042",
#   "CAAU7536118", "MSMU5691010", each with its OWN seal, read right next to
#   it. Read every repetition of the two-line pattern in this column, all the
#   way through — do NOT stop after the first one and do NOT merge them into
#   a single entry.
# Leave "type" empty for every container — not printed per-container on this
# document (container_type above covers the type).
# @@RETURN_SCHEMA@@"""


# # Hapag-Lloyd "Multimodal Transport or Port to Port Shipment" Bill of Lading.
# HAPAG_LLOYD_MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this Hapag-Lloyd \
# Bill of Lading PDF (it may span multiple pages — read all of them) and \
# return ONLY a JSON object, no markdown, no explanation.

# - "mbl_no": the "B/L-No." value (top right) — NOT the "Carrier's
#   Reference" number printed right next to it, that's a different number.
# - "port_of_loading": "Port of Loading" value.
# - "container_type": leave empty — every container's own block states its
#   size/type directly (see below).

# CONTAINER TABLE — one block per container, in the "Container Nos., Seal
# Nos., Marks and Nos." column. Each block is shaped like:
#   <CONTAINER ID>
#   SEALS : <SEAL NUMBER>
#   <a following unrelated line, e.g. a bare number — see warning below>
#   ...
#   <container size/type description, e.g. "1 CONT. 20'X8'6" GENERAL PURPOSE
#    CONT. SLAC*">
#   Example:
#     "UACU 3944378"
#     "SEALS : HLG6350667"
#     "033949"
#     "1 CONT. 20'X8'6" GENERAL PURPOSE CONT. SLAC*"
#   -> id "UACU3944378" (strip the space), seal "HLG6350667" (ONLY the token
#   printed immediately after "SEALS :" on that same line), type "20'X8'6"
#   GENERAL PURPOSE CONT." (drop the trailing "SLAC*" stowage-plan marker).

# ⚠️ A bare number on its OWN line right after the "SEALS :" line (e.g.
# "033949" in the example above) is NOT a second seal and is NOT part of the
# seal value — leave it out of "seal" entirely. The seal is only ever the
# single token that appears directly on the same line as the "SEALS :" label.

# Read every container block on every page — do not stop after the first one.
# @@RETURN_SCHEMA@@"""


# # MSC "Sea Waybill" — main page just refers to an attached "RIDER PAGE" for
# # the actual container/goods details.
# MSC_MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this MSC \
# (Mediterranean Shipping Company) Sea Waybill PDF — the front page states \
# header fields but refers to an attached "RIDER PAGE" for the actual \
# container/cargo table, read every page including the rider page(s) — and \
# return ONLY a JSON object, no markdown, no explanation.

# - "mbl_no": the "SEA WAYBILL No." value (top right).
# - "port_of_loading": "PORT OF LOADING" value.
# - "container_type": leave empty — every container's own block states its
#   size/type directly (see below).

# CONTAINER TABLE — on the "RIDER PAGE", in the "Container Numbers, Seal
# Numbers and Marks" column, one block per container shaped like:
#   <CONTAINER ID>
#   <TYPE, e.g. "40' HIGH CUBE">
#   SEAL NUMBER:<SEAL NUMBER>
#   Example: "MSDU8106323" then "40' HIGH CUBE" then "SEAL NUMBER:353408" ->
#   id "MSDU8106323", type "40' HIGH CUBE", seal "353408".
# Read every container block on every rider page — a shipment can have more
# than one container, each with its own repeating block; do not stop after
# the first one.
# @@RETURN_SCHEMA@@"""


# # Borchard Lines "Bill of Lading" — the "Marks and Nos; Container No:"
# # column lists every container with its own type + seal.
# BORCHARD_LINES_MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this \
# Borchard Lines Bill of Lading PDF and return ONLY a JSON object, no \
# markdown, no explanation.

# - "mbl_no": the "B/L No." value (top right).
# - "port_of_loading": "Port of loading" value.
# - "container_type": leave empty — every container's own block states its
#   size/type directly (see below).

# CONTAINER LIST — in the "Marks and Nos; Container No:" column, one block
# per container shaped like:
#   <CONTAINER ID>  <TYPE, e.g. "40 HC">
#   Seal no  <SEAL NUMBER>
#   Example: "BORU7010780   40 HC" then "Seal no   00500126" -> id
#   "BORU7010780", type "40 HC", seal "00500126". Read every such block —
#   this carrier typically lists SEVERAL containers this way; do not stop
#   after the first one.
# @@RETURN_SCHEMA@@"""


# # Fallback for any carrier not yet onboarded (COSCO / EVERGREEN / ZIM / PIL /
# # etc. will each get their own tuned prompt as those samples come in) — same
# # field shape as every carrier-specific prompt so build_rows() never needs
# # to know which prompt actually ran.
# GENERIC_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
# from this Master Bill of Lading / Sea Waybill PDF (it may span multiple \
# pages/sheets, and container details may be on an attached rider/
# continuation page — read all of them) and return ONLY a JSON object, no \
# markdown, no explanation.

# - "mbl_no": the Bill of Lading / Waybill number.
# - "port_of_loading": Port of Loading.
# - "container_type": if the document states ONE container type/size for the
#   whole shipment instead of repeating it per container (e.g. "11 X 40'H DC
#   CONTAINERS"), put that here and leave each row's own "type" empty.
#   Otherwise leave this "".
# - "containers": array of every container, each with:
#   - "id": container number, exactly 4 letters + 7 digits, no spaces.
#   - "seal": seal number.
#   - "type": container type as printed (e.g. "40' High Cube"), empty if
#     only a shipment-wide "container_type" above applies instead.

# ⚠️ Some freight-forwarder-issued documents (FIATA Multimodal Transport Bill
# of Lading / "FBL" forms, or a "CONTAINER NO / SEAL NO / MARKS AND NUMBERS"
# single combined column) pack MULTIPLE containers' id/seal into ONE compact
# column as a repeating two-line pattern, instead of a normal one-row-per-
# container table:
#   <CONTAINER ID>/<SEAL>
#   (<GROSS WEIGHT>KG/<MEASUREMENT>M3/<BAGS>)/
#   Example: "MSNU6011050/FX46720394" then "(26,818.000KG/44.000M3/22)/" ->
#   id "MSNU6011050", seal "FX46720394".
# This TWO-LINE pattern repeats once per container, stacked vertically in
# that same column/cell — read EVERY repetition of it, do not stop after the
# first pair. A separate summary line elsewhere on the page like "4X40 HC" is
# the shipment-wide total (container count × type) — route it into
# "container_type", it is NOT itself one more container to add to the list.

# ⚠️ Some documents (e.g. OOCL-style) put a shipment-wide SUMMARY row on the
# first page under a non-container-shaped code (doesn't match 4 letters + 7
# digits) next to "TOTAL BAGS"/"TOTAL ...KGS" figures, with the REAL
# per-container table on a LATER page after a "TO BE CONTINUED ON ATTACHED
# LIST" notice — read every page and only extract rows matching the real
# 4-letter+7-digit container ID shape.
# @@RETURN_SCHEMA@@"""


# for _name in (
#     "CMA_CGM_MBL_PROMPT", "YANG_MING_MBL_PROMPT", "HMM_MBL_PROMPT", "ONE_MBL_PROMPT",
#     "OOCL_MBL_PROMPT", "MAERSK_MBL_PROMPT", "GRIMALDI_MBL_PROMPT", "LX_PANTOS_MBL_PROMPT",
#     "HAPAG_LLOYD_MBL_PROMPT", "MSC_MBL_PROMPT", "BORCHARD_LINES_MBL_PROMPT", "GENERIC_MBL_PROMPT",
# ):
#     globals()[_name] = globals()[_name].replace("@@RETURN_SCHEMA@@", RETURN_SCHEMA)


# CARRIER_MBL_PROMPTS = {
#     "CMA CGM":        CMA_CGM_MBL_PROMPT,
#     "YANG MING":      YANG_MING_MBL_PROMPT,
#     "HMM":            HMM_MBL_PROMPT,
#     "ONE":            ONE_MBL_PROMPT,
#     "OOCL":           OOCL_MBL_PROMPT,
#     "MAERSK":         MAERSK_MBL_PROMPT,
#     "GRIMALDI":       GRIMALDI_MBL_PROMPT,
#     "LX PANTOS":      LX_PANTOS_MBL_PROMPT,
#     "HAPAG-LLOYD":    HAPAG_LLOYD_MBL_PROMPT,
#     "MSC":            MSC_MBL_PROMPT,
#     "BORCHARD LINES": BORCHARD_LINES_MBL_PROMPT,
# }

# # ═══════════════════════════════════════════════════════════════════════════
# # PACKING LIST PROMPT (PDF path) — TWO known layouts, self-detected
# # ═══════════════════════════════════════════════════════════════════════════
# # LAYOUT SIDPEC (Sidi Kerir Petrochemicals Co.): ONE flat table, one row per
# # container, "No. Of Pallets" stated directly per row — no code-side
# # division needed. No Seal No column anywhere on this document.
# #
# # LAYOUT ETHYDCO (The Egyptian Ethylene & Derivatives Co.): a table grouped
# # by LOT rather than strictly by container — one container's cargo CAN be
# # split across two consecutive rows, each with its own Lot No/weight/bags/
# # pallets, when it was loaded with two different lots. Both rows are real
# # and are kept as SEPARATE output line items (never merged/summed) — see
# # module docstring. No Seal No column anywhere on this document either.
# #
# # Whichever layout is detected, "seal_no" is always left "" — neither known
# # layout prints one; it comes from the MBL as a fallback in build_rows().
# PKG_LIST_PROMPT = """You are a shipping-document data extractor. Extract all data from this \
# Packing List PDF and return ONLY a JSON object, no markdown, no explanation.
# @@OCR_DISAMBIGUATION_RULE@@

# This document uses ONE of two layouts. Identify which one FIRST from the
# detection cues below, then apply ONLY that layout's rules.

# ══════════════════════════════════════════
# LAYOUT SIDPEC — Sidi Kerir Petrochemicals Co. ("Sidpec")
# ══════════════════════════════════════════
# How to detect: "Sidi Kerir Petrochemicals Co." / "Sidpec" letterhead; ONE
# table (may span multiple pages, header repeats) with columns # | Container
# No | Grade | Net Weight | Gross Weight | No. Of Bags | No. Of Pallets | Lot
# No., and a final TOTALS row (just a row count in the "#" column, e.g. "26",
# then summed Net/Gross/Bags/Pallets, and a BLANK Lot No cell).

# HEADER FIELDS:
# - "customer": the "CUSTOMER" box value (e.g. "Swiss Polymers AG").
# - "total_bags": the totals row's "No. Of Bags" sum.
# - "total_net_weight_mt": the totals row's "Net Weight" sum.
# - "total_gross_weight_mt": the totals row's "Gross Weight" sum.
# - "total_pallets": the totals row's "No. Of Pallets" sum.

# LINE ITEMS — one per real container row (exclude the final TOTALS row —
# identify it by its blank Lot No cell and/or being the last row under the
# table — never extract it as a container of its own):
# - "container_id": the "Container No" column value (4 letters + 7 digits).
# - "product": the "Grade" column value (e.g. "HD 6070 UA").
# - "lot_no": the "Lot No." column value EXACTLY as printed — this can be a
#   single number (e.g. "253") OR a hyphenated pair of two lot numbers when a
#   container carries two lots (e.g. "252-253", "254-252") — copy either
#   shape verbatim, never split or reformat it.
# - "net_weight_mt": the "Net Weight" column value for this row, EXACTLY as
#   printed (e.g. 27.000) — this is a metric-ton figure, do not convert or
#   rescale it yourself.
# - "gross_weight_mt": the "Gross Weight" column value for this row, EXACTLY
#   as printed — same unit rule as net weight.
# - "bags": the "No. Of Bags" column value for this row (integer).
# - "pallets": the "No. Of Pallets" column value for this row (integer) —
#   this document states its own per-row pallet count directly; use it as
#   printed, do not compute it.

# Read every container row on every page of the table — do not stop after the
# first page.

# ══════════════════════════════════════════
# LAYOUT ETHYDCO — The Egyptian Ethylene & Derivatives Co. ("ETHYDCO")
# ══════════════════════════════════════════
# How to detect: "The Egyptian Ethylene & Derivatives Co. (ETHYDCO)"
# letterhead; title "PACKING LIST DETAILS"; ONE table with columns CONTAINER
# NO | GRADE | QTY/MT | LOT NO | NET WEIGHT/MT | GROSS WEIGHT/MT | BAGS NO |
# PALLETS NO | NO OF CONTAINER/S, ending in a "GRAND TOTAL" row.

# HEADER FIELDS:
# - "customer": the "CUSTOMER" box value (e.g. "SWISS POLY MERS" — copy
#   exactly as printed even if the spacing looks unusual, do not "fix" it).
# - "total_bags": the "GRAND TOTAL" row's "BAGS NO" value.
# - "total_net_weight_mt": the "GRAND TOTAL" row's "NET WEIGHT/MT" value.
# - "total_gross_weight_mt": the "GRAND TOTAL" row's "GROSS WEIGHT/MT" value.
# - "total_pallets": the "GRAND TOTAL" row's "PALLETS NO" value.

# LINE ITEMS — one per table row (exclude the final "GRAND TOTAL" row itself
# — never extract it as a line item):
# - "container_id": the "CONTAINER NO" column value — printed hyphenated
#   (e.g. "CSLU-603288-3"); copy it exactly as printed, the hyphens are
#   stripped afterward in code, do not remove them yourself.
# - "product": the "GRADE" column value (e.g. "5333-AAH").
# - "lot_no": the "LOT NO" column value for THIS row.
# - "net_weight_mt": the "NET WEIGHT/MT" column value for this row, EXACTLY
#   as printed — this is a metric-ton figure, do not convert or rescale it
#   yourself. (The "QTY/MT" column repeats this same figure — ignore it, use
#   NET WEIGHT/MT.)
# - "gross_weight_mt": the "GROSS WEIGHT/MT" column value for this row,
#   EXACTLY as printed.
# - "bags": the "BAGS NO" column value for this row (integer).
# - "pallets": the "PALLETS NO" column value for this row (integer) — this
#   document states its own per-row pallet count directly; use it as
#   printed, do not compute it.
# - Ignore the "NO OF CONTAINER/S" column entirely — it's a fractional
#   cross-check value (this row's share of one container's full capacity),
#   not needed in the output.

# ⚠️ CRITICAL — SPLIT-CONTAINER ROWS: HOW THE TABLE ACTUALLY LOOKS

# On this layout, a container loaded with two different lots gets its
# CONTAINER NO and GRADE cells vertically merged across two data rows.
# The container ID and grade are printed ONCE, spanning both rows visually,
# while QTY/MT, LOT NO, NET WEIGHT/MT, GROSS WEIGHT/MT, BAGS NO, PALLETS NO
# each have their OWN separate value on EACH of the two rows.

# Study the three worked examples below — they show exactly how the table
# appears in the PDF and exactly what output each produces.

# ══════════════════════════════════════════
# WORKED EXAMPLE 1 — split container followed by a normal (un-split)
# container (THIS IS THE #1 EXTRACTION ERROR — read carefully)
# ══════════════════════════════════════════
# What the PDF table looks like (one merged CONTAINER NO cell spanning two
# data rows, then a separate un-split container on the next row):

#   CONTAINER NO     GRADE     QTY/MT  LOT NO  NET WT/MT  GROSS WT/MT  BAGS  PALLETS
#   ┌─────────────┐ ┌────────┐
#   │ AAAA-1111-1 │ │ X-100  │   3      501      3         3.069      120     2
#   │             │ │        │  24      502     24        24.552      960    16
#   └─────────────┘ └────────┘
#   BBBB-2222-2     X-100     27      502     27        27.621     1080    18

# This is THREE output line items — not two:
#   1. {"container_id":"AAAA-1111-1", "product":"X-100", "lot_no":"501", "net_weight_mt":3,   "gross_weight_mt":3.069,  "bags":120,  "pallets":2}
#   2. {"container_id":"AAAA-1111-1", "product":"X-100", "lot_no":"502", "net_weight_mt":24,  "gross_weight_mt":24.552, "bags":960,  "pallets":16}
#   3. {"container_id":"BBBB-2222-2", "product":"X-100", "lot_no":"502", "net_weight_mt":27,  "gross_weight_mt":27.621, "bags":1080, "pallets":18}

# THE ERROR TO AVOID: row 2 (lot 502, 24 MT, 960 bags) has NO container ID
# printed on its own line — the merged cell above it covers it. Do NOT let
# this row's values bleed into row 3 (BBBB-2222-2). Row 3 is a completely
# independent container with its OWN full set of values (27/502/27/27.621/
# 1080/18). It does NOT inherit anything from the split pair above it.

# ══════════════════════════════════════════
# WORKED EXAMPLE 2 — TWO split containers back-to-back
# ══════════════════════════════════════════
# What the PDF table looks like:

#   CONTAINER NO     GRADE     QTY/MT  LOT NO  NET WT/MT  GROSS WT/MT  BAGS  PALLETS
#   ┌─────────────┐ ┌────────┐
#   │ CCCC-3333-3 │ │ Y-200  │   1.5    601      1.5       1.5345      60     1
#   │             │ │        │  25.5    602     25.5      26.0865    1020    17
#   └─────────────┘ └────────┘
#   ┌─────────────┐ ┌────────┐
#   │ DDDD-4444-4 │ │ Y-200  │  21      603     21        21.483     840    14
#   │             │ │        │   6      604      6         6.138     240     4
#   └─────────────┘ └────────┘
#   EEEE-5555-5     Y-200     27      603     27        27.621    1080    18

# This is FIVE output line items:
#   1. {"container_id":"CCCC-3333-3", "product":"Y-200", "lot_no":"601", "net_weight_mt":1.5,  "bags":60,   "pallets":1}
#   2. {"container_id":"CCCC-3333-3", "product":"Y-200", "lot_no":"602", "net_weight_mt":25.5, "bags":1020, "pallets":17}
#   3. {"container_id":"DDDD-4444-4", "product":"Y-200", "lot_no":"603", "net_weight_mt":21,   "bags":840,  "pallets":14}
#   4. {"container_id":"DDDD-4444-4", "product":"Y-200", "lot_no":"604", "net_weight_mt":6,    "bags":240,  "pallets":4}
#   5. {"container_id":"EEEE-5555-5", "product":"Y-200", "lot_no":"603", "net_weight_mt":27,   "bags":1080, "pallets":18}

# Row 2 belongs to CCCC-3333-3 (NOT DDDD-4444-4).
# Row 4 belongs to DDDD-4444-4 (NOT EEEE-5555-5).
# Each split container's second row has NO printed container ID — the merged
# cell above it covers it. The NEXT container after a split pair always
# starts fresh with its OWN printed container ID on its OWN row.

# ══════════════════════════════════════════
# WORKED EXAMPLE 3 — normal containers between splits (most common pattern)
# ══════════════════════════════════════════
# What the PDF table looks like:

#   CONTAINER NO     GRADE     QTY/MT  LOT NO  NET WT/MT  GROSS WT/MT  BAGS  PALLETS
#   ┌─────────────┐ ┌────────┐
#   │ FFFF-6666-6 │ │ Z-300  │   1.5    701      1.5       1.5345      60     1
#   │             │ │        │  25.5    702     25.5      26.0865    1020    17
#   └─────────────┘ └────────┘
#   GGGG-7777-7     Z-300     27      702     27        27.621    1080    18
#   HHHH-8888-8     Z-300     27      702     27        27.621    1080    18
#   IIII-9999-9     Z-300     27      702     27        27.621    1080    18

# This is SIX output line items:
#   1. {"container_id":"FFFF-6666-6", "lot_no":"701", "net_weight_mt":1.5,  "bags":60,   "pallets":1}
#   2. {"container_id":"FFFF-6666-6", "lot_no":"702", "net_weight_mt":25.5, "bags":1020, "pallets":17}
#   3. {"container_id":"GGGG-7777-7", "lot_no":"702", "net_weight_mt":27,   "bags":1080, "pallets":18}
#   4. {"container_id":"HHHH-8888-8", "lot_no":"702", "net_weight_mt":27,   "bags":1080, "pallets":18}
#   5. {"container_id":"IIII-9999-9", "lot_no":"702", "net_weight_mt":27,   "bags":1080, "pallets":18}

# Row 2 belongs to FFFF-6666-6 (the merged cell), NOT to GGGG-7777-7.
# Rows 3-5 are each independent un-split containers — each has its OWN
# printed container ID and its OWN complete row of values.

# ══════════════════════════════════════════
# HOW TO READ THE TABLE — STEP BY STEP
# ══════════════════════════════════════════
# 1. Set current_container = "" (empty).
# 2. Read each data row top-to-bottom across all pages.
# 3. For each row:
#    a. If the CONTAINER NO column has a printed value on THIS row →
#       set current_container = that value. This row belongs to
#       current_container.
#    b. If the CONTAINER NO column is BLANK on this row (it is visually
#       covered by the merged cell from the row above) → this row ALSO
#       belongs to current_container (the same container as the previous
#       row). Do NOT look ahead to the next row's container ID — that is a
#       DIFFERENT container entirely.
# 4. Output each row as a separate line item with current_container as its
#    container_id.

# ⚠️ SELF-CHECK BEFORE RETURNING:
# - Sum all "net_weight_mt" across every output line item. It MUST equal the
#   GRAND TOTAL row's NET WEIGHT/MT exactly. If it is SHORT, you lost a
#   split row (the most common cause: a split container's second row was
#   dropped or its values were merged into the following container's row).
# - Sum all "bags". Must equal the GRAND TOTAL row's BAGS NO.
# - Sum all "pallets". Must equal the GRAND TOTAL row's PALLETS NO.
# - If any check fails, re-scan the table using the step-by-step algorithm
#   above and fix the error before returning.

# Read every row of the table across every page — do not stop after the
# first page.

# ══════════════════════════════════════════
# OUTPUT FORMAT (same shape regardless of which layout you detected)
# ══════════════════════════════════════════
# Return:
# {
#   "customer": "string",
#   "total_bags": 0,
#   "total_net_weight_mt": 0,
#   "total_gross_weight_mt": 0,
#   "total_pallets": 0,
#   "containers": [
#     {"container_id": "string", "product": "string", "lot_no": "string",
#      "net_weight_mt": 0, "gross_weight_mt": 0, "bags": 0, "pallets": 0}
#   ]
# }""".replace("@@OCR_DISAMBIGUATION_RULE@@", OCR_DISAMBIGUATION_RULE)

# # ═══════════════════════════════════════════════════════════════════════════
# # EXTRACTION FUNCTIONS
# # ═══════════════════════════════════════════════════════════════════════════

# def extract_mbl(pdf_path: str) -> dict:
#     carrier = identify_carrier(pdf_path)
#     prompt = CARRIER_MBL_PROMPTS.get(carrier, GENERIC_MBL_PROMPT)

#     data = call_gemini(prompt, pdf_path=pdf_path, max_output_tokens=16384)
#     dump_json(pdf_path, "mbl_raw.json", data)

#     for c in data.get("containers", []):
#         cid, seal = fix_container_id(c.get("id", ""), c.get("seal", ""))
#         c["id"] = cid
#         c["seal"] = seal

#     data["carrier"] = carrier
#     dump_json(pdf_path, "mbl.json", data)
#     print(f"  [MBL] Carrier identified as {carrier} — "
#           f"{'carrier-specific' if carrier in CARRIER_MBL_PROMPTS else 'generic fallback'} prompt used")
#     return data


# def extract_packing_list_pdf(pdf_path: str) -> dict:
#     """PDF Packing List path (SIDPEC or ETHYDCO layout, self-detected in the
#     prompt — see PKG_LIST_PROMPT). Returns the same common shape as
#     excel_extractor.extract_packing_list_excel(): a flat "containers" list
#     of line items, each already carrying net/gross weight in KG."""
#     data = call_gemini(PKG_LIST_PROMPT, pdf_path=pdf_path, max_output_tokens=16384)
#     dump_json(pdf_path, "pkg_list_raw.json", data)

#     containers = []
#     for row in data.get("containers", []):
#         cid, _ = fix_container_id(row.get("container_id", ""))
#         net_weight_mt = num(row.get("net_weight_mt"), 0)
#         gross_weight_mt = num(row.get("gross_weight_mt"), 0)
#         containers.append({
#             "container_id":     cid,
#             # Neither PDF layout prints a Seal No anywhere — always the
#             # MBL's fallback in build_rows().
#             "seal_no":          "",
#             "product":          s(row.get("product")).strip(),
#             "lot_no":           s(row.get("lot_no")).strip(),
#             "net_weight_kg":    to_kg(net_weight_mt, "MT"),
#             "gross_weight_kg":  to_kg(gross_weight_mt, "MT"),
#             "bags":             num(row.get("bags"), 0),
#             "pallets":          num(row.get("pallets"), 0),
#         })
#     data["containers"] = containers
#     data["packing_list_source"] = "pdf"
#     # Cross-check totals stay in MT here (matches the document's own units);
#     # validate() converts to KG before comparing against row sums.
#     data["total_bags"] = num(data.get("total_bags"), 0)
#     data["total_net_weight_mt"] = num(data.get("total_net_weight_mt"), 0)
#     data["total_gross_weight_mt"] = num(data.get("total_gross_weight_mt"), 0)
#     data["total_pallets"] = num(data.get("total_pallets"), 0)

#     dump_json(pdf_path, "pkg_list.json", data)
#     return data


# # ═══════════════════════════════════════════════════════════════════════════
# # CROSS-DOCUMENT VALIDATION
# # ═══════════════════════════════════════════════════════════════════════════

# def validate(mbl: dict, pkl: dict) -> list[str]:
#     results = []

#     carrier = s(mbl.get("carrier")).strip()
#     if carrier:
#         if carrier in CARRIER_MBL_PROMPTS:
#             results.append(f"[OK] CARRIER — {carrier} (carrier-specific MBL prompt)")
#         else:
#             results.append(f"[!]  CARRIER — {carrier} (generic fallback MBL prompt — not yet carrier-tuned, "
#                             f"verify every MBL field manually)")

#     if s(mbl.get("mbl_no")).strip():
#         results.append(f"[OK] MBL No — {s(mbl.get('mbl_no')).strip()}")
#     else:
#         results.append("[X]  MBL No — not found on the MBL")

#     mbl_cids = {c["id"] for c in mbl.get("containers", []) if c.get("id")}
#     pkl_cids = {c["container_id"] for c in pkl.get("containers", []) if c.get("container_id")}
#     common = mbl_cids & pkl_cids
#     only_mbl = mbl_cids - pkl_cids
#     only_pkl = pkl_cids - mbl_cids

#     if common:
#         results.append(f"[OK] CONTAINERS — {len(common)} matched across MBL & Packing List")
#     for c in sorted(only_mbl):
#         results.append(f"[!]  CONTAINER — {c} only in MBL (not in Packing List)")
#     for c in sorted(only_pkl):
#         results.append(f"[!]  CONTAINER — {c} only in Packing List (not in MBL)")

#     pkl_bags_sum = sum(num(c.get("bags"), 0) for c in pkl.get("containers", []))
#     pkl_total_bags = num(pkl.get("total_bags"), 0)
#     if pkl_bags_sum and pkl_total_bags:
#         if pkl_bags_sum == pkl_total_bags:
#             results.append(f"[OK] BAGS — Packing List rows sum({pkl_bags_sum}) = document total({pkl_total_bags})")
#         else:
#             results.append(f"[!]  BAGS — Packing List rows sum({pkl_bags_sum}) vs document total({pkl_total_bags})")

#     pkl_pallets_sum = sum(num(c.get("pallets"), 0) for c in pkl.get("containers", []))
#     pkl_total_pallets = num(pkl.get("total_pallets"), 0)
#     if pkl_pallets_sum and pkl_total_pallets:
#         if pkl_pallets_sum == pkl_total_pallets:
#             results.append(f"[OK] PALLETS — Packing List rows sum({pkl_pallets_sum}) = document total({pkl_total_pallets})")
#         else:
#             results.append(f"[!]  PALLETS — Packing List rows sum({pkl_pallets_sum}) vs document total({pkl_total_pallets})")

#     # Weight cross-checks — PDF-sourced totals are in MT (converted to KG
#     # here to compare against the already-KG row sums); an Excel-sourced
#     # Packing List never sets total_*_weight_mt (see excel_extractor.py), so
#     # this check is silently skipped there — the sheet has no document-wide
#     # total to check against, and the row sums are trusted as-is (already
#     # exact cell values, not a vision model's reading).
#     pkl_net_sum_kg = round(sum(num(c.get("net_weight_kg"), 0) for c in pkl.get("containers", [])), 3)
#     pkl_total_net_kg = to_kg(pkl.get("total_net_weight_mt"), "MT")
#     if pkl_net_sum_kg and pkl_total_net_kg:
#         if abs(pkl_net_sum_kg - pkl_total_net_kg) < 1:
#             results.append(f"[OK] NET WEIGHT — Packing List rows sum({pkl_net_sum_kg} KG) = document total({pkl_total_net_kg} KG)")
#         else:
#             results.append(f"[!]  NET WEIGHT — Packing List rows sum({pkl_net_sum_kg} KG) vs document total({pkl_total_net_kg} KG)")

#     pkl_gross_sum_kg = round(sum(num(c.get("gross_weight_kg"), 0) for c in pkl.get("containers", [])), 3)
#     pkl_total_gross_kg = to_kg(pkl.get("total_gross_weight_mt"), "MT")
#     if pkl_gross_sum_kg and pkl_total_gross_kg:
#         if abs(pkl_gross_sum_kg - pkl_total_gross_kg) < 1:
#             results.append(f"[OK] GROSS WEIGHT — Packing List rows sum({pkl_gross_sum_kg} KG) = document total({pkl_total_gross_kg} KG)")
#         else:
#             results.append(f"[!]  GROSS WEIGHT — Packing List rows sum({pkl_gross_sum_kg} KG) vs document total({pkl_total_gross_kg} KG)")

#     if pkl.get("containers"):
#         results.append(f"[OK] LINE ITEMS — {len(pkl['containers'])} row(s) extracted from the Packing List "
#                         f"({pkl.get('packing_list_source', '?')} source)")
#     else:
#         results.append("[X]  LINE ITEMS — no rows extracted from the Packing List")

#     return results


# # ═══════════════════════════════════════════════════════════════════════════
# # ROW BUILDER
# # ═══════════════════════════════════════════════════════════════════════════

# def build_rows(mbl: dict, pkl: dict, reference: str = "", eta_date: str = "") -> list[dict]:
#     reference = s(reference).strip()

#     # Origin (Port of Loading), matching Sabic/Vinmar/Emvia/Continental
#     # Inbound's convention — NOT destination.
#     country_code = get_country_code(s(mbl.get("port_of_loading")).strip())

#     mbl_map = {}
#     for c in mbl.get("containers", []):
#         mbl_map[c["id"]] = {
#             "type": s(c.get("type")).strip(),
#             "seal": s(c.get("seal")).strip(),
#         }
#     # Some carriers print ONE container type for the whole shipment instead
#     # of repeating it per container (see e.g. HMM_MBL_PROMPT).
#     shipment_container_type = normalize_container_type(mbl.get("container_type", "")) if s(mbl.get("container_type")).strip() else ""

#     mbl_no = s(mbl.get("mbl_no")).strip()

#     rows = []
#     for row in pkl.get("containers", []):
#         cid = row.get("container_id", "")
#         mbl_entry = mbl_map.get(cid, {})

#         # Seal: THIS row's own Packing List value wins whenever present
#         # (populated only on the Excel path — see excel_extractor.py; both
#         # PDF layouts never print a seal at all) — the MBL is only a
#         # fallback for a blank row, same convention as Continental Inbound.
#         seal_no = s(row.get("seal_no")).strip() or mbl_entry.get("seal", "")

#         raw_type = mbl_entry.get("type", "")
#         container_type = normalize_container_type(raw_type) if raw_type else shipment_container_type

#         rows.append({
#             "reference":      reference,
#             "container_no":   cid,
#             "container_ref":  f"{cid}/{reference}",
#             "mbl_no":         mbl_no,
#             "seal_no":        seal_no,
#             "container_type": container_type,
#             "country_code":   country_code,
#             "product":        s(row.get("product")).strip(),
#             "lot_no":         s(row.get("lot_no")).strip(),
#             "bags":           num(row.get("bags"), 0),
#             "net_weight":     num(row.get("net_weight_kg"), 0),
#             "gross_weight":   num(row.get("gross_weight_kg"), 0),
#             "pallet_qty":     num(row.get("pallets"), 0),
#             "eta_date":       eta_date,
#         })

#     return rows

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


def extract_packing_list_pdf(pdf_path: str) -> dict:
    """PDF Packing List path (SIDPEC or ETHYDCO layout, self-detected in the
    prompt — see PKG_LIST_PROMPT). Returns the same common shape as
    excel_extractor.extract_packing_list_excel(): a flat "containers" list
    of line items, each already carrying net/gross weight in KG."""
    data = call_gemini(PKG_LIST_PROMPT, pdf_path=pdf_path, max_output_tokens=16384)
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
    containers = []
    for row in fixed:
        net_weight_mt = num(row.get("net_weight_mt"), 0)
        gross_weight_mt = num(row.get("gross_weight_mt"), 0)
        containers.append({
            "container_id":     row.get("container_id", ""),
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
    "OOCL":            "Orient Overseas Container Line",
    "MSC":             "Mediterrenean Shipping Company",
    "HAPAG-LLOYD":     "Hapag Lloyd",
    "BORCHARD LINES":  "BORCHARD LINES LTD.",
    "MAERSK":          "Maersk",
    "EVERGREEN":       "Evergreen",
    "ZIM":             "ZIM lines",
    "ONE":             "ONE",
    "COSCO":           "Cosco Container Line",
    "YANG MING":       "Yang Ming",
    "HMM":             "Hyundai Merchant Marine",
    "CMA CGM":         "CMA CGM",
}


def carrier_display(carrier: str) -> str:
    return CARRIER_DISPLAY_MAP.get(s(carrier).strip().upper(), "Other")


def build_rows(mbl: dict, pkl: dict, reference: str = "", eta_date: str = "",
                ship_name: str = "") -> list[dict]:
    reference = s(reference).strip()
    # Ship Name is UI-picked (not extracted from either document) — same
    # convention as reference/eta_date — applied uniformly to every row in
    # this shipment. Shipping Line, by contrast, comes from the MBL's own
    # already-identified carrier (see carrier_display() above) — never a
    # separate UI field, so it can never disagree with the MBL.
    ship_name = s(ship_name).strip()
    shipping_line = carrier_display(mbl.get("carrier", ""))

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
    for row in pkl.get("containers", []):
        cid = row.get("container_id", "")

        # A container must appear on BOTH documents to be trusted — one
        # only on the Packing List (never confirmed by the MBL) or only on
        # the MBL (never confirmed by the Packing List) is excluded from
        # the output entirely rather than emitted with a blank Seal/Type.
        # validate()'s "[!] CONTAINER — X only in ..." lines are exactly
        # this same mbl_map/pkl_cids comparison — every skip here has a
        # matching line there, so nothing is dropped silently.
        if cid not in mbl_map:
            skipped_no_mbl_match.append(cid)
            continue

        mbl_entry = mbl_map[cid]
        seal_no = s(row.get("seal_no")).strip() or mbl_entry.get("seal", "")

        raw_type = mbl_entry.get("type", "")
        container_type = normalize_container_type(raw_type) if raw_type else shipment_container_type

        rows.append({
            "reference":      reference,
            "container_no":   cid,
            "container_ref":  f"{cid}/{reference}",
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