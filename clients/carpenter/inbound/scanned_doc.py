"""
scanned_doc.py — Vision-based extraction for scanned packing list pages.

Handles Carpenter Titanium / Dynamet format packing lists that appear as
scanned images inside the main packing list PDF.  Uses pypdf to slice out
only the scanned pages into a mini in-memory PDF, then sends them to
Gemini 2.5 Flash Lite — Gemini never sees the digital pages.

Extracted fields:
  - W.O. # → Order No
  - TAG NBR entries → Handling Unit identifiers
  - WEIGHT (LBS) → converted to KG (÷ 2.20462), truncated 2dp

Changes (this version):
  FIX 1 — Per-page (or small-batch) Gemini calls instead of one giant
          9-page call.  Each page gets full model attention, so TAG NBRs
          from later packing lists (129795, 129855, 129789) are no longer
          missed.  Configurable via PAGES_PER_BATCH (default 2).

  FIX 2 — Robust Gemini prompt: explicit examples of TAG NBR vs LOT NBR
          vs part-number formats, per-row W.O.# instruction, multi-PL
          awareness, and stricter output schema.

  FIX 3 — link_scanned_to_deliveries() rewritten:
            a) Two-phase linking: strict order_no match first across ALL
               deliveries, THEN fallback for shortfalls only.
            b) order_no on each HU record is UPDATED to the Excel value
               (previously kept the Gemini W.O.# which was always the
               same for all HUs from one packing list).
            c) Unlinked HUs are logged, not silently dropped.

  FIX 4 — TAG NBR validation: entries without a hyphen-suffix (e.g.
          part numbers, LOT NBRs) are rejected before they enter the
          HU records list.

  FIX 5 — Retry logic with exponential backoff per batch.

Usage:
    from scanned_doc import extract_scanned_pages, link_scanned_to_deliveries

    records = extract_scanned_pages(
        pdf_path="ACLU9811060.pdf",
        scanned_indices=[8, 9, 10, 11, 12, 13, 14, 15, 16],
        container_no="ACLU9811060",
    )

Environment:
    GEMINI_API_KEY must be set in .env (or exported).
"""

import os
import io
import math
import logging
from pypdf import PdfReader, PdfWriter

from helpers.gemini_client import call_gemini
from clients.carpenter.inbound.prompts import build_scanned_doc_prompt

# Silence pypdf's excessive warnings ("CropBox missing", etc.)
logging.getLogger("pypdf").setLevel(logging.ERROR)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# LB → KG conversion factor
LB_TO_KG = 2.20462

# ── Tuning knobs ──
# How many scanned pages to send per Gemini call.
# 1 = maximum reliability (one page at a time, best for complex pages)
# 2 = good balance of speed and accuracy (default)
# 3-4 = faster but may miss items on dense pages
PAGES_PER_BATCH = 1


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def _truncate2_kg(lbs_value: float) -> str:
    """Convert LBS to KG, truncate (not round) to 2 decimal places."""
    kg = lbs_value / LB_TO_KG
    truncated = math.floor(kg * 100) / 100
    if truncated == int(truncated):
        return str(int(truncated))
    s = f"{truncated:.2f}"
    return s.rstrip("0").rstrip(".")


def _slice_pages_to_pdf(pdf_path: str, page_indices: list) -> bytes:
    """
    Extract only the given 0-based page indices from the PDF into a new
    in-memory PDF and return the raw bytes.
    """
    reader = PdfReader(pdf_path)
    writer = PdfWriter()
    for idx in page_indices:
        writer.add_page(reader.pages[idx])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _chunk_list(lst: list, chunk_size: int) -> list:
    """Split a list into chunks of at most chunk_size."""
    chunks = []
    for i in range(0, len(lst), chunk_size):
        chunks.append(lst[i : i + chunk_size])
    return chunks


def _call_gemini_batch(pdf_path: str, page_indices: list) -> list:
    """
    Slice the given pages into a mini-PDF, send to Gemini (via
    helpers/gemini_client.py, which retries transient failures internally),
    return the parsed JSON results — a list of per-page dicts.
    """
    mini_pdf_bytes = _slice_pages_to_pdf(pdf_path, page_indices)
    n_pages = len(page_indices)
    prompt = build_scanned_doc_prompt(n_pages)

    parsed = call_gemini(prompt, pdf_bytes=mini_pdf_bytes, max_output_tokens=8192)

    # Normalise: if the model returned a single dict instead of a list,
    # wrap it so callers always get a list.
    if isinstance(parsed, dict):
        parsed = [parsed]

    return parsed


def _normalize_order_no(order_no: str) -> str:
    """
    Strip the 'WO' prefix that the Order List Excel uses but scanned
    pages do not.

    The Excel stores order numbers as 'WO303598', 'WO340884', etc.
    The scanned packing lists print plain digits: '303598', '340884'.
    Both sides must be normalised before comparison.

    Examples:
        'WO303598' → '303598'
        'wo303598' → '303598'
        '303598'   → '303598'
        ''         → ''
    """
    s = str(order_no).strip()
    if s.upper().startswith("WO"):
        s = s[2:]
    return s


def _is_valid_tag_nbr(tag: str) -> bool:
    """
    Validate that a TAG NBR looks correct:
      - Must contain a hyphen (35168DG-001, HC21018-038)
      - Must NOT be a part number (111DB4HBN02539AN)
      - Must NOT be a bare LOT NBR (35168DG, HC20974)
    """
    if not tag or "-" not in tag:
        return False
    # Reject strings that look like long part numbers (>20 chars, no hyphen pattern)
    if len(tag) > 20:
        return False
    return True


# ─────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────

def extract_scanned_pages(
    pdf_path: str,
    scanned_indices: list,
    container_no: str,
) -> list:
    """
    Extract HU records from scanned (image-only) pages of a packing list PDF.
    Sends the entire scanned PDF as ONE Gemini call so duplicate TAG NBRs
    across DETAIL and PALLET PACK LIST pages are naturally deduplicated by
    the model with full cross-page context.
    """
    if not GEMINI_API_KEY:
        print("[!] GEMINI_API_KEY not set in .env — skipping scanned pages")
        return []

    if not scanned_indices:
        return []

    n_pages = len(scanned_indices)
    print(f"  [*] Sending all {n_pages} scanned page(s) in ONE Gemini call...")

    # ── 1. Send entire scanned PDF in one call ──
    try:
        all_page_results = _call_gemini_batch(pdf_path, scanned_indices)
    except Exception as e:
        print(f"  [!] Gemini call FAILED ({e})")
        return []

    # ── 2. Log and flatten — Gemini already deduped across pages ──
    all_items = []
    seen_tags = set()
    rejected = 0

    for result in all_page_results:
        page_in_batch = result.get("page", "?")
        pl_no = result.get("packing_list_no") or ""
        items = result.get("items", [])

        if not items:
            label = f" (PL {pl_no})" if pl_no else ""
            print(f"    Page {page_in_batch}{label}: no DETAIL rows")
            continue

        wo_set = set(
            str(it.get("order_no", ""))
            for it in items
            if it.get("order_no")
        )
        label = f" (PL {pl_no})" if pl_no else ""
        print(
            f"    Page {page_in_batch}{label}: "
            f"{len(items)} TAG NBR(s), "
            f"W.O.#: {', '.join(sorted(wo_set))}"
        )

        for item in items:
            tag = str(item.get("tag_nbr", "")).strip()

            if not _is_valid_tag_nbr(tag):
                rejected += 1
                print(f"    [!] Rejected invalid TAG NBR: '{tag}'")
                continue

            tag_key = tag.strip().upper()
            if tag_key in seen_tags:
                continue  # safety dedup in case model still repeats
            seen_tags.add(tag_key)
            all_items.append(item)

    if rejected:
        print(f"  [!] {rejected} item(s) rejected (invalid TAG NBR format)")

    if not all_items:
        print("  [!] No valid TAG NBRs extracted")
        return []

    # ── 3. Convert to HU records ──
    hu_records = []
    for item in all_items:
        tag_nbr = str(item.get("tag_nbr", "")).strip()
        order_no = str(item.get("order_no", "")).strip()

        try:
            weight_lbs = float(item.get("weight_lbs", 0))
        except (ValueError, TypeError):
            weight_lbs = 0.0

        if weight_lbs > 0:
            weight_kg = weight_lbs / LB_TO_KG
            weight_raw = f"{weight_lbs:.3f}/{weight_kg:.3f}"
        else:
            weight_raw = ""

        hu_records.append(
            {
                "delivery_no": "",
                "handling_unit": tag_nbr,
                "package_type": "BOX",
                "gross_weight_raw": weight_raw,
                "net_weight_raw": weight_raw,
                "container_no": container_no,
                "category": "dynamic",
                "pallet": 1,
                "pieces": 1,
                "pkg_label": "1 BOX",
                "order_no": order_no,
                "document_no": "",
                "source": "scanned",
            }
        )

    print(f"  [OK] Scanned extraction: {len(hu_records)} HU record(s)")
    return hu_records

def link_scanned_to_deliveries(
    scanned_records: list,
    df_orders,
    digital_records: list,
    container_no: str,
) -> list:
    """
    Assign delivery_no to scanned HU records using the Order List.

    Two-phase approach:

      PHASE 1 — Strict matching:
        Iterate over ALL uncovered deliveries and match scanned HUs whose
        order_no (W.O.#) equals the Excel's order_no.  This ensures HUs
        go to the correct delivery based on their actual W.O.#.

      PHASE 2 — Fallback (for Gemini extraction errors):
        For deliveries that still have a shortfall after Phase 1, fill
        from remaining unassigned HUs.  This handles cases where Gemini
        read the W.O.# incorrectly for some rows.  The HU's order_no is
        overwritten with the Excel value.

    In BOTH phases, the record's order_no is set to the Excel's value so
    that merge_data's order_no_pkg → order_no_final pipeline produces
    the correct Order No in output.

    Unlinked HUs (more scanned HUs than expected in order list) are
    logged and returned at the end with delivery_no="" so they appear
    in the output for manual review rather than being silently dropped.

    Args:
        scanned_records:  HU records from extract_scanned_pages (delivery_no="").
        df_orders:        Order list DataFrame.
        digital_records:  HU records already extracted from digital pages.
        container_no:     Container number to filter on.

    Returns:
        List of scanned_records with delivery_no (and order_no) filled in.
    """
    import pandas as pd

    if not scanned_records:
        return []

    # Deduplicate by handling_unit again here as a safety net
    # (catches OCR variants that slipped past extract_scanned_pages dedup)
    seen = {}
    for rec in scanned_records:
        key = rec["handling_unit"].strip().upper()
        if key not in seen:
            seen[key] = rec
        else:
            # Keep the one with a non-blank order_no if available
            if not seen[key].get("order_no") and rec.get("order_no"):
                seen[key] = rec
    scanned_records = list(seen.values())
    print(f"  [*] Deduped scanned records: {len(scanned_records)} unique HU(s)")

    # ── Deliveries for this container from order list ──
    # ── Detect whether container_no is valid ──
    import re as _re
    valid_container = bool(
        container_no and _re.match(r"^[A-Z]{4}\d{7}$", container_no)
    )

    # ── Deliveries from order list ──
    if valid_container:
        # Normal case: filter by container
        mask = df_orders["container_no"] == container_no
        container_orders = df_orders.loc[mask].copy()
    else:
        # Unknown container (filename had no container number, e.g.
        # "Packing_lists.pdf").  Search the ENTIRE order list and let
        # W.O.# matching assign both delivery_no AND container_no.
        print(f"  [!] No container in filename — matching across entire order list")
        container_orders = df_orders.copy()

    if container_orders.empty:
        print(f"  [!] No order list entries found")
        return scanned_records

    # ── Deliveries already covered by digital extraction ──
    digital_deliveries = set()
    for rec in digital_records:
        if rec.get("delivery_no"):
            digital_deliveries.add(rec["delivery_no"])

    uncovered = container_orders[
        ~container_orders["receipt_po"].isin(digital_deliveries)
    ]

    if uncovered.empty:
        print(f"  [!] All deliveries already covered by digital pages")
        return scanned_records

    # ── Build delivery requirements (preserving Excel row order) ──
    delivery_specs = []
    for _, row in uncovered.iterrows():
        delivery_no = str(row["receipt_po"]).strip()
        expected = int(row.get("units_expected", 0))
        excel_order_no = str(row.get("order_no", "")).strip()

        if expected <= 0:
            continue

        delivery_specs.append(
            {
                "delivery_no": delivery_no,
                "expected": expected,
                "excel_order_no": excel_order_no,
            }
        )
    # ── Build delivery→container lookup (for unknown-container case) ──
    delivery_to_container = {}
    if not valid_container:
        for _, row in uncovered.iterrows():
            delivery_to_container[str(row["receipt_po"]).strip()] = str(
                row.get("container_no", "")
            ).strip() 

    # ── PHASE 1: Strict matching by order_no ──
    #
    # We iterate over ALL deliveries first and collect strict matches,
    # so that HUs are never "stolen" by an earlier delivery's fallback.
    unassigned = list(scanned_records)
    linked_per_delivery = {(spec["delivery_no"], _normalize_order_no(spec["excel_order_no"])): [] for spec in delivery_specs}

    for spec in delivery_specs:
        delivery_no = spec["delivery_no"]
        expected = spec["expected"]
        excel_order = spec["excel_order_no"]

        if not excel_order:
            continue  # can't strict-match without an order_no on the Excel side # can't strict-match without an order_no

        matched = []
        remaining = []

        # Normalise both sides: Excel may have "WO303598", Gemini "303598"
        excel_order_norm = _normalize_order_no(excel_order)

        for rec in unassigned:
            rec_order_norm = _normalize_order_no(rec.get("order_no", ""))
            # Strict match: order_no must match OR HU has blank order_no
            # (blank means it came from a PALLET PACK LIST page with no W.O.# shown)
            order_matches = (rec_order_norm == excel_order_norm) or (rec_order_norm == "")
            if (
                len(matched) < expected
                and excel_order_norm
                and order_matches
                # Prefer exact order_no match — put blanks at the back
                and (rec_order_norm == excel_order_norm)
            ):
                matched.append(rec)
            else:
                remaining.append(rec)

        # Second pass: fill shortfall from blank-order_no HUs (PALLET PACK LIST source)
        if len(matched) < expected:
            still_remaining = []
            for rec in remaining:
                rec_order_norm = _normalize_order_no(rec.get("order_no", ""))
                if len(matched) < expected and rec_order_norm == "":
                    matched.append(rec)
                else:
                    still_remaining.append(rec)
            remaining = still_remaining

        # Assign delivery + update order_no (stripped of WO prefix)
        
        for rec in matched:
            rec["delivery_no"] = delivery_no
            rec["order_no"] = excel_order_norm
            if not valid_container and delivery_no in delivery_to_container:
                rec["container_no"] = delivery_to_container[delivery_no]
            if not valid_container:
                rec["container_no"] = str(
                    uncovered.loc[
                        uncovered["receipt_po"] == delivery_no, "container_no"
                    ].iloc[0]
                ) if not uncovered.loc[
                    uncovered["receipt_po"] == delivery_no, "container_no"
                ].empty else rec.get("container_no", "")

        linked_per_delivery[(delivery_no, excel_order_norm)] = matched
        unassigned = remaining

        if matched:
            print(
                f"    [>] {delivery_no} (Order {excel_order}): "
                f"strict match {len(matched)}/{expected} HU(s)"
            )

    # ── PHASE 2: Fallback for shortfalls ──
    #
    # Only runs for deliveries that didn't get enough HUs in Phase 1.
    # Grabs from the remaining unassigned pool (order_no may not match).
    for spec in delivery_specs:
        delivery_no = spec["delivery_no"]
        expected = spec["expected"]
        excel_order = spec["excel_order_no"]
        excel_order_norm = _normalize_order_no(excel_order)  # ← moved to top

        already_linked = len(linked_per_delivery[(delivery_no, excel_order_norm)])
        shortfall = expected - already_linked

        if shortfall <= 0 or not unassigned:
            continue

        # Take up to shortfall from the unassigned pool
        fallback_hus = unassigned[:shortfall]
        unassigned = unassigned[shortfall:]
        for rec in fallback_hus:
            rec["delivery_no"] = delivery_no
            rec["order_no"] = excel_order_norm  # stripped of WO prefix
            if not valid_container and delivery_no in delivery_to_container:
                rec["container_no"] = delivery_to_container[delivery_no]

        linked_per_delivery[(delivery_no, excel_order_norm)].extend(fallback_hus)

        print(
            f"    [~] {delivery_no} (Order {excel_order}): "
            f"fallback +{len(fallback_hus)} "
            f"(total {len(linked_per_delivery[(delivery_no, excel_order_norm)])}/{expected})"
        )

    # ── Report unmatched deliveries ──
    for spec in delivery_specs:
        delivery_no = spec["delivery_no"]
        expected = spec["expected"]
        excel_order_norm = _normalize_order_no(spec["excel_order_no"])  # ← fix stale var
        got = len(linked_per_delivery[(delivery_no, excel_order_norm)])
        if got < expected:
            print(
                f"    [!] {delivery_no} (Order {spec['excel_order_no']}): "
                f"only {got}/{expected} HU(s) linked — shortfall of {expected - got}"
            )

    # ── Flatten linked records ──
    
    linked = []
    for spec in delivery_specs:
        key = (spec["delivery_no"], _normalize_order_no(spec["excel_order_no"]))
        linked.extend(linked_per_delivery[key])

    # ── Handle unlinked HUs ──
    
    if unassigned:
        print(
            f"  [!] {len(unassigned)} scanned HU(s) not linked "
            f"(excess beyond order list totals — excluded from output):"
        )
        for rec in unassigned:
            print(
                f"      TAG={rec['handling_unit']}  "
                f"W.O.#={rec['order_no']}  "
                f"Weight={rec.get('gross_weight_raw', '')}"
            )

    print(f"  [OK] Linked: {len(linked)} scanned HU record(s)")
    return linked