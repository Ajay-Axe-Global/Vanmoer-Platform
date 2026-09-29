"""
Client: VINMAR (Tegral / Axia Plastics — Van Moer loading releases)
Task: Outbound
Document: Delivery / Release Note PDF(s) (one or more per run)

Pure regex extraction, same pattern as clients/sabic/outbound/task.py — no AI
involved. Vinmar's release notes come in 3 slightly different letterheads
(Axia Plastics "Instructions - SR...", Tegral/Profine "Delivery / Release
Note", and a plain "360 Plastics"-style note) but all 3 share the same
underlying field layout: a Sold-to/Ship-to/Trucker header block, a Release
Instruction box (Release No. / Delivery No.), a shipment table (Final Place
of Delivery / Loading Point / packages), an Item table (Item No. / Product
Description / Batch / Quantity / Unit), and — when the loading point is Van
Moer Kallo — a Loading Info page carrying a fixed "IMPORTANT !!!!****...
****!!!" warning line. One set of regexes below handles all 3 layouts.
"""

import re
from datetime import datetime

from flask import Blueprint, g, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from database.db import SessionLocal
from helpers.base_task import BaseTask
from helpers.decorators import task_access_required
from helpers.excel_writer import write_excel
from helpers.jobs import build_reference, job_output_path, log_job, new_job_dir
from helpers.pdf_utils import extract_text

CLIENT_SLUG = "vinmar"
TASK_SLUG = "outbound"

OUTPUT_FILENAME = "Vinmar_Outbound_Output.xlsx"

# Common European country names as they're spelled out in full on the 360
# Plastics-style layout, which — unlike the other two — doesn't print a
# trailing 2-letter ISO code next to the Ship-to postal code.
COUNTRY_NAME_TO_CODE = {
    "NETHERLANDS": "NL", "GERMANY": "DE", "BELGIUM": "BE", "FRANCE": "FR",
    "LUXEMBOURG": "LU", "UNITED KINGDOM": "GB", "SPAIN": "ES", "ITALY": "IT",
    "POLAND": "PL", "AUSTRIA": "AT", "SWITZERLAND": "CH", "DENMARK": "DK",
    "SWEDEN": "SE", "NORWAY": "NO", "IRELAND": "IE", "PORTUGAL": "PT",
    "CZECH REPUBLIC": "CZ", "HUNGARY": "HU", "SLOVAKIA": "SK", "SLOVENIA": "SI",
}

# One item row per line: "<item no> <description> <batch/PO> <qty> <unit>".
# Description and batch are both variable-length tokens, so the description
# is matched non-greedy and lets the regex engine backtrack until batch/qty/
# unit line up at the end of the line (batch commonly contains a dash, e.g.
# "3610667-01"; quantity never does, which is what makes the split
# unambiguous).
ITEM_ROW_RE = re.compile(
    r"^([\w\-]+)\s+(.+?)\s+([\w\-]+)\s+([\d.,]+)\s+(T|KG|KGS|MT)\s*$",
    re.MULTILINE,
)


def _release_no(t: str) -> str:
    m = re.search(r"Release\s*No\.?\s*:\s*(\S+)", t, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _delivery_no(t: str) -> str:
    m = re.search(r"Delivery\s*No\.?\s*:\s*(\S+)", t, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _trucker_name(t: str) -> str:
    # First non-blank line right after the "Trucker" label — the company
    # name always comes first, any address lines follow on subsequent lines.
    m = re.search(r"Trucker\s*\n\s*(.+?)\s*\n", t, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _ship_to_block(t: str) -> str:
    m = re.search(r"Ship-to Party\s*\n(.*?)\n\s*Trucker", t, re.IGNORECASE | re.DOTALL)
    return m.group(1) if m else ""


def _country_code(t: str) -> str:
    block = _ship_to_block(t)
    # Layouts that print a trailing ISO code next to the postal code, e.g.
    # "6181 MA, NL" or "66954, DE".
    m = re.search(r",\s*([A-Z]{2})\s*$", block.strip())
    if m:
        return m.group(1)
    # Fallback: the country spelled out in full (e.g. "NETHERLANDS").
    for name, code in COUNTRY_NAME_TO_CODE.items():
        if re.search(rf"\b{name}\b", block, re.IGNORECASE):
            return code
    return ""


def _remark(t: str) -> str:
    # Van Moer's Loading Info page wraps its one must-not-miss instruction in
    # "IMPORTANT !!!!****...****!!!" markers (e.g. the anti-slip-mats line).
    # Layouts with no Loading Info page (e.g. 360 Plastics) simply have none
    # of this — remark is left blank rather than guessed.
    m = re.search(r"IMPORTANT\s*!+\s*\*+\s*(.*?)\s*\*+\s*!+", t, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()


# HTML <input type="date"> always submits "YYYY-MM-DD" regardless of browser
# locale; the OP column reads "YYYYMMDD 00:00:00" (same convention as Sabic
# Outbound's planned_date / Vinmar Inbound's eta_date).
PLANNED_DATE_INPUT_FORMAT = "%Y-%m-%d"
PLANNED_DATE_OUTPUT_FORMAT = "%Y%m%d 00:00:00"


def format_planned_date(raw: str) -> str:
    return datetime.strptime(raw.strip(), PLANNED_DATE_INPUT_FORMAT).strftime(PLANNED_DATE_OUTPUT_FORMAT)


def _weight_kg(raw: str, unit: str) -> int:
    val = float(raw.strip().replace(",", ""))
    if unit.upper() in ("T", "MT"):
        val *= 1000
    return int(round(val))


class VinmarOutboundTask(BaseTask):
    client_slug = CLIENT_SLUG
    task_slug = TASK_SLUG
    label = "Vinmar — Outbound"

    required_documents = [
        {"key": "release_note", "label": "Delivery / Release Note PDF(s)", "accept": ".pdf", "multiple": True},
    ]

    column_config = [
        {"header": "Reference",    "field_key": "reference",    "width": 24},
        {"header": "Description",  "field_key": "description",  "width": 34},
        {"header": "Planned Date", "field_key": "planned_date", "width": 20},
        {"header": "Public ID",    "field_key": "public_id",    "width": 34},
        {"header": "Remarks",      "field_key": "remark",       "width": 40},
        {"header": "Country Code", "field_key": "country_code", "width": 12},
    ]

    def _extract_rows(self, text: str, planned_date: str) -> list[dict]:
        # One output row per release note document — even when it lists
        # several item lines (e.g. a LTL note combining 2 products), they all
        # belong to the same shipment/reference, so they're joined as
        # separate lines within ONE Description cell rather than exploded
        # into multiple rows.
        reference = "/".join(v for v in (_release_no(text), _delivery_no(text)) if v)
        public_id = _trucker_name(text)
        remark = _remark(text)
        country_code = _country_code(text)

        # Grouped by product name — batch/PO number is deliberately NOT part
        # of the grouping key: the same product split across several batches
        # (e.g. two lots of "PP J340") collapses into one summed-weight line,
        # and two different products that happen to share one batch/PO still
        # get their own separate lines, keyed only by product.
        weight_by_product: dict[str, int] = {}
        for m in ITEM_ROW_RE.finditer(text):
            _item_no, product, _batch, qty_raw, unit = m.groups()
            product = product.strip()
            weight_by_product[product] = weight_by_product.get(product, 0) + _weight_kg(qty_raw, unit)

        if not weight_by_product:
            return []

        descriptions = [f"{weight} KGS - {product}" for product, weight in weight_by_product.items()]

        return [{
            "reference": reference,
            "description": "\n".join(descriptions),
            "planned_date": planned_date,
            "public_id": public_id,
            "remark": remark,
            "country_code": country_code,
        }]

    # ── BaseTask entry point ────────────────────────────────────────────────
    def process(self, files: dict, output_path: str | None = None, planned_date: str = "") -> dict:
        paths = files.get("release_note", [])
        if isinstance(paths, str):
            paths = [paths]
        rows = []
        for p in paths:
            rows.extend(self._extract_rows(extract_text(p), planned_date))
        return {"rows": rows, "summary": {"documents_processed": len(paths)}}


# ── Flask Blueprint ──────────────────────────────────────────────────────────
bp = Blueprint(
    "vinmar_outbound", __name__,
    url_prefix=f"/app/{CLIENT_SLUG}/{TASK_SLUG}",
    template_folder="templates",
)

_task = VinmarOutboundTask()


@bp.route("/")
def index():
    # Namespaced under vinmar_outbound/ — Flask's template loader is global
    # across all blueprints, so a bare "index.html" would collide with other
    # clients' same-named templates and silently serve the wrong one.
    return render_template("vinmar_outbound/index.html", label=_task.label, documents=_task.required_documents)


@bp.route("/process", methods=["POST"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def process():
    files = request.files.getlist("release_note")
    if not files:
        return jsonify({"error": "At least one Delivery / Release Note PDF is required."}), 400

    planned_date_raw = (request.form.get("planned_date") or "").strip()
    if not planned_date_raw:
        return jsonify({"error": "Planned Date is required."}), 400
    try:
        planned_date = format_planned_date(planned_date_raw)
    except ValueError:
        return jsonify({"error": "Invalid Planned Date."}), 400

    job_id, job_dir = new_job_dir()
    session = SessionLocal()
    try:
        saved_paths = []
        for f in files:
            if not f.filename.lower().endswith(".pdf"):
                continue
            p = job_dir / secure_filename(f.filename)
            f.save(p)
            saved_paths.append(str(p))

        if not saved_paths:
            return jsonify({"error": "No valid PDF files uploaded."}), 400

        result = _task.process({"release_note": saved_paths}, planned_date=planned_date)
        write_excel(result["rows"], _task.column_config, str(job_output_path(job_id)))

        reference, reference_count = build_reference(r.get("reference") for r in result["rows"])
        source_filename = ", ".join(f.filename for f in files if f.filename.lower().endswith(".pdf"))
        log_job(session, g.user["user_id"], CLIENT_SLUG, TASK_SLUG, f"{job_id}/output.xlsx", "success",
                reference=reference, source_filename=source_filename, row_count=len(result["rows"]),
                reference_count=reference_count)

        return jsonify({
            "success": True,
            "job_id": job_id,
            "summary": result["summary"],
            "rows": result["rows"],
            "download_url": f"/app/{CLIENT_SLUG}/{TASK_SLUG}/download/{job_id}",
        })
    except Exception as e:
        log_job(session, g.user["user_id"], CLIENT_SLUG, TASK_SLUG, None, "failed")
        return jsonify({"error": str(e)}), 500
    finally:
        session.close()


@bp.route("/download/<job_id>")
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def download(job_id):
    from database.models import JobHistory
    session = SessionLocal()
    try:
        job = session.query(JobHistory).filter_by(
            output_filename=f"{job_id}/output.xlsx"
        ).first()
        if not job or (g.user["role"] != "admin" and job.user_id != g.user["user_id"]):
            return jsonify({"error": "Not found"}), 404
    finally:
        session.close()

    path = job_output_path(job_id)
    if not path.exists():
        return jsonify({"error": "Output file not found."}), 404
    return send_file(path, as_attachment=True, download_name=OUTPUT_FILENAME)
