"""
Sunrise Inbound — task module.

Source documents: MBL (PDF, one file) + Packing List (Excel, ALWAYS
one-file-per-container — confirmed with the user: a multi-container
shipment is uploaded as N separate Excel files, never several containers'
worth of rows stacked in one file). See excel_extractor.py for the sheet
layout and per-lot grouping rule, extractor.py for MBL extraction
(reused wholesale from Swiss Inbound's carrier-prompt library) and
row-building.

UI fields are just Reference + ETA Date, applied uniformly to every row —
same "manual override" convention as every other Inbound task (e.g.
Vinmar's external_id).
"""

import traceback
from datetime import datetime

from flask import (
    Blueprint,
    g,
    jsonify,
    render_template,
    request,
    send_file,
)
from werkzeug.utils import secure_filename

from database.db import SessionLocal
from helpers.base_task import BaseTask
from helpers.decorators import task_access_required
from helpers.excel_writer import write_excel
from helpers.jobs import build_reference, job_output_path, log_job, new_job_dir

from .excel_extractor import extract_packing_list_excel
from .extractor import build_rows, extract_mbl, validate

CLIENT_SLUG = "sunrise"
TASK_SLUG = "inbound"

# ═══════════════════════════════════════════════════════════════════════════
# COLUMN CONFIG
# ═══════════════════════════════════════════════════════════════════════════

COLUMN_CONFIG = [
    {"header": "Reference",        "field_key": "reference",       "width": 22},
    {"header": "Container",        "field_key": "container_no",    "width": 16},
    {"header": "Container/Ref",    "field_key": "container_ref",   "width": 30},
    {"header": "Container Type",   "field_key": "container_type",  "width": 14},
    {"header": "Container Type2",  "field_key": "container_type2", "width": 14},
    {"header": "Seal",             "field_key": "seal_no",         "width": 14},
    {"header": "Country Code",     "field_key": "country_code",    "width": 10},
    {"header": "Product",          "field_key": "product",         "width": 16},
    {"header": "Lot",              "field_key": "lot_no",          "width": 16},
    {"header": "Bags Qty",         "field_key": "bags_qty",        "width": 12, "num_format": "#,##0"},
    {"header": "Pallet",           "field_key": "pallet_count",    "width": 10, "num_format": "#,##0"},
    {"header": "Net Weight (KG)",  "field_key": "net_weight",      "width": 16, "num_format": "#,##0"},
    {"header": "Gross Weight (KG)", "field_key": "gross_weight",   "width": 16, "num_format": "#,##0"},
    {"header": "ETA Date",         "field_key": "eta_date",        "width": 20},
]

OUTPUT_FILENAME = "Sunrise_Inbound_Outcome.xlsx"

# HTML <input type="date"> always submits "YYYY-MM-DD" regardless of browser
# locale; the OP column must read "YYYYMMDD 00:00:00" (same convention as
# Sabic/Vinmar/Emvia Inbound).
ETA_DATE_INPUT_FORMAT = "%Y-%m-%d"
ETA_DATE_OUTPUT_FORMAT = "%Y%m%d 00:00:00"


def format_eta_date(raw: str) -> str:
    """Convert a UI-submitted 'YYYY-MM-DD' ETA date into the OP format."""
    return datetime.strptime(raw.strip(), ETA_DATE_INPUT_FORMAT).strftime(ETA_DATE_OUTPUT_FORMAT)


# ═══════════════════════════════════════════════════════════════════════════
# TASK CLASS
# ═══════════════════════════════════════════════════════════════════════════

class SunriseInboundTask(BaseTask):
    client_slug = CLIENT_SLUG
    task_slug = TASK_SLUG
    label = "Sunrise Inbound"

    required_documents = [
        {"key": "mbl",           "label": "MBL (Bill of Lading)",             "accept": ".pdf", "multiple": False},
        {"key": "packing_lists", "label": "Packing List (Excel, 1 per container)", "accept": ".xlsx,.xls", "multiple": True},
    ]

    column_config = COLUMN_CONFIG
    writes_own_output = False

    def process(self, files: dict, output_path: str | None = None,
                reference: str = "", eta_date: str = "") -> dict:
        # ── Step 1: extraction ──────────────────────────────────────
        mbl_data = extract_mbl(files["mbl"])
        packing_lists = [extract_packing_list_excel(p) for p in files["packing_lists"]]

        # ── Step 2: cross-document validation ───────────────────────
        validation = validate(mbl_data, packing_lists)

        # ── Step 3: build outcome rows ──────────────────────────────
        rows = build_rows(mbl_data, packing_lists, reference, eta_date)

        # ── Summary stats ───────────────────────────────────────────
        containers = set(r["container_no"] for r in rows)
        total_bags = sum(r["bags_qty"] for r in rows)
        total_net = sum(r["net_weight"] for r in rows)

        summary = {
            "mbl_no":           mbl_data.get("mbl_no", ""),
            "carrier":          mbl_data.get("carrier", ""),
            "reference":        reference,
            "eta_date":         eta_date,
            "total_rows":       len(rows),
            "total_containers": len(containers),
            "total_bags":       total_bags,
            "total_net_weight": round(total_net, 3),
            "validation":       validation,
        }

        return {"rows": rows, "summary": summary}


# ═══════════════════════════════════════════════════════════════════════════
# BLUEPRINT
# ═══════════════════════════════════════════════════════════════════════════

_task = SunriseInboundTask()

bp = Blueprint(
    "sunrise_inbound",
    __name__,
    template_folder="templates",
    url_prefix="/app/sunrise/inbound",
)


@bp.route("/")
def index():
    return render_template("sunrise_inbound/index.html")


@bp.route("/process", methods=["POST"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def process():
    mbl_file = request.files.get("mbl")
    if not mbl_file or not mbl_file.filename:
        return jsonify({"error": "Missing document: MBL (Bill of Lading)"}), 400

    pkl_files = [f for f in request.files.getlist("packing_lists") if f and f.filename]
    if not pkl_files:
        return jsonify({"error": "At least one Packing List Excel file is required."}), 400

    reference = (request.form.get("reference") or "").strip()
    if not reference:
        return jsonify({"error": "Reference is required."}), 400

    eta_date_raw = (request.form.get("eta_date") or "").strip()
    if not eta_date_raw:
        return jsonify({"error": "ETA Date is required."}), 400
    try:
        eta_date = format_eta_date(eta_date_raw)
    except ValueError:
        return jsonify({"error": "Invalid ETA Date."}), 400

    job_id, job_dir = new_job_dir()
    session = SessionLocal()
    try:
        # ── Save uploaded files ─────────────────────────────────────
        mbl_path = str(job_dir / secure_filename(mbl_file.filename))
        mbl_file.save(mbl_path)

        pkl_paths = []
        for f in pkl_files:
            p = str(job_dir / secure_filename(f.filename))
            f.save(p)
            pkl_paths.append(p)

        # ── Run the task ────────────────────────────────────────────
        output_path = str(job_output_path(job_id))
        result = _task.process(
            {"mbl": mbl_path, "packing_lists": pkl_paths},
            output_path, reference=reference, eta_date=eta_date,
        )

        rows = result["rows"]
        summary = result["summary"]

        # ── Write Excel ─────────────────────────────────────────────
        write_excel(rows, COLUMN_CONFIG, output_path)

        # ── Log the job ─────────────────────────────────────────────
        reference_val, reference_count = build_reference([summary.get("reference") or ""])
        source_filename = ", ".join([mbl_file.filename] + [f.filename for f in pkl_files])
        log_job(session, g.user["user_id"], CLIENT_SLUG, TASK_SLUG, f"{job_id}/output.xlsx", "success",
                reference=reference_val, source_filename=source_filename,
                row_count=len(rows), reference_count=reference_count)

        return jsonify({
            "success": True,
            "job_id": job_id,
            "summary": summary,
            "output_file": OUTPUT_FILENAME,
        })

    except Exception as e:
        log_job(session, g.user["user_id"], CLIENT_SLUG, TASK_SLUG, None, "failed")
        traceback.print_exc()
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
