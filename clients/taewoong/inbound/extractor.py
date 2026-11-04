"""
Taewoong Inbound (Taewoong Logistics Germany — shipper Hyosung Chemical) —
MBL extraction, Packing List extraction, cross-document validation and
row-building.

Source documents
  - MBL (PDF)          any carrier. Every carrier layout already known to the
                       platform (Swiss's CARRIER_MBL_PROMPTS library) is reused
                       unchanged; on top of it this client adds the
                       "NON-NEGOTIABLE SEA WAYBILL" layout issued by
                       YANG MING (KOREA) CO., LTD. as agent (Hyosung / Busan
                       shipments), see SEAWAYBILL_MBL_PROMPT.
  - Packing List (PDF) ONE layout only: Hyosung Chemical's "PACKING LIST"
                       (see PKG_LIST_PROMPT) — per-container table with
                       CNT NO / SEAL NO / Lot No / WEIGHT / GROSS WEIGHT / GRADE.

Where each OP column comes from
  Container No   Packing List (clean print), matched to the MBL container
  Seal No        MBL; if the MBL gives none for a container, the optional
                 Arrival Notice PDF (per container); last resort Packing List
  Container Type MBL ("40HQ" -> normalized)
  Product        Packing List GRADE (e.g. R202P)
  Bags Qty       MBL per-container bag count; else net weight / bag size
                 ("NET WT.: 25KGS" on the Packing List)
  Net Weight     Packing List per-container WEIGHT
  Gross Weight   Packing List per-container GROSS WEIGHT, else MBL
  Pallet Qty     not stated on any document -> 0 (same convention as Sunrise)
  Lot            Packing List Lot No
UI inputs (External ID, Shipping Line, Vessel/Ship Name, ETA) apply to every row.
"""

import re

from clients.swiss.inbound.extractor import (
    CARRIER_DISPLAY_MAP,
    CARRIER_MBL_PROMPTS,
    GENERIC_MBL_PROMPT,
    RETURN_SCHEMA,
    carrier_display,
    identify_carrier,
    is_valid_container_id,
)
from helpers.doc_common import (
    dump_json,
    fix_container_id,
    normalize_container_type,
    num,
    s,
)
from helpers.gemini_client import call_gemini

# Shipping-line dropdown: same short aliases as EDF (+ "Other"); the output
# gets the full OP spelling via carrier_display().
SHIPPING_LINE_OPTIONS = list(CARRIER_DISPLAY_MAP.keys()) + ["Other"]

# Net weight vs bags x bag size tolerance, and total-vs-sum tolerance (kg).
WEIGHT_TOLERANCE_KG = 1.0


# ═══════════════════════════════════════════════════════════════════════════
# MBL — new layout + carrier fallback chain
# ═══════════════════════════════════════════════════════════════════════════

SEAWAYBILL_MBL_PROMPT = """You are a shipping-document data extractor. Extract the following fields \
from this "NON-NEGOTIABLE SEA WAYBILL" PDF (issued by YANG MING (KOREA) CO., \
LTD. "as agent for the Carrier", for cargo shipped from Korea) and return \
ONLY a JSON object, no markdown, no explanation. It may have an attached \
list page — read every page.

- "mbl_no": the value in the "B/L No." box at the top right (e.g.
  "YMJAN360205116") — NOT the "Booking No." box next to it (e.g.
  "N360205116 W").
- "port_of_loading": the "Port of Loading" box (e.g. "BUSAN, KOREA").
- "container_type": leave empty — every container row carries its own type.

CONTAINER ROWS — in the table "PARTICULARS FURNISHED BY MERCHANT" there is \
ONE row per container, split across the narrow columns "MKS & NOS/CONTAINER \
NOS" | "NO OF PKGS" | "DESCRIPTION OF PACKAGES AND GOODS" | weight. Read left \
to right it is:
  <CONTAINER ID> <TYPE like 40HQ> | FCL/FCL <SEAL> <BAGS> BAGS <GROSS>KGS | <CBM>CBM
  Example: "TCNU4805272 40HQ FCL/FCL YMAT778847 960 BAGS 24115.200KGS
  37.500CBM" -> id "TCNU4805272", type "40HQ", seal "YMAT778847", bags 960,
  gross_weight_kg 24115.2.
The seal is the alphanumeric code printed IMMEDIATELY after "FCL/FCL" (e.g.
"YMAT778847", "YMAV499275") with no space in between — never the phrase
"SHIPPER'S DECLARED SEAL NUMBER" (a heading, not a seal). The text above the
rows (e.g. "2 CTNR", "S.T.C 1920 BAGS", "48230.400KGS", "75.000CBM") is the
shipment-wide TOTAL, NOT a container — never output it as one. Read every
container row; each gross weight is that row's OWN value.
⚠️ Some copies are BROKEN: the 4-letter owner prefix of the container id is cut
off or hidden (the row starts with a stray letter/fragment such as "J4805272").
Then copy exactly what is printed for "id" — never invent or complete the
letters; the 7 digits are what matter.
@@RETURN_SCHEMA@@"""
SEAWAYBILL_MBL_PROMPT = SEAWAYBILL_MBL_PROMPT.replace("@@RETURN_SCHEMA@@", RETURN_SCHEMA)


def _prompt_chain(carrier: str) -> list[tuple[str, str]]:
    """Prompts to try for this carrier, in order, as (label, prompt). The next
    one is used only when the previous produced no valid container ids, so a
    carrier already tuned in the platform keeps behaving exactly as before and
    the new layout is the safety net (and the first choice for carriers the
    platform doesn't know)."""
    known = CARRIER_MBL_PROMPTS.get(carrier)
    chain = []
    if known:
        chain.append((f"{carrier} (carrier-specific)", known))
    chain.append(("Non-negotiable sea waybill (Korea)", SEAWAYBILL_MBL_PROMPT))
    chain.append(("generic fallback", GENERIC_MBL_PROMPT))
    return chain


def _clean_containers(data: dict) -> int:
    """Normalises ids/seals in place; returns how many ids are valid-shaped."""
    good = 0
    for c in data.get("containers", []) or []:
        raw = re.sub(r"[\s\-]", "", s(c.get("id"))).upper()
        cid, seal = fix_container_id(raw, s(c.get("seal")))
        if not re.fullmatch(r"[A-Z]{4}\d{7}", cid):
            # Broken id (prefix hidden): keep what is printed, untouched, so
            # the 7 digits can be matched to the Packing List later.
            cid = raw
        c["id"], c["seal"] = cid, seal
        if re.search(r"\d{7}$", cid):
            good += 1
    return good


def extract_mbl(pdf_path: str) -> dict:
    carrier = identify_carrier(pdf_path)

    data, used = {}, ""
    for label, prompt in _prompt_chain(carrier):
        data = call_gemini(prompt, pdf_path=pdf_path, max_output_tokens=16384, call_label="mbl")
        if isinstance(data, list):
            data = data[0] if data else {}
        used = label
        if _clean_containers(data):
            break

    data["carrier"] = carrier
    data["_prompt_used"] = used
    dump_json(pdf_path, "mbl.json", data)
    print(f"  [MBL] Carrier {carrier} — prompt used: {used}")
    return data


# ═══════════════════════════════════════════════════════════════════════════
# PACKING LIST — Hyosung layout
# ═══════════════════════════════════════════════════════════════════════════

PKG_LIST_PROMPT = """You are a shipping-document data extractor. Extract data from this \
Hyosung Chemical Corporation "PACKING LIST" PDF and return ONLY a JSON \
object, no markdown, no explanation.

Layout: header boxes (Shipper, Applicant, Notify Party, Port of Loading, \
Carrier ...), a goods block ("POLYPROPYLENE RESIN / GRADE : R202P", marks \
with "NET WT.: 25KGS"), a totals table (GRADE | Quantity (BAGS) | NET \
WEIGHT (MTS) (KGS) | GROSS WEIGHT (KGS) | CBM), and a SMALL CONTAINER TABLE:
  CNT NO. | SEAL NO. | Lot No | WEIGHT | GROSS WEIGHT | GRADE
  e.g. "FFAU1502650 | YMAV499275 | 609315 | 24,000.00 | 24,115.20 | R202P"
The GRADE cell may be merged across several rows — then it applies to all of \
them.

Fields:
- "packing_list_no": the "No. & Date of Packing list" number (e.g. "3181-202609-8200-001").
- "grade": the product grade (e.g. "R202P").
- "bag_size_kg": kg per bag from "NET WT.: 25KGS" (number, e.g. 25), 0 if absent.
- "total_bags": Quantity (BAGS) total from the totals table (integer, no commas).
- "total_net_kg": total NET WEIGHT in KGS (e.g. 48000), no commas.
- "total_gross_kg": total GROSS WEIGHT in KGS (e.g. 48230.4), no commas.
- "containers": one entry for EVERY row of the container table:
  {"id": "FFAU1502650", "seal": "YMAV499275", "lot": "609315",
   "net_weight_kg": 24000.0, "gross_weight_kg": 24115.2, "grade": "R202P"}
  Numbers are plain numbers (strip thousands commas). Container ids are 4 \
letters + 7 digits with no spaces. Lot numbers are copied exactly as printed \
(keep leading zeros, do not turn them into numbers).
@@OCR_RULE@@
Return:
{"packing_list_no": "", "grade": "", "bag_size_kg": 0, "total_bags": 0,
 "total_net_kg": 0, "total_gross_kg": 0,
 "containers": [{"id": "", "seal": "", "lot": "", "net_weight_kg": 0,
                 "gross_weight_kg": 0, "grade": ""}]}"""

_OCR_RULE = (
    "\n⚠️ A container id is ALWAYS 4 LETTERS followed by 7 DIGITS. Seals and lot "
    "numbers have no fixed pattern: transcribe the exact glyphs, looking "
    "carefully at O/0, I/1, S/5, B/8, Z/2.\n"
)
PKG_LIST_PROMPT = PKG_LIST_PROMPT.replace("@@OCR_RULE@@", _OCR_RULE)


def _f(value) -> float:
    """Number from a model field that may still carry thousands commas."""
    if isinstance(value, (int, float)):
        return float(value)
    text = s(value).strip().replace(",", "")
    try:
        return float(text) if text else 0.0
    except ValueError:
        return 0.0


def extract_packing_list(pdf_path: str, mbl_container_ids: list[str] | None = None) -> dict:
    prompt = PKG_LIST_PROMPT
    if mbl_container_ids:
        prompt += (
            "\n\n⚠️ CONTAINER ID REFERENCE LIST from the MBL of this same shipment: "
            f"[{', '.join(mbl_container_ids)}]. If a container id you read is one or two "
            "characters off one of these, use the reference id."
        )
    data = call_gemini(prompt, pdf_path=pdf_path, max_output_tokens=8192, call_label="packing_list")
    if isinstance(data, list):
        data = data[0] if data else {}

    containers = []
    for c in data.get("containers", []) or []:
        cid, seal = fix_container_id(s(c.get("id")), s(c.get("seal")))
        if not cid:
            continue
        containers.append({
            "id": cid,
            "seal": seal.strip(),
            "lot": s(c.get("lot")).strip(),
            "net_weight_kg": _f(c.get("net_weight_kg")),
            "gross_weight_kg": _f(c.get("gross_weight_kg")),
            "grade": s(c.get("grade")).strip() or s(data.get("grade")).strip(),
        })

    result = {
        "packing_list_no": s(data.get("packing_list_no")).strip(),
        "grade": s(data.get("grade")).strip(),
        "bag_size_kg": _f(data.get("bag_size_kg")),
        "total_bags": int(_f(data.get("total_bags"))),
        "total_net_kg": _f(data.get("total_net_kg")),
        "total_gross_kg": _f(data.get("total_gross_kg")),
        "containers": containers,
    }
    dump_json(pdf_path, "packing_list.json", result)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# MATCHING
# ═══════════════════════════════════════════════════════════════════════════

def _match_container(cid: str, reference_ids: set[str], threshold: int = 2) -> str:
    """Maps an id read from one document onto the closest id of the other
    (OCR confusions, or a prefix cut off in a scan: a unique suffix match
    counts too). Returns cid unchanged when nothing is close."""
    if not cid or cid in reference_ids:
        return cid
    # Broken MBL: the 4-letter owner prefix is cut off / unreadable but the 7
    # digits are there. The digits decide, the damaged prefix is ignored.
    m = re.search(r"\d{7}$", cid)
    if m:
        same = [r for r in reference_ids if r.endswith(m.group(0))]
        if len(same) == 1:
            return same[0]
    best, best_d = None, threshold + 1
    for r in reference_ids:
        if len(r) != len(cid):
            continue
        d = sum(1 for a, b in zip(cid, r) if a != b)
        if d < best_d:
            best, best_d = r, d
    return best if best and best_d <= threshold else cid


def _mbl_map(mbl: dict, pl_ids: set[str]) -> dict[str, dict]:
    """MBL containers keyed by the Packing List's id spelling."""
    out = {}
    for c in mbl.get("containers", []) or []:
        cid = s(c.get("id")).strip()
        if cid:
            out[_match_container(cid, pl_ids)] = c
    return out


# ═══════════════════════════════════════════════════════════════════════════
# VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

def validate_seals(mbl: dict, pl: dict, arrival_notice: dict | None) -> list[str]:
    """Where each container's seal comes from, and what is still missing."""
    pl_conts = pl.get("containers", [])
    pl_ids = {c["id"] for c in pl_conts}
    mmap = _mbl_map(mbl, pl_ids)
    an_map = _an_seals_by_pl_id(arrival_notice["seals"], pl_ids) if arrival_notice else {}
    r = list(arrival_notice.get("warnings", [])) if arrival_notice else []
    for c in pl_conts:
        cid = c["id"]
        mseal = s((mmap.get(cid) or {}).get("seal")).strip()
        aseal = an_map.get(cid, "")
        if mseal:
            if aseal and aseal.upper() != mseal.upper():
                r.append(f"[!]  SEAL — {cid}: MBL {mseal} vs Arrival Notice {aseal} (MBL used)")
        elif aseal:
            r.append(f"[OK] SEAL — {cid}: not on the MBL, taken from the Arrival Notice ({aseal})")
        elif c["seal"]:
            where = "MBL or the Arrival Notice" if arrival_notice else "MBL (no Arrival Notice uploaded)"
            r.append(f"[!]  SEAL — {cid}: not on the {where}, taken from the Packing List ({c['seal']})")
        else:
            where = "MBL, Arrival Notice or Packing List" if arrival_notice else \
                "MBL or Packing List (no Arrival Notice uploaded)"
            r.append(f"[X]  SEAL — {cid}: no seal on the {where}")
    for cid in an_map:
        if cid not in pl_ids:
            r.append(f"[!]  SEAL — {cid}: in the Arrival Notice but not in the Packing List")
    return r


def validate(mbl: dict, pl: dict) -> list[str]:
    r = []
    carrier = s(mbl.get("carrier")).strip()
    if carrier:
        r.append(f"[OK] CARRIER — {carrier} (MBL read with: {mbl.get('_prompt_used', '?')})")
    if s(mbl.get("mbl_no")).strip():
        r.append(f"[OK] MBL No — {s(mbl.get('mbl_no')).strip()}")
    else:
        r.append("[X]  MBL No — not found on the MBL")

    pl_conts = pl.get("containers", [])
    if not pl_conts:
        r.append("[X]  PACKING LIST — no container rows found (CNT NO / SEAL NO / Lot No table)")
        return r
    pl_ids = {c["id"] for c in pl_conts}
    mmap = _mbl_map(mbl, pl_ids)
    mbl_ids = set(mmap)

    if not mbl_ids:
        r.append("[X]  CONTAINERS — none extracted from the MBL")
    for cid in sorted(pl_ids & mbl_ids):
        r.append(f"[OK] CONTAINER — {cid} matched on MBL & Packing List")
    for cid in sorted(pl_ids - mbl_ids):
        r.append(f"[!]  CONTAINER — {cid} only on the Packing List (type/bags/seal from MBL missing)")
    for cid in sorted(mbl_ids - pl_ids):
        r.append(f"[!]  CONTAINER — {cid} only on the MBL (no lot/net weight, not in output)")

    for c in pl_conts:
        cid = c["id"]
        if not is_valid_container_id(cid):
            r.append(f"[!]  CONTAINER — {cid} fails the ISO 6346 check digit, verify the number")
        if not c["lot"]:
            r.append(f"[!]  LOT — {cid}: no lot number read from the Packing List")
        if not c["net_weight_kg"]:
            r.append(f"[!]  NET WEIGHT — {cid}: blank on the Packing List")
        m = mmap.get(cid)
        if m:
            mseal = s(m.get("seal")).strip().upper()
            if c["seal"] and mseal and c["seal"].upper() != mseal:
                r.append(f"[!]  SEAL — {cid}: MBL {mseal} vs Packing List {c['seal']} (MBL used)")
            mg = num(m.get("gross_weight_kg"), 0)
            if mg and c["gross_weight_kg"] and abs(mg - c["gross_weight_kg"]) > WEIGHT_TOLERANCE_KG:
                r.append(f"[!]  GROSS — {cid}: MBL {mg:g} vs Packing List {c['gross_weight_kg']:g} (Packing List used)")

    # Totals
    net_sum = sum(c["net_weight_kg"] for c in pl_conts)
    gross_sum = sum(c["gross_weight_kg"] for c in pl_conts)
    if pl["total_net_kg"]:
        if abs(net_sum - pl["total_net_kg"]) > WEIGHT_TOLERANCE_KG:
            r.append(f"[!]  NET — container rows sum {net_sum:g} kg vs Packing List total {pl['total_net_kg']:g} kg")
        else:
            r.append(f"[OK] NET — {net_sum:g} kg across {len(pl_conts)} container(s) = Packing List total")
    if pl["total_gross_kg"]:
        if abs(gross_sum - pl["total_gross_kg"]) > WEIGHT_TOLERANCE_KG:
            r.append(f"[!]  GROSS — container rows sum {gross_sum:g} kg vs Packing List total {pl['total_gross_kg']:g} kg")
        else:
            r.append(f"[OK] GROSS — {gross_sum:g} kg = Packing List total")

    bags = [_bags(c, mmap.get(c["id"]), pl) for c in pl_conts]
    if any(b == 0 for b in bags):
        r.append("[!]  BAGS — could not determine bags for some container (no MBL count, no bag size)")
    elif pl["total_bags"]:
        if sum(bags) != pl["total_bags"]:
            r.append(f"[!]  BAGS — containers sum {sum(bags)} vs Packing List total {pl['total_bags']}")
        else:
            r.append(f"[OK] BAGS — {sum(bags)} bags = Packing List total")
    return r


# ═══════════════════════════════════════════════════════════════════════════
# ROW BUILDER
# ═══════════════════════════════════════════════════════════════════════════

def _bags(c: dict, mbl_c: dict | None, pl: dict) -> int:
    """MBL per-container bag count; else net weight / bag size."""
    from_mbl = int(num(mbl_c.get("bags"), 0)) if mbl_c else 0
    if from_mbl:
        return from_mbl
    if pl.get("bag_size_kg") and c["net_weight_kg"]:
        return int(round(c["net_weight_kg"] / pl["bag_size_kg"]))
    return 0


def _an_seals_by_pl_id(an_seals: dict | None, pl_ids: set[str]) -> dict[str, str]:
    """Arrival Notice seals keyed by the Packing List's container id."""
    return {_match_container(cid, pl_ids): seal for cid, seal in (an_seals or {}).items() if seal}


def build_rows(mbl: dict, pl: dict, external_id: str, shipping_line: str,
               ship_name: str, eta_date: str, an_seals: dict | None = None) -> list[dict]:
    external_id = s(external_id).strip()
    pl_ids = {c["id"] for c in pl.get("containers", [])}
    mmap = _mbl_map(mbl, pl_ids)
    an_map = _an_seals_by_pl_id(an_seals, pl_ids)
    shipment_type = s(mbl.get("container_type")).strip()

    rows = []
    for c in pl.get("containers", []):
        cid = c["id"]
        m = mmap.get(cid, {})
        raw_type = s(m.get("type")).strip() or shipment_type
        gross = c["gross_weight_kg"] or num(m.get("gross_weight_kg"), 0)
        lot = c["lot"]
        rows.append({
            "external_id":        external_id,
            "container_no":       cid,
            "seal_no":            s(m.get("seal")).strip() or an_map.get(cid, "") or c["seal"],
            "container_type":     normalize_container_type(raw_type) if raw_type else "",
            "product":            c["grade"] or pl.get("grade", ""),
            "bags_qty":           _bags(c, m, pl),
            "net_weight":         c["net_weight_kg"],
            "gross_weight":       gross,
            # No source document states pallets — always 0 (same as Sunrise).
            "pallet_count":       0,
            "lot_no":             lot,
            "external_container": f"{external_id}/{cid}",
            "lot_container":      f"{lot}/{cid}",
            "shipping_line":      shipping_line,
            "ship_name":          ship_name,
            "eta_date":           eta_date,
        })
    return rows
