"""
Sealogs Inbound — MBL / Packing List / Arrival Notice extraction, validation
and row-building.

The Packing List and Arrival Notice assume no fixed layout: they go to Gemini with a
layout-agnostic prompt that says WHAT to find (not where it is printed), so
master BLs, house BLs, sea waybills, any carrier's arrival notice and any
shipper's packing list all work:

  MBL            read with the CARRIER-SPECIFIC layout (platform prompt
                 library); unknown carrier -> generic MBL prompt; the dynamic
                 prompt is only a safety net when no container is found.
                 Gives container ids, per-container packages and gross.
  Packing List   package kind, total packages, total net/gross, and
                 per-container lines ONLY when the list assigns packages to
                 container numbers (most don't).
  Arrival Notice (optional) container list, per-container piece count and gross.

OP columns and where each value comes from
  Container No   Arrival Notice ids if given (cleanest print), else MBL.
                 A broken MBL id (letters hidden) is matched on its 7 digits.
  Product        always "BUNDLES" (fixed)
  Goods Type     BUNDLE / BAGS / PALLETS, from the package kind printed on the
                 documents; a generic word ("PKGS", "PACKAGES") means BUNDLE.
  Qty            packing list per container -> MBL per container -> Arrival
                 Notice per container -> shipment total when ONE container.
  Gross          same priority as Qty.
  Net            packing list per container -> packing list total when ONE
                 container. Never guessed for several containers (left blank
                 with a warning).
UI inputs (External ID, Shipping Line, Ship Name, ETA) apply to every row.
"""

import re

from clients.swiss.inbound.extractor import (
    CARRIER_DISPLAY_MAP,
    CARRIER_MBL_PROMPTS,
    GENERIC_MBL_PROMPT,
    carrier_display,
    identify_carrier,
    is_valid_container_id,
)
from clients.taewoong.inbound.extractor import _clean_containers, _f, _match_container
from helpers.doc_common import dump_json, num, s
from helpers.gemini_client import call_gemini

SHIPPING_LINE_OPTIONS = list(CARRIER_DISPLAY_MAP.keys()) + ["Other"]

FIXED_PRODUCT = "BUNDLES"
GOODS_TYPES = ("BUNDLE", "BAGS", "PALLETS")
DEFAULT_GOODS_TYPE = "BUNDLE"
TOLERANCE_KG = 1.0

# ═══════════════════════════════════════════════════════════════════════════
# SHARED PROMPT RULES
# ═══════════════════════════════════════════════════════════════════════════

_RULES = """
RULES
- A container id is 4 LETTERS + 7 DIGITS, no spaces (e.g. FBLU2201516). Some
  copies are BROKEN: the letters are cut off or hidden. Then copy exactly what
  is printed (e.g. "J4805272") — never invent or complete the letters.
- Numbers are plain numbers (strip thousands commas). ALL weights in KG: if a
  document states tonnes/MT multiply by 1000, if pounds multiply by 0.45359.
- "package kind" is the word the document uses for what is packed: copy it as
  printed (e.g. "BUNDLES", "BAGS", "PALLETS", "PKGS", "PACKAGES", "CARTONS").
- Package COUNT means number of packages. It is NOT a length in metres, a
  volume (CBM/M3) or a weight.
- Never use a shipment-wide total as one container's own figure when the
  shipment has several containers; leave that field 0 instead.
"""

_MBL_PROMPT = """You are a shipping-document data extractor. This is a Bill of Lading / \
Sea Waybill PDF (master or house BL, any carrier or forwarder, any layout, \
possibly several pages with an attached container list — read every page). \
Return ONLY a JSON object, no markdown.

Find:
- "mbl_no": the B/L number (not the booking number).
- "port_of_loading".
- "package_kind": the kind of package as printed ("PACKAGES", "BAGS", ...), "" if none.
- "total_packages": shipment total package count (e.g. from "S.T.C: 26 PACKAGES" or a TOTAL line), 0 if none.
- "total_gross_kg": shipment total gross weight in KG, 0 if none.
- "containers": EVERY container with
  {"id": "...", "seal": "...", "type": "20'OT / 40HQ ...", "packages": 0, "gross_weight_kg": 0}
  The container/seal/type may sit in a small block ("Container | Seals | Type | Packages | Weight"),
  in a table row, or in a marks column. "packages" and "gross_weight_kg" are that container's OWN
  values when printed, else 0. Lines such as "SAY TOTAL ONE (1) CONTAINER ONLY" or shipment
  totals are not containers.
@@RULES@@
Return:
{"mbl_no": "", "port_of_loading": "", "package_kind": "", "total_packages": 0,
 "total_gross_kg": 0,
 "containers": [{"id": "", "seal": "", "type": "", "packages": 0, "gross_weight_kg": 0}]}""".replace("@@RULES@@", _RULES)

_PL_PROMPT = """You are a shipping-document data extractor. This is a PACKING LIST \
(any shipper, any layout; may be a "packing/weight list" with one line per \
package, or a summary). Return ONLY a JSON object, no markdown. Read every page.

Find:
- "package_kind": the word used for what is packed ("BUNDLES", "BAGS", "PALLETS", "PKGS", "PACKAGES", ...) — from the totals line, the "SAY TOTAL ..." line, headers or the goods description; "" if none.
- "total_packages": total number of packages (e.g. "26 PKGS", "SAY TOTAL TWENTY SIX (26) PKG"). If no total is printed, count the package/line numbers. NOT the length/quantity-in-metres column.
- "total_net_kg": total net weight in KG.  - "total_gross_kg": total gross weight in KG.
  Use the printed totals; if none, sum the lines.
- "containers": ONLY if the packing list itself assigns lines or totals to container numbers
  (e.g. a "Container No." column or per-container sections), one entry per container:
  {"id": "...", "packages": 0, "net_weight_kg": 0, "gross_weight_kg": 0}
  (sum that container's lines). Otherwise return an empty array — do NOT invent containers.
@@RULES@@
Return:
{"package_kind": "", "total_packages": 0, "total_net_kg": 0, "total_gross_kg": 0,
 "containers": [{"id": "", "packages": 0, "net_weight_kg": 0, "gross_weight_kg": 0}]}""".replace("@@RULES@@", _RULES)

_AN_PROMPT = """You are a shipping-document data extractor. This is an ARRIVAL NOTICE \
(any shipping line / agent, any layout). Return ONLY a JSON object, no markdown. \
Read every page; ignore terms-and-conditions text.

Find:
- "package_kind": kind of package if stated ("Packages", "Bags", ...), "" if none.
- "containers": EVERY container with
  {"id": "...", "seal": "...", "type": "...", "packages": 0, "gross_weight_kg": 0}
  where "packages" is that container's own piece/package count and
  "gross_weight_kg" its own gross weight (columns like "CNTR PIECE COUNT",
  "CNTR GROSS WEIGHT"). Not the "Total" row.
@@RULES@@
Return:
{"package_kind": "", "containers": [{"id": "", "seal": "", "type": "", "packages": 0, "gross_weight_kg": 0}]}""".replace("@@RULES@@", _RULES)


def _as_dict(data) -> dict:
    if isinstance(data, list):
        return data[0] if data and isinstance(data[0], dict) else {}
    return data if isinstance(data, dict) else {}


# ═══════════════════════════════════════════════════════════════════════════
# EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════

def _norm_containers(items) -> list[dict]:
    out = []
    for c in items or []:
        out.append({
            "id": s(c.get("id")),
            "seal": s(c.get("seal")).strip(),
            "type": s(c.get("type")).strip(),
            "packages": int(_f(c.get("packages"))),
            "net_weight_kg": _f(c.get("net_weight_kg")),
            "gross_weight_kg": _f(c.get("gross_weight_kg")),
        })
    return out


def _read_with(prompt: str, pdf_path: str, label: str) -> dict:
    """One MBL read. The platform's carrier prompts return "bags" per
    container; here that is the package count, so it is mapped to "packages"."""
    data = _as_dict(call_gemini(prompt, pdf_path=pdf_path, max_output_tokens=16384, call_label=label))
    data["containers"] = [
        {"id": s(c.get("id")), "seal": s(c.get("seal")).strip(), "type": s(c.get("type")).strip(),
         "packages": int(_f(c.get("packages", c.get("bags")))),
         "gross_weight_kg": _f(c.get("gross_weight_kg"))}
        for c in data.get("containers", []) or []
    ]
    return data


def extract_mbl(pdf_path: str) -> dict:
    """MBL is read with the carrier-specific layout (Swiss's carrier prompt
    library, shared by every client). A carrier the platform doesn't know gets
    the generic MBL prompt. The layout-agnostic prompt below is only a safety
    net, used when that read finds no containers."""
    carrier = identify_carrier(pdf_path)
    if carrier in CARRIER_MBL_PROMPTS:
        prompt, used = CARRIER_MBL_PROMPTS[carrier], f"{carrier} carrier-specific layout"
    else:
        prompt, used = GENERIC_MBL_PROMPT, f"generic layout (carrier {carrier} has no specific layout yet)"

    data = _read_with(prompt, pdf_path, "mbl")
    if not _clean_containers(data):
        safety = _read_with(_MBL_PROMPT, pdf_path, "mbl_dynamic")
        if _clean_containers(safety):
            data, used = {**data, **safety}, used + " found no containers, dynamic read used instead"

    data["carrier"] = carrier
    data["_prompt_used"] = used
    data["package_kind"] = s(data.get("package_kind")).strip()
    data["total_packages"] = int(_f(data.get("total_packages")))
    data["total_gross_kg"] = _f(data.get("total_gross_kg"))
    dump_json(pdf_path, "mbl.json", data)
    print(f"  [MBL] Carrier {carrier} - {used}")
    return data


def extract_packing_list(pdf_path: str, reference_ids: list[str] | None = None) -> dict:
    prompt = _PL_PROMPT
    if reference_ids:
        prompt += (f"\n\nContainer ids of this shipment (from other documents): [{', '.join(reference_ids)}]. "
                   "Only if the packing list lists containers, use these spellings.")
    data = _as_dict(call_gemini(prompt, pdf_path=pdf_path, max_output_tokens=8192, call_label="packing_list"))
    result = {
        "package_kind": s(data.get("package_kind")).strip(),
        "total_packages": int(_f(data.get("total_packages"))),
        "total_net_kg": _f(data.get("total_net_kg")),
        "total_gross_kg": _f(data.get("total_gross_kg")),
        "containers": _norm_containers(data.get("containers")),
    }
    _clean_containers(result)
    dump_json(pdf_path, "packing_list.json", result)
    return result


def extract_arrival_notice(pdf_path: str) -> dict:
    data = _as_dict(call_gemini(_AN_PROMPT, pdf_path=pdf_path, max_output_tokens=4096, call_label="arrival_notice"))
    result = {"package_kind": s(data.get("package_kind")).strip(),
              "containers": _norm_containers(data.get("containers"))}
    _clean_containers(result)
    dump_json(pdf_path, "arrival_notice.json", result)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# RECONCILIATION
# ═══════════════════════════════════════════════════════════════════════════

def goods_type_from(*kinds: str) -> tuple[str, str]:
    """(goods_type, note). First document whose package kind is recognisable
    wins; a generic word / nothing means BUNDLE (Sealogs ships bundles)."""
    printed = [k.strip() for k in kinds if k and k.strip()]
    for k in printed:
        u = k.upper()
        if re.search(r"BUNDLE|BDL|BNDL", u):
            return "BUNDLE", ""
        if re.search(r"BAG|SACK", u):
            return "BAGS", ""
        if re.search(r"PALLET|PLT|SKID", u):
            return "PALLETS", ""
    if printed:
        return DEFAULT_GOODS_TYPE, (f"package kind \"{printed[0]}\" is generic, "
                                    f"{DEFAULT_GOODS_TYPE} assumed")
    return DEFAULT_GOODS_TYPE, f"no package kind on the documents, {DEFAULT_GOODS_TYPE} assumed"


def reconcile(mbl: dict, pl: dict, an: dict | None) -> dict:
    """One record per container with qty/gross/net resolved and the notes
    explaining anything missing."""
    mbl_c = mbl.get("containers", [])
    an_c = (an or {}).get("containers", [])
    # Reference ids: AN print is cleanest, then MBL.
    base = [c["id"] for c in an_c if c["id"]] or [c["id"] for c in mbl_c if c["id"]]
    ids = list(dict.fromkeys(_match_container(i, set(base)) for i in base))
    refs = set(ids)

    def by_id(items):
        return {_match_container(c["id"], refs): c for c in items if c["id"]}

    mmap, amap, pmap = by_id(mbl_c), by_id(an_c), by_id(pl.get("containers", []))
    # MBL-only ids not covered by the AN list
    for cid in mmap:
        if cid not in refs:
            ids.append(cid)
            refs.add(cid)

    single = len(ids) == 1
    notes, records = [], []
    for cid in ids:
        m, a, p = mmap.get(cid, {}), amap.get(cid, {}), pmap.get(cid, {})

        def pick(key, src):
            for d, label in src:
                v = d.get(key) if d else 0
                if v:
                    return v, label
            return 0, ""

        qty, qsrc = pick("packages", [(p, "Packing List"), (m, "MBL"), (a, "Arrival Notice")])
        gross, gsrc = pick("gross_weight_kg", [(p, "Packing List"), (m, "MBL"), (a, "Arrival Notice")])
        net, nsrc = pick("net_weight_kg", [(p, "Packing List")])

        if single:
            if not qty:
                qty, qsrc = (pl.get("total_packages") or mbl.get("total_packages") or 0), "shipment total"
            if not gross:
                gross, gsrc = (pl.get("total_gross_kg") or mbl.get("total_gross_kg") or 0), "shipment total"
            if not net:
                net, nsrc = pl.get("total_net_kg") or 0, "Packing List total"
        records.append({"id": cid, "qty": int(qty), "gross": gross, "net": net,
                        "src": {"qty": qsrc, "gross": gsrc, "net": nsrc}})
    return {"records": records, "single": single}


# ═══════════════════════════════════════════════════════════════════════════
# VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

def validate(mbl: dict, pl: dict, an: dict | None) -> list[str]:
    r = []
    if s(mbl.get("mbl_no")).strip():
        r.append(f"[OK] B/L No — {s(mbl.get('mbl_no')).strip()} (MBL read with: {mbl.get('_prompt_used', '?')})")
    else:
        r.append("[X]  B/L No — not found on the MBL")
    if an is None:
        r.append("[!]  ARRIVAL NOTICE — not uploaded, container ids and per-container figures come from the MBL / Packing List only")

    rec = reconcile(mbl, pl, an)
    records = rec["records"]
    if not records:
        r.append("[X]  CONTAINERS — none found on the MBL or Arrival Notice")
        return r

    mbl_ids = {_match_container(c["id"], {x["id"] for x in records}) for c in mbl.get("containers", []) if c["id"]}
    an_ids = {_match_container(c["id"], {x["id"] for x in records}) for c in (an or {}).get("containers", []) if c["id"]}
    for x in records:
        cid = x["id"]
        if not is_valid_container_id(cid):
            r.append(f"[!]  CONTAINER — {cid} is not a valid container number (letters missing or check digit fails), verify it")
        else:
            r.append(f"[OK] CONTAINER — {cid}")
        if an is not None and cid not in an_ids:
            r.append(f"[!]  CONTAINER — {cid} is on the MBL but not on the Arrival Notice")
        if an is not None and cid not in mbl_ids:
            r.append(f"[!]  CONTAINER — {cid} is on the Arrival Notice but not on the MBL")
        for key, label in (("qty", "QTY"), ("gross", "GROSS"), ("net", "NET")):
            if not x[key]:
                reason = "several containers and no per-container figure on the documents, left blank" \
                    if not rec["single"] else "not found on any document"
                r.append(f"[X]  {label} — {cid}: {reason}")
            elif x["src"][key] and x["src"][key] != "Packing List":
                r.append(f"[OK] {label} — {cid}: {x[key]:g} from the {x['src'][key]}")

    tot_q = sum(x["qty"] for x in records)
    tot_g = sum(x["gross"] for x in records)
    tot_n = sum(x["net"] for x in records)
    for label, got, ref_pl, ref_mbl in (
        ("QTY", tot_q, pl.get("total_packages"), mbl.get("total_packages")),
        ("GROSS", tot_g, pl.get("total_gross_kg"), mbl.get("total_gross_kg")),
        ("NET", tot_n, pl.get("total_net_kg"), 0),
    ):
        for ref, doc in ((ref_pl, "Packing List"), (ref_mbl, "MBL")):
            if ref and got:
                if abs(got - ref) > (0 if label == "QTY" else TOLERANCE_KG):
                    r.append(f"[!]  {label} — output total {got:g} vs {doc} total {ref:g}")
                else:
                    r.append(f"[OK] {label} — output total {got:g} = {doc} total")

    gt, note = goods_type_from(pl.get("package_kind"), mbl.get("package_kind"), (an or {}).get("package_kind"))
    r.append(f"[{'!' if note else 'OK'}]  GOODS TYPE — {gt}" + (f" ({note})" if note else ""))
    return r


# ═══════════════════════════════════════════════════════════════════════════
# ROW BUILDER
# ═══════════════════════════════════════════════════════════════════════════

def build_rows(mbl: dict, pl: dict, an: dict | None, external_id: str, shipping_line: str,
               ship_name: str, eta_date: str) -> list[dict]:
    external_id = s(external_id).strip()
    goods_type, _ = goods_type_from(pl.get("package_kind"), mbl.get("package_kind"), (an or {}).get("package_kind"))
    rows = []
    for x in reconcile(mbl, pl, an)["records"]:
        rows.append({
            "external_id":        external_id,
            "external_container": f"{external_id}/{x['id']}",
            "container_no":       x["id"],
            "shipping_line":      shipping_line,
            "ship_name":          ship_name,
            "product":            FIXED_PRODUCT,
            "goods_type":         goods_type,
            "qty":                x["qty"],
            "gross_weight":       x["gross"],
            "net_weight":         x["net"],
            "eta_date":           eta_date,
        })
    return rows
