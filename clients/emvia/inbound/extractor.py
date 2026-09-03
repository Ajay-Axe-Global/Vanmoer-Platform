"""
Emvia Inbound (Warehouse 1147) — Extraction, validation, and row-building.

Single fixed layout per document, PDF-only — unlike Vinmar/Sabic, warehouse
1147 has (so far) only ever been seen with one MBL layout (Hapag-Lloyd
"Multimodal Transport / Port to Port" Bill of Lading) and one Packing List
layout (VE Staal B.V.'s "Packing List Enclosure" bundle table), so there is
no carrier-detection dispatch or multi-layout self-detection here — if a
second layout for either document shows up later, follow Vinmar's
CARRIER_MBL_PROMPTS / self-detecting-prompt pattern to add it.

Country-code lookup, container-type normalization, and MT/KG conversion are
shared with Sabic/Vinmar Inbound via helpers/doc_common.py.

Batch numbers on the Packing List are cross-checked/corrected against the
PDF's own text layer (see _pdf_batch_numbers below) rather than trusted
purely from the Gemini call — a page break splitting one bundle row's
bundle-number line from its own "// <batch>" line onto the NEXT page proved
unreliable for the model to re-pair correctly even with explicit prompt
instructions (it kept attaching the orphaned batch number to the following
row instead of the row it actually belongs to). Since this document has a
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
# MBL PROMPT — Hapag-Lloyd "Multimodal Transport or Port to Port Shipment"
# Bill of Lading (warehouse 1147's only layout so far)
# ═══════════════════════════════════════════════════════════════════════════

MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this Hapag-Lloyd \
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

⚠️ UNIT — the "GROSS WT.(...)" label's own parenthesized unit is NOT always
"MTS" on every document; some print it already in KGS (e.g. "GROSS WT.
(KGS): 27610.000"). Read whichever unit is ACTUALLY printed in that
parenthesis for THIS document and set "gross_weight_unit" accordingly: "MT"
if it reads "(MTS)"/"(MT)"/"(TONS)", "KG" if it reads "(KGS)"/"(KG)". Return
"gross_weight" exactly as printed either way — the unit conversion happens
afterward in code, not by you; do not multiply or divide the number
yourself regardless of which unit it's in.

⚠️ A bare number on its OWN line right after the "SEALS :" line (e.g.
"033949" in the example above) is NOT a second seal and is NOT part of the
seal value — it belongs to unrelated document text (marks/numbers block
layout), leave it out of "seal" entirely. The seal is only ever the single
token that appears directly on the same line as the "SEALS :" label.

Read every container block on every page — do not stop after the first one.

Return:
{
  "mbl_no": "string", "port_of_loading": "string", "port_of_discharge": "string",
  "destination_country": "string",
  "containers": [
    {"id": "string", "seal": "string", "type": "string", "gross_weight": 0,
     "gross_weight_unit": "MT"}
  ]
}"""


# ═══════════════════════════════════════════════════════════════════════════
# PACKING LIST PROMPT — VE Staal B.V. "Packing List Enclosure" (bundle table)
# ═══════════════════════════════════════════════════════════════════════════
# PKG_LIST_PROMPT = """You are a shipping-document data extractor. Extract all data from this VE \
# Staal B.V. Packing List PDF — it is a cover page followed by one or more \
# "Packing List Enclosure" pages, read every page — and return ONLY a JSON \
# object, no markdown, no explanation.
 
# COVER PAGE:
# - "destination_country": the "COUNTRY OF FINAL DESTINATION" value (e.g.
#   "BELGIUM").
 
# LAST ENCLOSURE PAGE — a "GRAND TOTAL :" row (the very last totals row in the
# whole document, after every container/order/grade/size has been summed):
# - "grand_total_pieces": that row's own "Pcs of Bar" figure, transcribed
#   exactly as printed on THIS document (a whole number).
# - "grand_total_net_mt": that row's own "NetWt (TO)" figure, transcribed
#   exactly as printed on THIS document (a decimal number of metric tons).
# These two are a printed ground truth used only to verify your own row
# extraction below — read them directly off the page, do not compute them
# yourself, and do not reuse a number from any other example in these
# instructions.
 
# ENCLOSURE PAGES — a table with columns: Grade/Cust. Material | Size | Shape
# | Length | Tolerance | Heat No | Bundle | Pcs of Bar | Gross Wt (TO) | Tare
# Wt (TO) | NetWt (TO), grouped under a "CONTAINER NO : <id>" header (repeated
# on every page it spans) and further split into "ORDER NO <n>" sub-sections.
# ⚠️ An "ORDER NO" section can CONTINUE onto a following page WITHOUT
# repeating its "ORDER NO <n>" label — a page that starts straight into more
# bundle rows (no new "ORDER NO" line visible) still belongs to the last
# "ORDER NO" seen, do not treat a missing label as the start of some
# unlabeled group. Ignore the order-no grouping itself for the output (it's
# not one of the fields below), just make sure you read every real bundle row
# underneath it, from whichever order section it falls under, including rows
# that continue after a page break with no new header.
 
# ⚠️ CRITICAL — FIRST ROW AFTER A SECTION HEADER. The very first bundle row
# right after a category header line (e.g. "COLD DRAWN GROUND - 2G") and/or
# an "ORDER NO" line is a REAL data row, even though its Grade/Cust. Material
# cell may look truncated or partially merged with the header above it (e.g.
# showing "1/1.4307" instead of the full "1.4301/1.4307" because the leading
# characters were absorbed by the category label's layout). This row has its
# own Bundle number, its own "// <batch>" line, its own Pcs/weights — do NOT
# skip it, do NOT merge it with the row below it, and do NOT steal its batch
# number and assign it to the next row's weights. If the Grade text looks
# shorter or unusual on the first row, that is a layout artifact, not a
# reason to drop the row.
 
# Each REAL bundle row has a Bundle cell spanning TWO lines: a long bundle
# number, then a SECOND line starting with "//" followed by a shorter number —
# e.g.:
#   "13000549195"
#   "// 8664556"
# -> that row's "batch_no" is "8664556" (whatever digits are actually printed
# after "//" on the SECOND line — never the long first-line bundle number
# itself).
 
# ⚠️ CRITICAL — PAGE-BREAK SPLIT ROWS. This two-line Bundle cell can be torn
# apart by a page break: a page can END right after a row's bundle-number
# line AND its Pcs/Gross Wt/Tare Wt/NetWt figures (a complete-LOOKING row),
# with that SAME row's "// <batch>" second line pushed onto the very TOP of
# the NEXT page, printed there alone before any new row's Grade/Cust.
# Material starts.
 
# REAL WORKED EXAMPLE — exactly how this layout appears at an actual page
# boundary on this type of document:
 
#   ══════ end of page 2 ══════
#   1.4301/1.4307   40 X 40 X 4 MM   ANGLE   6.00 - 6.20   EN 10056   GFG96
#   13000549194
#   // 8664547
#                                                     36    0.516   0.002   0.514
#   1.4301/1.4307   40 X 40 X 4 MM   ANGLE   6.00 - 6.20   EN 10056   GFH12
#   13000549193
#                                                     37    0.534   0.002   0.532
#   ══════ start of page 3 ══════
#   // 8664550
#   1.4301/1.4307   40 X 40 X 4 MM   ANGLE   6.00 - 6.20   EN 10056   GFH23
#   13000549186
#   // 8664588
#                                                     39    0.564   0.002   0.562
#   1.4301/1.4307   40 X 40 X 4 MM   ANGLE   6.00 - 6.20   EN 10056   GFH23
#   13000549185
#   // 8664591
#                                                     39    0.564   0.002   0.562
 
# STEP-BY-STEP CORRECT READING of the above:
 
#   (a) LAST COMPLETE ROW OF PAGE 2 — bundle 13000549194, batch_no "8664547"
#       (its "//" line IS on the same page, so it's fully complete), pieces 36,
#       gross_weight_mt 0.516, net_weight_mt 0.514.
 
#   (b) LAST ROW OF PAGE 2 — SPLIT ACROSS THE PAGE BREAK — bundle
#       13000549193 has its Pcs/weights on page 2 (37 / 0.534 / 0.532) but
#       its "//" batch line is MISSING from page 2. That batch line is the
#       VERY FIRST line of page 3: "// 8664550". So:
#         batch_no = "8664550"
#         pieces = 37
#         gross_weight_mt = 0.534
#         net_weight_mt = 0.532
#       The "8664550" is NOT the batch of the row printed below it on page 3
#       (bundle 13000549186) — it belongs UPWARD to bundle 13000549193 whose
#       weights (37, 0.534, 0.532) are on the previous page.
 
#   (c) FIRST FULL ROW OF PAGE 3 — bundle 13000549186, batch_no "8664588"
#       (its own "//" line is right below it, on the same page), pieces 39,
#       gross_weight_mt 0.564, net_weight_mt 0.562.
 
#   (d) SECOND ROW OF PAGE 3 — bundle 13000549185, batch_no "8664591",
#       pieces 39, gross_weight_mt 0.564, net_weight_mt 0.562.
 
# The CORRECT output for these four rows:
#   {"product": "EN 10056", "batch_no": "8664547", "pieces": 36,
#    "net_weight_mt": 0.514, "gross_weight_mt": 0.516},
#   {"product": "EN 10056", "batch_no": "8664550", "pieces": 37,
#    "net_weight_mt": 0.532, "gross_weight_mt": 0.534},
#   {"product": "EN 10056", "batch_no": "8664588", "pieces": 39,
#    "net_weight_mt": 0.562, "gross_weight_mt": 0.564},
#   {"product": "EN 10056", "batch_no": "8664591", "pieces": 39,
#    "net_weight_mt": 0.562, "gross_weight_mt": 0.564}
 
# ⚠️ THE MOST COMMON WRONG OUTPUT looks like this (DO NOT produce this):
#   {"batch_no": "8664547", "pieces": 36, ...},
#   {"batch_no": "8664556", "pieces": 37, ...},   ← WRONG: "8664556" is from
#        the PREVIOUS row, duplicated because the model didn't see the
#        orphaned "// 8664550" at the top of page 3. The correct batch is
#        "8664550".
#   {"batch_no": "8664550", "pieces": 39, ...},   ← WRONG: "8664550" paired
#        with 39/0.564/0.562 which actually belong to batch "8664588". The
#        model attached the orphaned batch to the NEXT row instead of the
#        PREVIOUS row.
#   {"batch_no": "8664588", "pieces": 39, ...}
# This cascading error happens when the orphaned "//" line at the top of a
# new page is attached DOWNWARD to the next row instead of UPWARD to the
# last row of the previous page. It also causes batch "8664556" to be
# duplicated (appearing on two rows) — which is ALWAYS a red flag.
 
# ⚠️ PAGE-BOUNDARY PROCEDURE — every time you reach a page boundary inside
# the bundle table, STOP and follow these steps BEFORE continuing:
#   1. Look at the VERY FIRST non-header line on the new page. Is it a
#      standalone "// <digits>" line with NO bundle-number line directly
#      above it on THIS page?
#   2. If YES → that "//" line's batch_no belongs to the LAST bundle row
#      you read on the PREVIOUS page. Go back and assign it NOW.
#   3. If NO → the previous page's last row already had its "//" line, and
#      this page starts fresh with a new row. Proceed normally.
# Do this check at EVERY page transition. Never skip it.
 
# ⚠️ CRITICAL — every batch number on this document is DIFFERENT, even
# between consecutive rows. If you find yourself about to output the SAME
# batch_no on two or more rows, that is a red flag you did not actually read
# one of them — go back and re-read that row's own "//" line individually.
# Never copy a neighboring row's batch number. Two mistakes are equally wrong:
# (a) pairing the orphaned batch with the following row's numbers instead of
# the row it belongs to, and (b) duplicating the previous row's batch number
# because the orphaned line was missed — both have been seen to happen.
 
# ⚠️ This document is a photocopy/scan — digits can look similar to each other
# (e.g. 2 vs 6, 8 vs 6, 3 vs 8), which is a common cause of misreading one
# digit in an otherwise-correct number. Read each digit of the batch number
# individually and carefully, directly off the row you are currently on,
# rather than pattern-matching against a nearby row's number or reusing a
# value from memory. Double check every batch number digit-by-digit before
# finalizing it.
 
# For every such row, extract:
# - "product": this row's "Tolerance" column value (e.g. "h9-K240", "h9") —
#   this is the per-row product identifier on this document, not the page-1
#   "Description Of Goods" text.
# - "batch_no": as described above.
# - "pieces": the "Pcs of Bar" column value (integer).
# - "net_weight_mt": the "NetWt (TO)" column value for this row (already in
#   metric tons).
# - "gross_weight_mt": the "Gross Wt (TO)" column value for this row (already
#   in metric tons).
 
# ⚠️ Do NOT extract a "SIZE TOTAL" / "SHAPE TOTAL" / "GRADE TOTAL" / "ORDER
# TOTAL" / "GRAND TOTAL" row as a bundle — those ARE, however, real printed
# subtotals you must use for a self-check (see below); just don't emit them
# as bundle rows themselves (no Tolerance/Heat No/Bundle number of their own).
 
# ⚠️ SELF-CHECK — this document lists many near-identical-looking rows (same
# Tolerance, similar weights) across several pages, which makes it easy to
# skip rows without noticing. Before finalizing your output:
# 1. For each "SIZE TOTAL" line, sum the "Pcs of Bar" and "NetWt (TO)" of the
#    bundle rows you extracted for that same Size/Tolerance/Heat-No group
#    directly above it, and compare to the printed SIZE TOTAL row. If they
#    don't match, you missed or misread a row in that group — go back and
#    re-read that group's rows (including across a page break) until they do.
# 2. Do the same check one level up against each "GRADE TOTAL" and, at the
#    very end of the document, against the final "GRAND TOTAL" line (Pcs of
#    Bar and NetWt (TO)) — your full "bundles" list's pieces and net_weight_mt
#    should sum to exactly that GRAND TOTAL. If not, re-scan every page,
#    including ones you think you already covered, until it matches.
# Read every real bundle row on every page, including across an "ORDER NO"
# section boundary and a page boundary — do not stop early.
 
# Group the rows under their container:
# "containers": [
#   {"container_no": "string", "bundles": [
#     {"product": "string", "batch_no": "string", "pieces": 0,
#      "net_weight_mt": 0, "gross_weight_mt": 0}
#   ]}
# ]
 
# Return:
# {
#   "destination_country": "string",
#   "grand_total_pieces": 0,
#   "grand_total_net_mt": 0,
#   "containers": [
#     {"container_no": "string", "bundles": [
#       {"product": "string", "batch_no": "string", "pieces": 0,
#        "net_weight_mt": 0, "gross_weight_mt": 0}
#     ]}
#   ]
# }"""
 

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
 
⚠️ CRITICAL — every batch number on this document is DIFFERENT, even
between consecutive rows. If you find yourself about to output the SAME
batch_no on two or more rows, that is a red flag you did not actually read
one of them — go back and re-read that row's own "//" line individually.
Never copy a neighboring row's batch number. Two mistakes are equally wrong:
(a) pairing the orphaned batch with the following row's numbers instead of
the row it belongs to, and (b) duplicating the previous row's batch number
because the orphaned line was missed — both have been seen to happen.
 
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
# DETERMINISTIC BATCH-NUMBER CROSS-CHECK (pdfplumber, no LLM involved)
# ═══════════════════════════════════════════════════════════════════════════

_BATCH_NO_RE = re.compile(r"//\s*(\d{5,9})")


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


# ═══════════════════════════════════════════════════════════════════════════
# EXTRACTION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def extract_mbl(pdf_path: str) -> dict:
    data = call_gemini(MBL_PROMPT, pdf_path=pdf_path, max_output_tokens=8192)
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
        })
    data["containers"] = containers

    dump_json(pdf_path, "mbl.json", data)
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
    if pdf_batches and len(pdf_batches) == len(all_bundles):
        for bundle, batch_no in zip(all_bundles, pdf_batches):
            bundle["batch_no"] = batch_no
        data["batch_no_source"] = "pdf_text_layer"
    else:
        data["batch_no_source"] = "gemini"
        data["batch_no_count_mismatch"] = {
            "pdf_text_layer_count": len(pdf_batches),
            "gemini_row_count": len(all_bundles),
        }

    dump_json(pdf_path, "pkg_list.json", data)
    return data


# ═══════════════════════════════════════════════════════════════════════════
# CROSS-DOCUMENT VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

def validate(mbl: dict, pkl: dict) -> list[str]:
    results = []

    if pkl.get("batch_no_source") == "pdf_text_layer":
        results.append("[OK] BATCH NO — read directly from the PDF's text layer (deterministic, not the AI model)")
    elif "batch_no_count_mismatch" in pkl:
        mismatch = pkl["batch_no_count_mismatch"]
        results.append(f"[!]  BATCH NO — PDF text layer has {mismatch['pdf_text_layer_count']} '//' batch tokens "
                        f"but extraction returned {mismatch['gemini_row_count']} bundle rows; counts must match "
                        f"exactly to safely auto-correct, so batch numbers below are the AI model's own reading "
                        f"(unverified) — check them manually")

    if s(mbl.get("mbl_no")).strip():
        results.append(f"[OK] MBL No — {s(mbl.get('mbl_no')).strip()}")
    else:
        results.append("[X]  MBL No — not found on the MBL")

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
    # instead of transcribing each row's own "//" line. Every bundle on
    # this document is a distinct batch — the same batch_no legitimately
    # repeating more than once or twice is essentially never correct, so
    # flag it loudly rather than silently shipping duplicated/misread rows.
    batch_no_counts: dict = {}
    for c in pkl.get("containers", []):
        for b in c.get("bundles", []):
            batch_no = s(b.get("batch_no")).strip()
            if batch_no:
                batch_no_counts[batch_no] = batch_no_counts.get(batch_no, 0) + 1
    for batch_no, count in batch_no_counts.items():
        if count > 2:
            results.append(f"[X]  BATCH NO — \"{batch_no}\" appears on {count} different bundle rows — "
                            f"every batch should be distinct, this strongly suggests the extraction repeated "
                            f"one row's value instead of reading each row separately; re-check those rows manually")

    return results


# ═══════════════════════════════════════════════════════════════════════════
# ROW BUILDER
# ═══════════════════════════════════════════════════════════════════════════

def build_rows(mbl: dict, pkl: dict, reference: str, warehouse: str) -> list[dict]:
    ui_reference = s(reference).strip()
    warehouse = s(warehouse).strip()

    country_code = get_country_code(
        s(pkl.get("destination_country")).strip() or s(mbl.get("destination_country")).strip()
    )

    mbl_map = {c["id"]: c for c in mbl.get("containers", []) if c.get("id")}

    rows = []
    for c in pkl.get("containers", []):
        cid = c.get("container_no", "")
        mbl_entry = mbl_map.get(cid, {})
        container_type = normalize_container_type(mbl_entry.get("type", "")) if mbl_entry.get("type") else ""
        seal_no = mbl_entry.get("seal", "")

        for b in c.get("bundles", []):
            # Excel-sourced bundles carry their OWN Ref/Receiver per row
            # (read straight off the sheet) — that always wins over the
            # UI-entered Reference field, which exists only for the PDF
            # path where no such per-row value is printed anywhere.
            row_reference = s(b.get("reference")).strip() or ui_reference
            receiver = s(b.get("receiver")).strip()
            ref_receiver = f"{row_reference}+{receiver}" if receiver else ""

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
                "pieces_qty":     num(b.get("pieces"), 0),
                "net_weight":     num(b.get("net_weight_kg"), 0),
                "gross_weight":   num(b.get("gross_weight_kg"), 0),
                "pallet_count":   0,
                "ref_receiver":   ref_receiver,
            })

    return rows
