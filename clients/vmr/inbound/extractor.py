"""
VMR Clients Inbound — Extraction, validation, and row-building.

"VMR Clients" is ONE task covering THREE underlying customers, chosen from a
UI dropdown (see task.py's CUSTOMERS): Karl Gross, Dashbach, Hakotrans. The
dropdown changes which documents are required and which columns the output
Excel gets — not three separate clients/tasks, one form that branches:

  - Karl Gross:            MBL only. No Packing List, no Product/Qty/Net/
                            Gross columns — Public ID/Seal/Shipping Line/etc.
                            all come from the MBL + UI-picked fields.
  - Dashbach / Hakotrans:  MBL + Packing List. Same base columns as Karl
                            Gross, PLUS Product, Product Qty, Net Weight,
                            Gross Weight — sourced from the Packing List,
                            EXCEPT Product itself, which (unusually) is
                            printed only on the MBL's goods-description
                            block, never on the Packing List, for this pair
                            of customers. Both customers share identical
                            extraction/output logic (confirmed by the user —
                            there is no behavioral difference between them,
                            only the display name differs).

MBL extraction is the same two-call pipeline used by Sabic/Vinmar/Emvia/
Continental/Swiss Inbound: identify_carrier() names the carrier, then
extract_mbl() dispatches to that carrier's own tuned prompt. Two carriers
have been seen in real VMR samples so far and get their own tuned prompts
below: COSCO SHIPPING LINES (Karl Gross's samples) and Evergreen Line
(issued as agent for a chartered vessel, e.g. "CMA CGM VENDOME" — Dashbach/
Hakotrans's sample). Every OTHER carrier reuses Swiss Inbound's
already-proven CARRIER_MBL_PROMPTS library wholesale (imported, not
duplicated) with one small addition appended (PRODUCT_ADDON below) since
Swiss's own prompts never needed to extract a "product" field — this
client's Dashbach/Hakotrans branch does. A GENERIC_MBL_PROMPT fallback
covers any carrier with no tuned prompt at all yet, same convention as every
other client's extractor.

Packing List extraction (Dashbach/Hakotrans only) is deliberately shallow:
this customer pair's Packing List is organized as one block/page PER
CONTAINER (the "MARKS" column states that container's id once for the whole
block), with several product-dimension rows (e.g. steel tube sizes like
"14*1.85") that are NOT separate lots/containers — just a size breakdown —
ending in a "TOTAL" row that already sums Net Weight, Gross Weight, and
Bundle count for that container. Per the user's explicit instruction: never
re-sum the dimension rows in code, never emit one output row per dimension
or per lot — read each block's own printed TOTAL row directly and emit
exactly ONE output row per container. Net Weight and Gross Weight are then
BOTH set to that same summed Net Weight figure (also per explicit
instruction — the two output columns are intentionally identical, not the
document's own separate Net/Gross totals).

Country-code/container-type normalization and container-ID fixups are
shared with every other client via helpers/doc_common.py.
"""

from helpers.doc_common import (
    dump_json,
    fix_container_id,
    normalize_container_type,
    num,
    s,
)
from helpers.gemini_client import call_gemini

# ═══════════════════════════════════════════════════════════════════════════
# MBL CARRIER IDENTIFICATION (call #1) — identical to Swiss/Continental's,
# already normalizes to "COSCO"/"EVERGREEN" among its fixed spellings.
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
named elsewhere on the page — they are frequently different companies. This \
also applies when a carrier (e.g. Evergreen Line) issues a Bill of Lading \
"as agent for the Carrier and the Vessel Provider" operating a chartered \
vessel under a different line's name (e.g. "CMA CGM VENDOME") — identify \
the ISSUING/SIGNING line (Evergreen), not the chartered vessel's own name.

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
-> "BORCHARD LINES". "COSCO SHIPPING LINES CO.,LTD." -> "COSCO". \
"Evergreen Marine (Asia) Pte. Ltd." / "Evergreen Line" -> "EVERGREEN".

If the issuer is real but not in that list, return its name as printed on \
the document. If you cannot tell at all, return "UNKNOWN".

{"carrier": "string"}"""


def identify_carrier(pdf_path: str) -> str:
    data = call_gemini(CARRIER_ID_PROMPT, pdf_path=pdf_path, max_output_tokens=256, call_label="carrier_id")
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
Seal numbers have no fixed letter/digit pattern to resolve ambiguity that
way, so for THAT field look especially carefully at the actual glyph shape
before deciding between visually similar pairs: "O"/"0", "I"/"1", "S"/"5",
"B"/"8", "Z"/"2" — transcribe exactly what is printed, do not default to
whichever reads more like a "normal" number."""

RETURN_SCHEMA = """
@@OCR_DISAMBIGUATION_RULE@@

Return:
{
  "mbl_no": "string", "port_of_loading": "string", "product": "string",
  "container_type": "string",
  "containers": [
    {"id": "string", "seal": "string", "type": "string"}
  ]
}""".replace("@@OCR_DISAMBIGUATION_RULE@@", OCR_DISAMBIGUATION_RULE)


# ── COSCO SHIPPING LINES — Karl Gross's samples ─────────────────────────────
# "PORT TO PORT OR COMBINED TRANSPORT BILL OF LADING" layout: a shipment-wide
# "N/M <bundle count> <PRODUCT>\nBUNDLES PO NO.: <n>" summary row (NOT a
# container), followed by one compact line per REAL container.
COSCO_MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this COSCO \
SHIPPING LINES "Port to Port or Combined Transport Bill of Lading" PDF and \
return ONLY a JSON object, no markdown, no explanation.

- "mbl_no": the "Bill of Lading No." value (top right box).
- "port_of_loading": "Port of Loading" value.
- "product": the goods description from the "Description of Goods" column's
  FIRST row — a shipment-wide summary shaped like "N/M <bundle count>
  <PRODUCT NAME>" with "BUNDLES" and a "PO NO.: <number>" line right below
  it (e.g. "PRIME NEW SEAMLESS STEEL TUBE"). Extract ONLY the product name
  itself — drop the bundle count, the word "BUNDLES", and the "PO NO."
  line entirely.
- "container_type": leave empty — every container row states its own type
  directly (see below).

⚠️ Do NOT treat that first "N/M ..." row as a container — it has no
container ID, only a shipment-wide bundle count, gross weight, and PO
number. The REAL container table is the block of compact lines below it.

CONTAINER TABLE — one line per container, shaped like:
  <CONTAINER ID> /<SEAL> / <BUNDLE COUNT> BUNDLES /FCL/FCL /<TYPE like
  40GP>/<GROSS WEIGHT>KGS;<MEASUREMENT>CBM
  Example: "CCLU5207522 /CX357374 / 15 BUNDLES /FCL/FCL /40GP/22033.000KGS;
  5.0000CBM" -> id "CCLU5207522", seal "CX357374", type "40GP".
Read every such line — a shipment can have more than one container, each
its own line; do not stop after the first one.
@@RETURN_SCHEMA@@"""


# ── Evergreen Line — Dashbach/Hakotrans's sample ────────────────────────────
# Issued as agent for the Carrier/Vessel Provider (a chartered vessel, e.g.
# "CMA CGM VENDOME") — numbered field boxes, one compact line per container
# INSIDE the goods-description area (not a separate table), followed by a
# shipment-wide product/HS-code line.
EVERGREEN_MBL_PROMPT = """You are a shipping-document data extractor. Extract data from this Evergreen \
Line Bill of Lading PDF (issued "as agent for the Carrier and the Vessel \
Provider", commonly under a chartered vessel name like "CMA CGM VENDOME" — \
that vessel name is NOT the carrier, Evergreen is) and return ONLY a JSON \
object, no markdown, no explanation.

- "mbl_no": the "(5) Document No." value.
- "port_of_loading": the "(15) Port of Loading" value.
- "product": the goods description line printed below the container list
  (e.g. "WELDED COLD ROLLED PRECISION STEEL TUBES") — drop a trailing
  "HS: <code>" line entirely, that is a tariff code, not part of the
  product name.
- "container_type": leave empty — every container line states its own size
  directly (see below).

CONTAINER LIST — inside the "(20) Description of Goods" area, one line per
container, shaped like:
  <CONTAINER ID>/<SIZE, e.g. 20' or 40'>/<SEAL>/<BUNDLE COUNT> BUNDLES
  <GROSS WEIGHT> KGS <MEASUREMENT> CBM
  Example: "EGSU2343828/20'/EMCWWA4154/25 BUNDLES 22526.000 KGS 20.0000
  CBM" -> id "EGSU2343828", type "20'", seal "EMCWWA4154".
Read every such line — do not stop after the first one. The total container
count is usually confirmed near the bottom as "<WORDS> (<digit>) CONTAINERS
ONLY" (e.g. "TWO (2) CONTAINERS ONLY").
@@RETURN_SCHEMA@@"""


for _name in ("COSCO_MBL_PROMPT", "EVERGREEN_MBL_PROMPT"):
    globals()[_name] = globals()[_name].replace("@@RETURN_SCHEMA@@", RETURN_SCHEMA)


# ── Every other carrier — reuse Swiss Inbound's already-proven prompt
# library wholesale (imported, never duplicated/forked) since it's the same
# kind of document (a Master Bill of Lading) and those prompts are already
# tuned per carrier from real samples across other clients. Swiss's prompts
# never needed a "product" field on the MBL (its Packing List always states
# product directly) — VMR's Dashbach/Hakotrans branch does, so PRODUCT_ADDON
# is appended to each one, asking for that one extra top-level field without
# touching the carrier-specific container-table logic at all.
from clients.swiss.inbound.extractor import CARRIER_MBL_PROMPTS as _SWISS_CARRIER_MBL_PROMPTS  # noqa: E402

PRODUCT_ADDON = """

Additionally, include a top-level "product" field in your JSON response
(alongside "mbl_no", "port_of_loading", etc.) with the shipment's goods
description — usually a short line near the shipper/goods-description block
(e.g. "PRIME NEW SEAMLESS STEEL TUBE", "WELDED COLD ROLLED PRECISION STEEL
TUBES", "STEEL PIPE") — drop any bundle/package count, unit words like
"BUNDLES", PO numbers, and HS/tariff code suffixes, keep just the product
name itself. "" if genuinely not printed anywhere."""

CARRIER_MBL_PROMPTS = {
    carrier: prompt + PRODUCT_ADDON
    for carrier, prompt in _SWISS_CARRIER_MBL_PROMPTS.items()
}
CARRIER_MBL_PROMPTS["COSCO"] = COSCO_MBL_PROMPT
CARRIER_MBL_PROMPTS["EVERGREEN"] = EVERGREEN_MBL_PROMPT


GENERIC_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
from this Master Bill of Lading / Sea Waybill PDF (it may span multiple \
pages/sheets, and container details may be on an attached rider/
continuation page — read all of them) and return ONLY a JSON object, no \
markdown, no explanation.

- "mbl_no": the Bill of Lading / Waybill number.
- "port_of_loading": Port of Loading.
- "product": the shipment's goods description, usually a short line near
  the shipper/goods-description block (e.g. "PRIME NEW SEAMLESS STEEL
  TUBE", "STEEL PIPE") — drop any bundle/package count, unit words, PO
  number, or HS/tariff code suffix, keep just the product name itself. ""
  if genuinely not printed anywhere.
- "container_type": if the document states ONE container type/size for the
  whole shipment instead of repeating it per container, put that here and
  leave each row's own "type" empty. Otherwise leave this "".
- "containers": array of every container, each with:
  - "id": container number, exactly 4 letters + 7 digits, no spaces.
  - "seal": seal number.
  - "type": container type as printed, empty if only a shipment-wide
    "container_type" above applies instead.
@@RETURN_SCHEMA@@"""

GENERIC_MBL_PROMPT = GENERIC_MBL_PROMPT.replace("@@RETURN_SCHEMA@@", RETURN_SCHEMA)


# ═══════════════════════════════════════════════════════════════════════════
# PACKING LIST PROMPT (Dashbach / Hakotrans only) — one block per container,
# read only each block's own printed TOTAL row, never re-sum dimension rows.
# ═══════════════════════════════════════════════════════════════════════════

PKG_LIST_PROMPT = """You are a shipping-document data extractor. Extract data from this Packing \
List PDF and return ONLY a JSON object, no markdown, no explanation.

This document is organized as ONE OR MORE per-container blocks (possibly
across several pages) — the "MARKS" column states ONE container ID (e.g.
"EGHU3386970") for the whole block. Each block lists several product
size/dimension rows (e.g. steel tube sizes like "14*1.85", "15*2.4") — these
are NOT separate lots or containers, just a size breakdown of the SAME
container's cargo, and you do NOT need to read or return them individually.

⚠️ Each block ends in its own "TOTAL" row with that block's own NET WEIGHT
(KGS), GROSS WEIGHT (KGS), and BUNDLE count already summed on the document
itself. Use THOSE printed totals directly — do NOT add up the dimension
rows yourself, and do NOT output one entry per dimension row.

⚠️ COLUMN ORDER — the TOTAL row (and every dimension row above it) always
prints NET WEIGHT in the LEFT/FIRST weight column and GROSS WEIGHT in the
RIGHT/SECOND weight column, immediately to its right. NET WEIGHT is ALWAYS
SMALLER than GROSS WEIGHT on the same row (gross includes packaging, net
does not) — e.g. if the TOTAL row reads "22476 | 22526 | 25", then
net_weight_kg is 22476 (the smaller, left-hand figure) and
gross_weight_kg is 22526 (the larger, right-hand figure), never the
other way round. Before answering, check: is your "net_weight_kg" ≤ your
"gross_weight_kg"? If not, you have read the two columns swapped — fix it.

⚠️ A signature/stamp graphic (e.g. "For and on behalf of ...", an authorized
signature line) is sometimes printed OVER or immediately beside a block's
TOTAL row, partially obscuring one of the weight figures — read the actual
printed digits underneath/around the stamp carefully rather than guessing;
do not assume a partly-obscured Net Weight equals the Gross Weight just
because it's harder to read, they are two genuinely different numbers.

If MORE THAN ONE container block appears in this document, there is also
usually a FINAL "TOTAL" row after every block, on its own, that sums ALL
containers together (e.g. a last "45026 | 45126 | 50" row, visibly larger
than — and separate from — each block's own smaller per-container TOTAL row
above it). Read that grand-total row too, if present.

Return one entry per container block (read every page — if the PDF covers
more than one container, each gets its own block and its own entry here):
{
  "containers": [
    {"container_id": "string", "net_weight_kg": 0, "gross_weight_kg": 0,
     "bundles": 0}
  ],
  "grand_total_net_weight_kg": 0,
  "grand_total_gross_weight_kg": 0,
  "grand_total_bundles": 0
}
("grand_total_*" fields: 0 if the document has only one container block and
no separate combined-total row.)"""


# ═══════════════════════════════════════════════════════════════════════════
# EXTRACTION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def extract_mbl(pdf_path: str) -> dict:
    carrier = identify_carrier(pdf_path)
    prompt = CARRIER_MBL_PROMPTS.get(carrier, GENERIC_MBL_PROMPT)

    data = call_gemini(prompt, pdf_path=pdf_path, max_output_tokens=16384, call_label="mbl")
    dump_json(pdf_path, "mbl_raw.json", data)

    for c in data.get("containers", []):
        cid, seal = fix_container_id(c.get("id", ""), c.get("seal", ""))
        c["id"] = cid
        c["seal"] = seal

    data["carrier"] = carrier
    data["product"] = s(data.get("product")).strip()
    dump_json(pdf_path, "mbl.json", data)
    print(f"  [MBL] Carrier identified as {carrier} — "
          f"{'carrier-specific' if carrier in CARRIER_MBL_PROMPTS else 'generic fallback'} prompt used")
    return data


def extract_packing_list(pdf_path: str) -> dict:
    data = call_gemini(PKG_LIST_PROMPT, pdf_path=pdf_path, max_output_tokens=16384, call_label="packing_list")
    dump_json(pdf_path, "pkg_list_raw.json", data)

    containers = []
    for row in data.get("containers", []):
        cid, _ = fix_container_id(row.get("container_id", ""))
        net_weight_kg = num(row.get("net_weight_kg"), 0)
        gross_weight_kg = num(row.get("gross_weight_kg"), 0)
        # Code-side safety net for the prompt's own column-order rule: Net
        # Weight can never legitimately exceed Gross Weight (gross includes
        # packaging). If Gemini reads the two weight columns swapped for a
        # given TOTAL row (observed on a real Dashbach/Hakotrans sample —
        # net_weight_kg came back holding the gross figure), this corrects
        # it rather than silently writing the wrong number to the Excel.
        if net_weight_kg and gross_weight_kg and net_weight_kg > gross_weight_kg:
            print(f"  [PKG] Swapped Net/Gross for container {cid} "
                  f"({net_weight_kg} > {gross_weight_kg}) — correcting")
            net_weight_kg, gross_weight_kg = gross_weight_kg, net_weight_kg
        containers.append({
            "container_id":    cid,
            "net_weight_kg":   net_weight_kg,
            "gross_weight_kg": gross_weight_kg,
            "bundles":         num(row.get("bundles"), 0),
        })

    # Grand-total cross-check — catches the case the swap-safety net above
    # can't (observed on a real sample: a signature stamp overlapping one
    # block's TOTAL row made Gemini read the SAME figure into both
    # net_weight_kg and gross_weight_kg, i.e. net_weight_kg == gross_weight_kg
    # rather than swapped, so the ">" check above never triggers). When the
    # document states its own combined grand total across every container
    # block, and exactly ONE container looks suspicious (its Net == Gross,
    # which never happens on a genuine row — packaging always adds some
    # weight), that one container's true Net Weight can be recovered exactly
    # by subtracting every other (trusted) container's own Net Weight from
    # the grand total — never guessed when more than one container is
    # ambiguous, since there'd be no way to tell which is really wrong.
    grand_total_net = num(data.get("grand_total_net_weight_kg"), 0)
    if grand_total_net and len(containers) > 1:
        suspicious = [c for c in containers if c["net_weight_kg"] == c["gross_weight_kg"]]
        if len(suspicious) == 1:
            others_net_sum = sum(c["net_weight_kg"] for c in containers if c is not suspicious[0])
            corrected_net = grand_total_net - others_net_sum
            if corrected_net > 0 and corrected_net != suspicious[0]["net_weight_kg"]:
                print(f"  [PKG] Grand-total cross-check corrected Net Weight for container "
                      f"{suspicious[0]['container_id']}: {suspicious[0]['net_weight_kg']} -> {corrected_net}")
                suspicious[0]["net_weight_kg"] = corrected_net

    data["containers"] = containers

    dump_json(pdf_path, "pkg_list.json", data)
    return data


# ═══════════════════════════════════════════════════════════════════════════
# FUZZY CONTAINER-ID MATCHING — same OCR-tolerant matching used by Swiss
# Inbound (clients/swiss/inbound/extractor.py), copied rather than imported
# since it's a small, self-contained utility and importing it would create a
# confusing cross-client dependency for something this tiny.
# ═══════════════════════════════════════════════════════════════════════════

def _fuzzy_match_container(cid: str, reference_cids: set[str], threshold: int = 2) -> str:
    """If cid isn't in reference_cids, find the closest match within
    `threshold` character differences (covers OCR confusions like U/Y,
    O/C/0, H/U, 1/6/l, B/8, S/5, Z/2 between the MBL and Packing List
    readings of the same container). Returns the matched id, or the
    original cid if no close match is found."""
    if cid in reference_cids or not cid:
        return cid

    best_match = None
    best_dist = threshold + 1
    for ref_cid in reference_cids:
        if len(ref_cid) != len(cid):
            continue
        dist = sum(1 for a, b in zip(cid, ref_cid) if a != b)
        if dist < best_dist:
            best_dist = dist
            best_match = ref_cid

    return best_match if best_match and best_dist <= threshold else cid


# ═══════════════════════════════════════════════════════════════════════════
# SHIPPING LINE DISPLAY MAP — reused verbatim from Swiss Inbound (same fixed
# spellings, including known typos like "Mediterrenean" — the client wants
# exactly this text). Imported, not duplicated, so a future correction to
# Swiss's map (e.g. onboarding a new carrier's display string) automatically
# applies here too.
# ═══════════════════════════════════════════════════════════════════════════

from clients.swiss.inbound.extractor import carrier_display  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# CROSS-DOCUMENT VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

def validate(mbl: dict, pkl: dict | None, needs_packing_list: bool) -> list[str]:
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

    if not mbl.get("containers"):
        results.append("[X]  CONTAINERS — no containers extracted from the MBL")
    else:
        results.append(f"[OK] CONTAINERS — {len(mbl['containers'])} found on the MBL")

    if not needs_packing_list:
        return results

    if not s(mbl.get("product")).strip():
        results.append("[!]  PRODUCT — not found on the MBL (Product column will be blank on every row)")

    if pkl is None:
        # Packing List is OPTIONAL for this customer pair — not uploaded is
        # a normal, expected state, not an extraction failure.
        results.append("[!]  PACKING LIST — not uploaded; Product Qty/Net/Gross left blank on every row")
        return results

    if not pkl.get("containers"):
        results.append("[X]  PACKING LIST — uploaded but no container blocks were extracted from it")
        return results

    mbl_cids = {c["id"] for c in mbl.get("containers", []) if c.get("id")}
    pkl_cids = {c["container_id"] for c in pkl.get("containers", []) if c.get("container_id")}
    common = mbl_cids & pkl_cids
    only_mbl = mbl_cids - pkl_cids
    only_pkl = pkl_cids - mbl_cids

    if common:
        results.append(f"[OK] CONTAINERS — {len(common)} matched across MBL & Packing List")
    for c in sorted(only_mbl):
        results.append(f"[!]  CONTAINER — {c} only in MBL (not in Packing List) — Product/Qty/Net/Gross will be 0")
    for c in sorted(only_pkl):
        results.append(f"[!]  CONTAINER — {c} only in Packing List (not in MBL) — excluded from output")

    return results


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC ID — one shipment-wide summary string ("10 x 40HC & 20FT"), computed
# once from the MBL's own container list and repeated identically on every
# output row (never a per-row value).
# ═══════════════════════════════════════════════════════════════════════════

def compute_public_id(mbl_containers: list[dict]) -> str:
    counts: dict[str, int] = {}
    order: list[str] = []
    for c in mbl_containers:
        raw_type = s(c.get("type")).strip()
        norm_type = normalize_container_type(raw_type) if raw_type else "??"
        if norm_type not in counts:
            counts[norm_type] = 0
            order.append(norm_type)
        counts[norm_type] += 1
    return " & ".join(f"{counts[t]} x {t}" for t in order)


# ═══════════════════════════════════════════════════════════════════════════
# ROW BUILDER
# ═══════════════════════════════════════════════════════════════════════════

def build_rows(mbl: dict, pkl: dict | None, needs_packing_list: bool, reference: str = "",
               ship_name: str = "", eta_date: str = "", etd_date: str = "") -> list[dict]:
    reference = s(reference).strip()
    ship_name = s(ship_name).strip()
    shipping_line = carrier_display(mbl.get("carrier", ""))
    public_id = compute_public_id(mbl.get("containers", []))

    pkl_map = {}
    if needs_packing_list and pkl:
        pkl_map = {c["container_id"]: c for c in pkl.get("containers", []) if c.get("container_id")}
    pkl_cids = set(pkl_map.keys())

    product = s(mbl.get("product")).strip()

    rows = []
    for c in mbl.get("containers", []):
        cid = s(c.get("id")).strip()
        if not cid:
            continue
        seal = s(c.get("seal")).strip()

        row = {
            "reference":     reference,
            "container_no":  cid,
            "container_ref": f"{cid}/{reference}",
            "public_id":     public_id,
            "seal_no":       seal,
            "shipping_line": shipping_line,
            "ship_name":     ship_name,
            "eta_date":      eta_date,
            "etd_date":      etd_date,
        }

        if needs_packing_list:
            if pkl is None:
                # Packing List is OPTIONAL for Dashbach/Hakotrans — when not
                # uploaded, Product still comes from the MBL (always
                # available), but Qty/Net/Gross have no source at all and
                # are left genuinely BLANK (empty string), never 0 — a 0
                # would misleadingly read as "zero weight/qty", not "unknown".
                row.update({
                    "product":       product,
                    "product_qty":   "",
                    "net_weight":    "",
                    "gross_weight":  "",
                })
            else:
                # Fuzzy-match the MBL's container id against the Packing
                # List's own readings before the lookup, so an OCR misread
                # on either document doesn't silently zero out Qty/Net/Gross.
                matched_cid = _fuzzy_match_container(cid, pkl_cids)
                pkl_row = pkl_map.get(matched_cid, {})
                net_weight_kg = num(pkl_row.get("net_weight_kg"), 0)
                row.update({
                    "product":       product,
                    "product_qty":   num(pkl_row.get("bundles"), 0),
                    # Net = Gross = the Packing List's own summed Net Weight
                    # — deliberately identical values, per explicit instruction.
                    "net_weight":    net_weight_kg,
                    "gross_weight":  net_weight_kg,
                })

        rows.append(row)

    return rows
