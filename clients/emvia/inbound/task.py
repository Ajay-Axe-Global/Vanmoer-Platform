"""
Emvia Inbound (Warehouse 1147) — task module.

Follows the same two-part structure as clients/vinmar/inbound/task.py:
  1. EmviaInboundTask(BaseTask)  — extraction, validation, Excel output
  2. Flask Blueprint              — index / process / download routes

Warehouse 1147 only, for this pass (NNRC comes later). The Packing List can
be either the PDF layout (extractor.py, Gemini-based) or an Excel sheet
(excel_extractor.py, pandas-based, no LLM — see that module's docstring for
why) — which path runs is decided by the uploaded file's own extension, not
a client-trusted form flag, so it's always correct regardless of what the
UI's toggle happened to show.
"""

import traceback
from datetime import datetime
from pathlib import Path

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
from .extractor import build_rows, extract_mbl, extract_packing_list, validate

EXCEL_EXTENSIONS = {".xlsx", ".xls"}

CLIENT_SLUG = "emvia"
TASK_SLUG = "inbound"

# ═══════════════════════════════════════════════════════════════════════════
# COLUMN CONFIG
# ═══════════════════════════════════════════════════════════════════════════

COLUMN_CONFIG = [
    {"header": "Reference",       "field_key": "reference",      "width": 22},
    {"header": "Container No",    "field_key": "container_no",   "width": 16},
    {"header": "Container/Ref",   "field_key": "container_ref",  "width": 34},
    {"header": "Mbl Number",      "field_key": "mbl_no",         "width": 20},
    {"header": "Seal No",         "field_key": "seal_no",        "width": 18},
    {"header": "Container Type",  "field_key": "container_type", "width": 14},
    {"header": "Warehouse",       "field_key": "warehouse",      "width": 12},
    {"header": "Country Code",    "field_key": "country_code",   "width": 10},
    {"header": "Product",         "field_key": "product",        "width": 14},
    {"header": "Batch No",        "field_key": "batch_no",       "width": 14},
    {"header": "Pieces Qty",      "field_key": "pieces_qty",     "width": 12, "num_format": "#,##0"},
    {"header": "Net Weight (KG)", "field_key": "net_weight",     "width": 16, "num_format": "#,##0"},
    {"header": "Gross Weight (KG)", "field_key": "gross_weight", "width": 16, "num_format": "#,##0"},
    {"header": "Pallet Count",    "field_key": "pallet_count",   "width": 12, "num_format": "#,##0"},
    # Only populated for Excel-sourced rows (Ref + Receiver, both given
    # directly on that sheet); empty for PDF-sourced rows, which have no
    # per-row Receiver value anywhere on the document.
    {"header": "Ref+Receiver",    "field_key": "ref_receiver",   "width": 26},
]

OUTPUT_FILENAME = "Emvia_Inbound_Outcome.xlsx"

# HTML <input type="date"> always submits "YYYY-MM-DD" regardless of browser
# locale; the OP column must read "YYYYMMDD 00:00:00" (same convention as
# Sabic/Vinmar Inbound).
ETA_DATE_INPUT_FORMAT = "%Y-%m-%d"
ETA_DATE_OUTPUT_FORMAT = "%Y%m%d 00:00:00"


def format_eta_date(raw: str) -> str:
    """Convert a UI-submitted 'YYYY-MM-DD' ETA date into the OP format."""
    return datetime.strptime(raw.strip(), ETA_DATE_INPUT_FORMAT).strftime(ETA_DATE_OUTPUT_FORMAT)


# ═══════════════════════════════════════════════════════════════════════════
# TASK CLASS
# ═══════════════════════════════════════════════════════════════════════════

class EmviaInboundTask(BaseTask):
    client_slug = "emvia"
    task_slug = "inbound"
    label = "Emvia Inbound"

    required_documents = [
        {"key": "mbl",          "label": "MBL (Bill of Lading)", "accept": ".pdf", "multiple": False},
        {"key": "packing_list", "label": "Packing List",         "accept": ".pdf,.xlsx,.xls", "multiple": False},
    ]

    column_config = COLUMN_CONFIG
    writes_own_output = False

    def process(self, files: dict, output_path: str | None = None, reference: str = "",
                warehouse: str = "1147") -> dict:
        # ── Step 1: extraction ──────────────────────────────────────
        mbl_data = extract_mbl(files["mbl"])

        pkl_path = files["packing_list"]
        if Path(pkl_path).suffix.lower() in EXCEL_EXTENSIONS:
            pkl_data = extract_packing_list_excel(pkl_path)
        else:
            pkl_data = extract_packing_list(pkl_path)

        # ── Step 2: Cross-document validation ───────────────────────
        validation = validate(mbl_data, pkl_data)

        # ── Step 3: Build outcome rows ──────────────────────────────
        # reference/warehouse are UI-entered (not extracted from the
        # documents) and apply uniformly to every row in this shipment.
        rows = build_rows(mbl_data, pkl_data, reference, warehouse)

        # ── Summary stats ───────────────────────────────────────────
        containers = set(r["container_no"] for r in rows)
        total_pieces = sum(r["pieces_qty"] for r in rows)
        total_net = sum(r["net_weight"] for r in rows)

        summary = {
            "mbl_no":           mbl_data.get("mbl_no", ""),
            "reference":        reference,
            "warehouse":        warehouse,
            "total_rows":       len(rows),
            "total_containers": len(containers),
            "total_pieces":     total_pieces,
            "total_net_weight": round(total_net, 3),
            "validation":       validation,
        }

        return {"rows": rows, "summary": summary}


# ═══════════════════════════════════════════════════════════════════════════
# BLUEPRINT
# ═══════════════════════════════════════════════════════════════════════════

_task = EmviaInboundTask()

bp = Blueprint(
    "emvia_inbound",
    __name__,
    template_folder="templates",
    url_prefix="/app/emvia/inbound",
)


@bp.route("/")
def index():
    return render_template("emvia_inbound/index.html")


@bp.route("/process", methods=["POST"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def process():
    for doc in _task.required_documents:
        f = request.files.get(doc["key"])
        if not f or not f.filename:
            return jsonify({"error": f"Missing document: {doc['label']}"}), 400

    eta_date_raw = (request.form.get("eta_date") or "").strip()
    if not eta_date_raw:
        return jsonify({"error": "ETA Date is required."}), 400
    try:
        format_eta_date(eta_date_raw)
    except ValueError:
        return jsonify({"error": "Invalid ETA Date."}), 400

    reference = (request.form.get("reference") or "").strip()
    warehouse = (request.form.get("warehouse") or "1147").strip()

    job_id, job_dir = new_job_dir()
    session = SessionLocal()
    try:
        # ── Save uploaded files ─────────────────────────────────────
        saved = {}
        for doc in _task.required_documents:
            f = request.files[doc["key"]]
            path = str(job_dir / secure_filename(f.filename))
            f.save(path)
            saved[doc["key"]] = path

        # ── Run the task ────────────────────────────────────────────
        output_path = str(job_output_path(job_id))
        result = _task.process(saved, output_path, reference=reference, warehouse=warehouse)

        rows = result["rows"]
        summary = result["summary"]

        # ── Write Excel ─────────────────────────────────────────────
        write_excel(rows, COLUMN_CONFIG, output_path)

        # ── Log the job ─────────────────────────────────────────────
        reference_val, reference_count = build_reference([summary.get("reference") or ""])
        source_filename = ", ".join(
            request.files[doc["key"]].filename for doc in _task.required_documents
        )
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
