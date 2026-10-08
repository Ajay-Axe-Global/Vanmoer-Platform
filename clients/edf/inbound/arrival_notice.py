"""
EDF Inbound (ED&F Man) — Arrival Notice PDF seal extraction.

Arrival Notice layouts differ by shipping line / office, and a container can
carry ONE seal or SEVERAL, so the PDF is sent to Gemini (extract_seals ->
_extract_seals_gemini) which returns every container's full seal list. A
deterministic pdfplumber parser for the MSC Belgium layout is kept as the
fallback when Gemini is unavailable. That layout's container table repeats
this block once per container:

    MSNU9884833 40HC 26680.000 kgs. / 66 cu. m.
    Seal Number: 846647483 B023238S B023268S

Each container carries SEVERAL seals on that one "Seal Number:" line (a
numeric carrier seal plus shipper "B......S" seals); all of them are kept, in
printed order, joined by single spaces. A long seal list can wrap onto the
next line, so lines directly after "Seal Number:" that are made only of
seal-like tokens are folded in too.

Only the container -> seals mapping is returned; the caller matches it to the
packing list's containers by container number.
"""

import re

import pdfplumber

# 4 letters + 7 digits, optionally split by a space/hyphen as some documents
# print it; normalised to the compact ISO form.
_CONTAINER_LINE_RE = re.compile(r"^\s*([A-Z]{4})[\s\-]?(\d{7})\b")
_SEAL_LABEL_RE = re.compile(r"^\s*Seal\s*(?:Number|No\.?|#)?s?\s*[:\-]\s*(.*)$", re.IGNORECASE)
_SEAL_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-/]{3,}$")
# A line that clearly starts the next section — never a seal continuation.
_STOP_RE = re.compile(r"^\s*(HS\s*Code|Total|COMMENTS|As agent)", re.IGNORECASE)


def _pdf_lines(path: str) -> list[str]:
    lines: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines.extend(text.splitlines())
    return lines


def _seal_tokens(text: str) -> list[str]:
    return [t for t in re.split(r"\s+", text.strip()) if t]


_CONTAINER_ID_RE = re.compile(r"^[A-Z]{4}\d{7}$")

_GEMINI_PROMPT = """\
You are reading a shipping-line / forwarder ARRIVAL NOTICE PDF. Layouts differ
between lines and offices, so read the document itself rather than assuming a
fixed shape.

TASK: for EVERY shipping container listed, return its container number and ALL
of its seal numbers.

- container_no: 11 characters = 4 uppercase letters + 7 digits, no spaces or
  hyphens (e.g. MSNU9884833). If printed with a space/hyphen, remove it.
- seals: a JSON array of EVERY seal printed for THAT container, in printed
  order, each as its own string exactly as printed. Some notices print a single
  seal per container, others print several (e.g. a numeric carrier seal plus
  one or more shipper seals like B023238S) on the same line or wrapped onto
  the next line — return all of them. Do NOT merge two containers' seals, and
  do not include text that is not a seal (labels like "Seal Number:", weights,
  HS codes, marks, booking or entrega numbers). If a container has no seal
  printed, return an empty array.
- Read every page. Ignore terms-and-conditions pages.

Be careful with look-alike characters in seals: transcribe exactly what is
printed (O vs 0, I vs 1, S vs 5, B vs 8).

Return ONLY a JSON array, one element per container, in document order:
[{"container_no": "MSNU9884833", "seals": ["846647483", "B023238S", "B023268S"]}]
"""


def _extract_seals_gemini(path: str) -> dict:
    """Layout-agnostic extraction via helpers/gemini_client.call_gemini().
    Raises on any failure so the caller can fall back to the regex parser."""
    from helpers.gemini_client import call_gemini

    raw = call_gemini(_GEMINI_PROMPT, pdf_path=path, max_output_tokens=4096, call_label="edf_arrival_notice")
    if isinstance(raw, dict):
        raw = raw.get("containers") if isinstance(raw.get("containers"), list) else [raw]

    seals: dict[str, str] = {}
    warnings: list[str] = []
    for item in raw or []:
        cid = re.sub(r"[\s\-]", "", str(item.get("container_no", ""))).upper()
        if not _CONTAINER_ID_RE.match(cid):
            warnings.append(f"[!]  SEAL — ignored invalid container number from Arrival Notice: {item.get('container_no')!r}")
            continue
        raw_seals = item.get("seals", item.get("seal_no", []))
        if isinstance(raw_seals, str):
            raw_seals = [raw_seals]
        tokens = [t for part in raw_seals for t in _seal_tokens(str(part))]
        if cid in seals:
            warnings.append(f"[!]  SEAL — {cid}: listed more than once in the Arrival Notice, keeping the first")
        elif tokens:
            seals[cid] = " ".join(tokens)
        else:
            warnings.append(f"[!]  SEAL — {cid}: no seal printed in the Arrival Notice")
    if not seals:
        raise RuntimeError("Gemini returned no container/seal pairs")
    return {"seals": seals, "warnings": warnings}


def extract_seals(path: str) -> dict:
    """Returns {"seals": {container_id: "SEAL1 SEAL2 ..."}, "warnings": [...]}.

    Gemini first (Arrival Notice layouts vary by shipping line and may print
    one seal or several per container); if it is unavailable or fails, falls
    back to the deterministic pdfplumber parser below, which only understands
    the MSC Belgium layout, and says so in the warnings.
    """
    try:
        return _extract_seals_gemini(path)
    except Exception as e:
        result = _extract_seals_regex(path)
        result["warnings"].insert(
            0, f"[!]  SEAL — AI extraction unavailable ({e}); used the basic MSC-layout parser instead, check the seals"
        )
        return result


def _extract_seals_regex(path: str) -> dict:
    lines = _pdf_lines(path)
    seals: dict[str, str] = {}
    warnings: list[str] = []

    current = None
    i = 0
    while i < len(lines):
        line = lines[i]

        m = _CONTAINER_LINE_RE.match(line)
        if m:
            current = m.group(1) + m.group(2)
            i += 1
            continue

        sm = _SEAL_LABEL_RE.match(line)
        if sm and current:
            tokens = _seal_tokens(sm.group(1))
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if (not nxt.strip() or _STOP_RE.match(nxt) or _CONTAINER_LINE_RE.match(nxt)
                        or _SEAL_LABEL_RE.match(nxt)):
                    break
                parts = _seal_tokens(nxt)
                if not parts or not all(_SEAL_TOKEN_RE.match(p) for p in parts):
                    break
                tokens.extend(parts)
                j += 1

            if current in seals:
                warnings.append(f"[!]  SEAL — {current}: listed more than once in the Arrival Notice, keeping the first")
            elif tokens:
                seals[current] = " ".join(tokens)
            else:
                warnings.append(f"[!]  SEAL — {current}: 'Seal Number' is blank in the Arrival Notice")
            current = None
            i = j
            continue

        i += 1

    if not seals:
        warnings.append("[X]  SEAL — no container/seal pairs found in the Arrival Notice PDF")
    return {"seals": seals, "warnings": warnings}
