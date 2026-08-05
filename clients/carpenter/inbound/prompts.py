"""
Gemini prompts for the Carpenter Inbound task. Kept separate from
arrival_notice.py / scanned_doc.py (and out of helpers/) because prompts are
entirely specific to this client/document type — the shared gemini_client.py
helper knows nothing about them.
"""


def build_arrival_prompt() -> str:
    """
    Craft the Gemini extraction prompt for an Arrival Notice PDF.

    The prompt is intentionally broad about seal formats and layout so it
    works regardless of which ACL office or forwarder produced the document.
    """
    return """\
You are a data-extraction specialist reading an ACL / freight-forwarder Arrival Notice PDF.

─── TASK ───
Find EVERY shipping container listed in this document.
For each container return:
  • container_no  — 11-character ISO code: 4 uppercase letters followed by 7 digits
                    Examples: ACLU9811060, GCNU4859065, TCKU3428920
  • seal_no       — the seal / lead number shown next to or below that container.
                    It may look like: UL-6101303  UL6101303  UL 6101303  12345678  etc.
                    If no seal is shown for a container, return "".
  • eta           — Estimated Time of Arrival, as printed in the document (e.g. "14 JUN 2025").
                    If a single ETA applies to all containers, repeat it for each.
                    If no ETA is found, return "".
  • etd           — Estimated Time of Departure, same rules as ETA.

─── OUTPUT FORMAT ───
Return a JSON array — one element per container, in document order:

[
  {
    "container_no": "ACLU9811060",
    "seal_no": "UL-6101303",
    "eta": "14 JUN 2025",
    "etd": "08 JUN 2025"
  },
  {
    "container_no": "GCNU4859065",
    "seal_no": "UL-6101304",
    "eta": "14 JUN 2025",
    "etd": "08 JUN 2025"
  }
]

─── RULES ───
1. container_no MUST match: 4 uppercase letters + 7 digits.  Reject anything else.
2. Extract EVERY container — do not stop after the first one.
3. If the document lists no containers at all, return an empty array: []
4. Return ONLY the JSON array — no markdown fences, no commentary, no extra text.\
"""


def build_scanned_doc_prompt(n_pages: int) -> str:
    """
    Build the Gemini extraction prompt for scanned Carpenter Titanium/Dynamet
    packing-list pages.

    The prompt is explicit about:
      - What a DETAIL section looks like (with an ASCII example)
      - How TAG NBR differs from LOT NBR and part numbers
      - That each row has its own W.O.# (must be read independently)
      - That different pages may belong to different packing lists
      - The exact JSON schema expected
    """
    return f"""You are a data-extraction specialist. You are reading {n_pages} scanned page(s) from Carpenter Titanium / Dynamet PACKING LIST documents.

─── TASK ───
For every page in this PDF, find the DETAIL section (if one exists) and extract every row from it.

─── WHAT THE DETAIL SECTION LOOKS LIKE ───
It is a table preceded by a dashed separator line. The columns are:

  - - - - - - - - D E T A I L - - - - - - - -
  TAG NBR         W.O. #   LOT NBR       QUANTITY UM       WEIGHT UM
  35168DG-001     303598   35168DG         278 lbs           278. lbs
  35168DG-002     303598   35168DG         289 lbs           289. lbs
  HC20974-041     340884   HC20974         554 lbs           554. lbs
  HC21018-038     340883   HC21018         926 lbs           926. lbs

─── FIELD DEFINITIONS ───

TAG NBR (Handling Unit identifier):
  • Always contains a HYPHEN followed by a numeric or alphanumeric suffix.
  • Examples: 35168DG-001, 35168DG-003A, 35168DG-003B, HC20974-041, HC21018-038, HC21018-039
  • NEVER a part number like "111DB4HBN02539AN" or "121DA2AGC13779B"
  • NEVER a bare LOT NBR like "35168DG" or "HC20974" (those lack the -suffix)

W.O. # (Work Order number):
  • A short NUMERIC code, typically 5-6 digits: 303598, 340884, 340882, 340883
  • Located in the W.O. # column — do NOT confuse with LOT NBR (which is alphanumeric)
  • CRITICAL: Read the W.O. # for EACH row from its own column. Different rows on the
    same page CAN have different W.O. # values. Do not copy the first row's value to all rows.

WEIGHT UM:
  • The weight in LBS from the WEIGHT UM column (rightmost numeric column).
  • Return as a number (e.g. 278.0, not "278. lbs").

─── PAGE TYPES ───
Not every page has a DETAIL section. Many pages are:
  • Cover pages (with SHIP TO address, packing instructions, customs info)
  • Instruction / paperwork pages
  • Invoice / billing pages
  • Summary pages
For these, return an empty items array.

─── OUTPUT FORMAT ───
Return a JSON array with exactly {n_pages} element(s), one per page:

[
  {{
    "page": 1,
    "packing_list_no": "129857",
    "items": [
      {{
        "tag_nbr": "35168DG-001",
        "order_no": "303598",
        "weight_lbs": 278.0
      }},
      {{
        "tag_nbr": "35168DG-002",
        "order_no": "303598",
        "weight_lbs": 289.0
      }}
    ]
  }},
  {{
    "page": 2,
    "packing_list_no": "129857",
    "items": []
  }}
]

─── ADDITIONAL SOURCE: PALLET PACK LIST ───
Some pages contain a PALLET PACK LIST section instead of (or in addition to) a DETAIL section:

  - - - - - - P A L L E T   P A C K   L I S T - - - - - -
  PALLET ID       TAG NBR         PART NBR          QUANTITY UM  TARE WT  NET WT UM
  35654DK-002     35654DK-002     111DC1DTM02161AN   109.316 kgs          241. lbs
  35654DK-002     35654DK-011     111DC1DTM02161AN   122.470 kgs          270. lbs
  35654DK-002     35654DK-012B    111DC1DTM02161AN    83.008 kgs          183. lbs

Extract every TAG NBR from the TAG NBR column (second column).
The W.O. # is NOT shown in pallet pack lists — set order_no to "" for these items.
The weight in LBS comes from the NET WT UM column (rightmost numeric column).
Do NOT use the PALLET ID column as a TAG NBR — only extract from TAG NBR column.

─── RULES ───
1. Process EVERY page. Do not skip any.
2. Extract EVERY row from each DETAIL table AND each PALLET PACK LIST. Do not skip any rows.
3. If a page has neither a DETAIL section nor a PALLET PACK LIST, return it with "items": [].
4. Different pages may belong to DIFFERENT packing lists with different W.O. # values.
5. Return ONLY the JSON array — no markdown fences, no commentary, no extra text.
6. Double-check each TAG NBR contains a hyphen. If it doesn't, skip it.
7. A TAG NBR already extracted from a DETAIL section on an earlier page must NOT
   be extracted again from a PALLET PACK LIST page — output each TAG NBR only once
   across the entire response.
─── COMMON TRAP: LOT NBR summary vs DETAIL table ───
Some pages have an ORDER SUMMARY section that looks like this:

  LOT NBR       W.O. #     QUANTITY UM
  35168DG       303598     2800 lbs / 11

This is NOT the DETAIL table! The LOT NBR here (e.g. "35168DG") has NO hyphen suffix —
it is a lot identifier, not a TAG NBR. Do NOT extract rows from this section.

Only extract from the DETAIL table, which has this header and dashed separator:
  - - - D E T A I L - - -
  TAG NBR       W.O. #   LOT NBR   QUANTITY UM   WEIGHT UM"""
