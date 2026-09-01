"""
Shared shipping-document extraction utilities used across client extractors
(country-code lookup, container-type normalization, weight/unit conversion,
package-type classification, safe JSON coercion). Pulled out of
clients/sabic/inbound/extractor.py so new clients (e.g. Vinmar) can reuse the
exact same, already-tuned logic without duplicating or risking a regression
in the working Sabic flow.
"""

import json
import os
import re

# ═══════════════════════════════════════════════════════════════════════════
# COUNTRY CODE LOOKUP
# ═══════════════════════════════════════════════════════════════════════════

PORT_COUNTRY_MAP = {
    "KING ABDULLAH": "SA", "JEDDAH": "SA", "JUBAIL": "SA", "YANBU": "SA",
    "DAMMAM": "SA", "RABIGH": "SA", "SAUDI ARABIA": "SA", "RIYADH": "SA",
    "PUSAN": "KR", "BUSAN": "KR", "INCHEON": "KR", "KOREA": "KR", "GWANGYANG": "KR",
    "MESAIEED": "QA", "DOHA": "QA", "QATAR": "QA",
    "HAIPHONG": "VN",
    "SANTOS": "BR", "BRAZIL": "BR", "PARANAGUA": "BR", "ITAJAI": "BR",
    "HOUSTON": "US", "LOS ANGELES": "US", "NEW YORK": "US", "SAVANNAH": "US",
    "NEW ORLEANS": "US", "GEISMAR": "US", "BATON ROUGE": "US",
    "CHARLESTON": "US", "LONG BEACH": "US", "UNITED STATES": "US",
    "ANTWERP": "BE", "BELGIUM": "BE",
    "ROTTERDAM": "NL", "NETHERLANDS": "NL",
    "HAMBURG": "DE", "BREMERHAVEN": "DE", "GERMANY": "DE",
    "SHANGHAI": "CN", "QINGDAO": "CN", "NINGBO": "CN", "CHINA": "CN",
    "SINGAPORE": "SG",
    "MUMBAI": "IN", "NHAVA SHEVA": "IN", "INDIA": "IN",
    "TOKYO": "JP", "YOKOHAMA": "JP", "KOBE": "JP", "JAPAN": "JP",
    "FELIXSTOWE": "GB", "SOUTHAMPTON": "GB", "UNITED KINGDOM": "GB",
    "LE HAVRE": "FR", "MARSEILLE": "FR", "FRANCE": "FR",
    "BARCELONA": "ES", "VALENCIA": "ES", "SPAIN": "ES",
    "GENOA": "IT", "GIOIA TAURO": "IT", "ITALY": "IT",
    "PIRAEUS": "GR", "GREECE": "GR",
    "ISTANBUL": "TR", "MERSIN": "TR", "TURKEY": "TR",
    "DURBAN": "ZA", "CAPE TOWN": "ZA", "SOUTH AFRICA": "ZA",
    "JEBEL ALI": "AE", "DUBAI": "AE", "ABU DHABI": "AE",
    "MUNDRA": "IN", "CHENNAI": "IN",
    "LAEM CHABANG": "TH", "THAILAND": "TH",
    "PORT KLANG": "MY", "MALAYSIA": "MY",
    "JAKARTA": "ID", "INDONESIA": "ID",
    "HO CHI MINH": "VN", "VIETNAM": "VN",
    "KARACHI": "PK", "PAKISTAN": "PK",
    "COLOMBO": "LK", "SRI LANKA": "LK",
}


def get_country_code(port_of_loading: str) -> str:
    upper = (port_of_loading or "").upper().strip()
    for key, code in PORT_COUNTRY_MAP.items():
        if key in upper:
            return code
    return "??"


# ═══════════════════════════════════════════════════════════════════════════
# CONTAINER ID NORMALIZATION
# ═══════════════════════════════════════════════════════════════════════════

CONTAINER_RE = re.compile(r'^([A-Z]{4})(\d{7})')


def fix_container_id(raw: str, existing_seal: str = "") -> tuple[str, str]:
    cleaned = (raw or "").replace(" ", "").strip().upper()
    m = CONTAINER_RE.match(cleaned)
    if not m:
        return cleaned, existing_seal
    container_id = m.group(1) + m.group(2)
    leftover = cleaned[len(container_id):]
    if leftover and leftover.isdigit() and not existing_seal:
        return container_id, leftover
    return container_id, existing_seal


# ═══════════════════════════════════════════════════════════════════════════
# CONTAINER TYPE NORMALIZATION
# ═══════════════════════════════════════════════════════════════════════════

# Checked in order — HC markers first, since a container can be labeled with
# both a cargo-type word AND a high-cube marker (e.g. "40' HC DRY VAN"),
# and high-cube must win in that case.
_HC_KEYWORDS = ("HIGH CUBE", "HIGHCUBE", "HQ", "HC", "9 6", "9X6", "9 X 6", "96")
_FT_KEYWORDS = ("DRY VAN", "DV", "ST", "GP", "GENERAL", "DRY", "FT")


def normalize_container_type(raw: str) -> str:
    """
    Collapse the free-text MBL container type into exactly one of
    "40HC" / "40FT" / "20FT".

      - 20' containers are ALWAYS "20FT" regardless of sub-type.
      - 40' containers are "40HC" if any high-cube marker is present,
        else "40FT".
      - An unrecognized 40' string defaults to "40HC"; an unrecognized
        20' string defaults to "20FT".
    """
    cleaned = re.sub(r'[^A-Z0-9 ]', ' ', (raw or "").upper())
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()

    is_40 = "40" in cleaned
    is_20 = "20" in cleaned

    if is_20 and not is_40:
        return "20FT"

    if is_40:
        for kw in _HC_KEYWORDS:
            if kw in cleaned:
                return "40HC"
        for kw in _FT_KEYWORDS:
            if kw in cleaned:
                return "40FT"
        return "40HC"

    return cleaned or "??"


def spaced_container_type(container_type: str) -> str:
    """
    Same container type, reformatted with a space between the size and the
    letters — "40HC" -> "40 HC", "20FT" -> "20 FT". Purely cosmetic; the
    value is otherwise identical to normalize_container_type()'s output.
    """
    return re.sub(r'(\d+)([A-Za-z]+)', r'\1 \2', container_type or "")


# ═══════════════════════════════════════════════════════════════════════════
# WEIGHT / PACKAGE-TYPE HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def to_kg(value, unit="MT"):
    """Convert a weight to KG. MT x 1000, KG passthrough."""
    val = num(value, 0)
    if not val:
        return 0
    if (unit or "MT").upper().strip() == "MT":
        return round(val * 1000, 3)
    return round(val, 3)


def determine_pkg_type(net_weight_kg, bags):
    """net_weight_kg / bags > 50 -> 'Big Bags', else 'Bags'."""
    if not bags or bags == 0:
        return "Bags"
    per_bag = net_weight_kg / bags
    return "Big Bags" if per_bag > 50 else "Bags"


# ═══════════════════════════════════════════════════════════════════════════
# SAFE JSON COERCION
# ═══════════════════════════════════════════════════════════════════════════

def s(value) -> str:
    """
    Safely stringify a Gemini-extracted field. A JSON null comes back as
    Python None, and plain str(None) == "None" — a non-empty, truthy string
    that silently poisons every `x or fallback` / `if x:` check downstream.
    """
    return "" if value is None else str(value)


def num(value, default=0):
    """
    Coerce a Gemini-extracted numeric field to a number, safely handling
    None, missing keys, empty strings, and stray non-numeric junk.
    """
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value
    try:
        txt = str(value).strip()
        if not txt:
            return default
        return float(txt) if "." in txt else int(txt)
    except (TypeError, ValueError):
        return default


# ═══════════════════════════════════════════════════════════════════════════
# DEBUG JSON DUMP
# ═══════════════════════════════════════════════════════════════════════════

def dump_json(pdf_path: str, suffix: str, data):
    try:
        out = os.path.join(os.path.dirname(pdf_path), suffix)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass
