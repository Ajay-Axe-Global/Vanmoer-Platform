"""
arrival_notice.py — Gemini-vision-based extraction for Arrival Notice PDFs.

Because Arrival Notice documents come from many different forwarders / ACL
offices they can have wildly different layouts.  The old pdfplumber regex
approach (hard-coded x0 column thresholds, fixed seal pattern) breaks on
new formats.  This module sends the entire PDF to Gemini as a single call
(via helpers/gemini_client.py — the official google-generativeai SDK) and
asks the model to return all containers + seals in a structured JSON array.

Return shape (identical to the old parse_arrival_notice):
    {
        "ACLU9811060": {
            "container_no": "ACLU9811060",
            "eta":          "14 JUN 2025",
            "etd":          "08 JUN 2025",
            "seal_no":      "UL-6101303",
            "weight_kgs":   "",          # kept for compatibility
        },
        ...
    }

Fallback:
    If the Gemini call fails for any reason, the function falls back
    silently to the legacy pdfplumber parser so the overall process
    never crashes.

Environment:
    GEMINI_API_KEY must be set in .env (or exported).
"""

import logging
import os
import re

from helpers.gemini_client import call_gemini
from clients.carpenter.inbound.prompts import build_arrival_prompt

# Silence pypdf warnings (used by the pdfplumber fallback's callers elsewhere)
logging.getLogger("pypdf").setLevel(logging.ERROR)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# ISO container-number pattern: 4 uppercase letters + 7 digits
CONTAINER_PATTERN = re.compile(r"^[A-Z]{4}\d{7}$")


# ─────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────

def _validate_and_index(raw_items: list) -> dict:
    """
    Validate each item from Gemini and build the final container → info dict.

    Filters out any entry whose container_no doesn't match the ISO pattern.
    Keeps weight_kgs as "" for compatibility with the rest of the pipeline.
    """
    result = {}
    rejected = 0

    for item in raw_items:
        ctr = str(item.get("container_no", "")).strip()
        if not CONTAINER_PATTERN.match(ctr):
            rejected += 1
            print(f"  [!] Arrival Notice — rejected invalid container_no: '{ctr}'")
            continue

        if ctr in result:
            continue  # already seen; keep the first occurrence (document order)

        result[ctr] = {
            "container_no": ctr,
            "eta": str(item.get("eta", "")).strip(),
            "etd": str(item.get("etd", "")).strip(),
            "seal_no": str(item.get("seal_no", "")).strip(),
            "weight_kgs": "",  # Gemini prompt doesn't extract weight; kept for compat
        }

    if rejected:
        print(f"  [!] Arrival Notice — {rejected} item(s) rejected (bad container_no)")

    return result


# ─────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────

def parse_arrival_notice_llm(pdf_path: str) -> dict:
    """
    Parse an Arrival Notice PDF using Gemini vision.

    Returns:
        dict  container_no → {container_no, eta, etd, seal_no, weight_kgs}
        (Same shape as the legacy pdfplumber parse_arrival_notice.)

    Fallback:
        If Gemini is unavailable or the call fails (helpers/gemini_client.py
        already retries transient failures internally), falls back to the
        legacy pdfplumber-based parser and logs a warning.
    """
    if not GEMINI_API_KEY:
        print("  [!] GEMINI_API_KEY not set — falling back to pdfplumber parser")
        return _fallback_pdfplumber(pdf_path)

    print("  [*] Arrival Notice — sending PDF to Gemini for container/seal extraction…")

    try:
        raw_items = call_gemini(build_arrival_prompt(), pdf_path=pdf_path, max_output_tokens=4096)
        if isinstance(raw_items, dict):
            raw_items = [raw_items]
    except Exception as e:
        print(f"  [!] Arrival Notice — Gemini failed ({e}). Falling back to pdfplumber.")
        return _fallback_pdfplumber(pdf_path)

    result = _validate_and_index(raw_items)
    print(f"  [OK] Arrival Notice — Gemini extracted {len(result)} container(s): {list(result.keys())}")
    return result


# ─────────────────────────────────────────────────────────────
# LEGACY FALLBACK  (original pdfplumber logic — kept intact)
# ─────────────────────────────────────────────────────────────

def _fallback_pdfplumber(pdf_path: str) -> dict:
    """
    Original pdfplumber-based Arrival Notice parser.
    Used as a fallback when Gemini is unavailable.

    FIX v2 (preserved from original carpenter.py):
      - Seal pattern accepts optional hyphen: ^UL-?\\d{7}$
      - All pages combined into one sorted word list for cross-page matching.
      - Seal search scans forward to next container (not fixed window).
    """
    import pdfplumber

    print("  [*] Arrival Notice — using pdfplumber fallback parser")

    pdf = pdfplumber.open(pdf_path)
    container_pattern = re.compile(r"^[A-Z]{4}\d{7}$")
    seal_pattern = re.compile(r"^UL-?\d{7}$")  # optional hyphen

    # ── ETA / ETD from page 1 ──
    page1_words = pdf.pages[0].extract_words()
    eta, etd = "", ""
    for i, w in enumerate(page1_words):
        if w["text"] == "ETA:" and i + 2 < len(page1_words):
            parts = [page1_words[j]["text"] for j in range(i + 1, min(i + 3, len(page1_words)))]
            eta = " ".join(parts)
        if w["text"] == "ETD:" and i + 2 < len(page1_words):
            parts = [page1_words[j]["text"] for j in range(i + 1, min(i + 3, len(page1_words)))]
            etd = " ".join(parts)

    # ── Combine words from ALL pages (cross-page handling) ──
    all_words = []
    for pi, page in enumerate(pdf.pages):
        for w in page.extract_words():
            all_words.append({**w, "global_top": pi * 10000 + w["top"]})
    all_words.sort(key=lambda w: (w["global_top"], w["x0"]))

    # ── Extract containers + seals ──
    containers = {}
    for i, w in enumerate(all_words):
        if container_pattern.match(w["text"]) and w["x0"] < 120:
            ctr = w["text"]
            if ctr in containers:
                continue

            # Scan forward for seal; stop at next container
            seal = ""
            for j in range(i + 1, min(i + 150, len(all_words))):
                candidate = all_words[j]
                if container_pattern.match(candidate["text"]) and candidate["x0"] < 120:
                    break
                if seal_pattern.match(candidate["text"]) and candidate["x0"] < 50:
                    seal = candidate["text"]
                    break

            # Weight on same line
            weight_kgs = ""
            for j in range(i + 1, min(i + 6, len(all_words))):
                if (all_words[j]["text"] == "KGS"
                        and abs(all_words[j]["global_top"] - w["global_top"]) < 3):
                    weight_kgs = all_words[j - 1]["text"]
                    break

            containers[ctr] = {
                "container_no": ctr,
                "eta": eta,
                "etd": etd,
                "seal_no": seal,
                "weight_kgs": weight_kgs,
            }

    pdf.close()
    print(f"  [OK] Arrival Notice (pdfplumber) — {len(containers)} container(s) found")
    return containers
