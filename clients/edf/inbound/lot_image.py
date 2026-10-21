"""
EDF Inbound (ED&F Man) — lot table image extraction (new packing list format).

The new Excel carries no real bags-per-lot data; that comes from a picture of
the supplier's lot table (Container # / Lot # / Production date / Expiration
date / Bags #). The user uploads it, or it is the image embedded in the
workbook. Gemini reads container, lot and bags only; the result replaces each
container's lots from the Excel.
"""

import re

from helpers.doc_common import fix_container_id, num, s

_CONTAINER_ID_RE = re.compile(r"^[A-Z]{4}\d{7}$")

_GEMINI_PROMPT = """\
This image is a lot table with the columns: Container #, Lot #, Production
date, Expiration date, Bags #. Return EVERY data row, in order, as JSON:
[{"container_no": "TLLU8629969", "lot": "163", "bags": 400}]

- container_no: 4 uppercase letters + 7 digits, no spaces.
- lot: the Lot # exactly as printed.
- bags: integer from the Bags # column.
- Do NOT include the TOTAL row, headers, dates, stamps or signatures.
- Transcribe carefully (O vs 0, I vs 1, S vs 5, B vs 8). Do not skip or merge rows.
Return ONLY the JSON array.
"""


def _check_digit_ok(cid: str) -> bool:
    """ISO 6346 check digit of an 11-char container number."""
    if not _CONTAINER_ID_RE.match(cid):
        return False
    values, n = {}, 10
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if n % 11 == 0:
            n += 1
        values[ch] = n
        n += 1
    total = sum((values[ch] if ch.isalpha() else int(ch)) * 2 ** i for i, ch in enumerate(cid[:10]))
    return total % 11 % 10 == int(cid[10])


def _mime(path: str) -> str:
    with open(path, "rb") as f:
        head = f.read(12)
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def extract_lot_image(path: str) -> dict:
    """Returns {"lots": {container_id: [(lot, bags), ...]}, "warnings": [...]}."""
    from helpers.gemini_client import call_gemini

    raw = call_gemini(_GEMINI_PROMPT, pdf_path=path, mime_type=_mime(path),
                      max_output_tokens=8192, call_label="edf_lot_image")
    if isinstance(raw, dict):
        raw = next((v for v in raw.values() if isinstance(v, list)), [raw])

    lots: dict[str, list[tuple[str, int]]] = {}
    warnings: list[str] = []
    for item in raw or []:
        cid, _ = fix_container_id(s(item.get("container_no")))
        if not _CONTAINER_ID_RE.match(cid):
            warnings.append(f"[!]  LOT IMAGE — ignored row with invalid container {item.get('container_no')!r}")
            continue
        lot = s(item.get("lot")).strip()
        bags = int(num(s(item.get("bags")).replace(",", ""), 0))
        if not lot or bags <= 0:
            warnings.append(f"[!]  LOT IMAGE — {cid}: unreadable lot/bags row (lot={lot!r}, bags={bags})")
            continue
        lots.setdefault(cid, []).append((lot, bags))
    if not lots:
        raise RuntimeError("No container/lot/bags rows could be read from the lot image")
    return {"lots": lots, "warnings": warnings}


def apply_lot_image(packing_list: dict, image: dict) -> list[str]:
    """Replaces each container's lots with the image's. Returns validation lines."""
    results = list(image["warnings"])
    image_lots = image["lots"]
    n_rows = sum(len(v) for v in image_lots.values())
    results.append(f"[OK] LOT IMAGE — {n_rows} lot row(s) read for {len(image_lots)} container(s)")

    # A one-character difference between an Excel container and an image
    # container that match nothing else is a typo on one side (e.g. MSNU8048169
    # vs MSBU8048169): treat them as the same container, keeping whichever ID
    # has a valid ISO 6346 check digit (the Excel's when neither / both do).
    excel_all = {c["container_id"] for c in packing_list["containers"]}
    unmatched_img = [i for i in image_lots if i not in excel_all]
    for c in packing_list["containers"]:
        cid = c["container_id"]
        if cid in image_lots:
            continue
        close = [i for i in unmatched_img if len(i) == len(cid) and sum(a != b for a, b in zip(i, cid)) == 1]
        if len(close) != 1:
            continue
        img_id = close[0]
        unmatched_img.remove(img_id)
        use = img_id if _check_digit_ok(img_id) and not _check_digit_ok(cid) else cid
        if use != img_id:
            image_lots[use] = image_lots.pop(img_id)
        c["container_id"] = use
        results.append(f"[!]  LOT IMAGE — Packing List has {cid}, image has {img_id}: treated as the same "
                       f"container, using {use} (check digit {'valid' if _check_digit_ok(use) else 'not verifiable'}). "
                       f"Please confirm.")

    excel_ids = set()
    for c in packing_list["containers"]:
        cid = c["container_id"]
        excel_ids.add(cid)
        if cid in image_lots:
            c["lots"] = image_lots[cid]
        else:
            c["lots"] = [("", c["bags"])]
            results.append(f"[!]  LOT IMAGE — {cid}: not found in the lot image, Lot left blank")
    # A container only the image lists still gets its rows. Shipment-level
    # fields (B/L, type, product, contract, origin, supplier) are copied from
    # the Packing List's first container; bags = the image's lot total.
    template = packing_list["containers"][0] if packing_list["containers"] else None
    for cid, lots in image_lots.items():
        if cid in excel_ids:
            continue
        if template is None:
            results.append(f"[!]  LOT IMAGE — {cid}: in the image but the Packing List has no containers, ignored")
            continue
        packing_list["containers"].append({**template, "container_id": cid, "bags": sum(b for _, b in lots),
                                           "lots": lots, "quantity_mt": 0, "bag_size_kg": 0})
        results.append(f"[!]  LOT IMAGE — {cid}: in the image but not in the Packing List, rows added using "
                       f"the shipment details of {template['container_id']}")
    return results
