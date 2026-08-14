"""
SABIC Inbound — task module.

Follows the same two-part structure as clients/carpenter/inbound/task.py:
  1. SabicInboundTask(BaseTask)  — extraction, validation, Excel output
  2. Flask Blueprint             — index / process / download routes
"""

import traceback

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
from helpers.jobs import job_output_path, log_job, new_job_dir

from .extractor import (
    build_rows,
    extract_invoice,
    extract_mbl,
    extract_packing_list,
    validate,
)

CLIENT_SLUG = "sabic"
TASK_SLUG = "inbound"

# ═══════════════════════════════════════════════════════════════════════════
# COLUMN CONFIG
# ═══════════════════════════════════════════════════════════════════════════

COLUMN_CONFIG = [
    {"header": "Ref No",          "field_key": "ref_no",         "width": 22},
    {"header": "Delivery No",     "field_key": "delivery_no",    "width": 16},
    {"header": "Container No",    "field_key": "container_no",   "width": 16},
    {"header": "Product",         "field_key": "product",        "width": 24},
    {"header": "Lot No",          "field_key": "lot_no",         "width": 16},
    {"header": "Country Code",    "field_key": "country_code",   "width": 10},
    {"header": "Pkg Type",        "field_key": "pkg_type",       "width": 10},
    {"header": "Pkg Qty",         "field_key": "pkg_qty",        "width": 10, "num_format": "#,##0"},
    {"header": "Net Weight",      "field_key": "net_weight",     "width": 14, "num_format": "#,##0.0000"},
    {"header": "Gross Weight",    "field_key": "gross_weight",   "width": 14, "num_format": "#,##0.0000"},
    {"header": "Seal No",         "field_key": "seal_no",        "width": 12},
    {"header": "Container + Ref", "field_key": "container_ref",  "width": 34},
    {"header": "Container Type",  "field_key": "container_type", "width": 16},
]

OUTPUT_FILENAME = "SABIC_Inbound_Outcome.xlsx"


# ═══════════════════════════════════════════════════════════════════════════
# TASK CLASS
# ═══════════════════════════════════════════════════════════════════════════

class SabicInboundTask(BaseTask):
    client_slug = "sabic"
    task_slug = "inbound"
    label = "SABIC Inbound"

    required_documents = [
        {"key": "mbl",          "label": "MBL (Sea Waybill)",    "accept": ".pdf", "multiple": False},
        {"key": "packing_list", "label": "Packing List",         "accept": ".pdf", "multiple": False},
        {"key": "invoice",      "label": "Commercial Invoice",   "accept": ".pdf", "multiple": False},
    ]

    column_config = COLUMN_CONFIG
    writes_own_output = False

    def process(self, files: dict, output_path: str | None = None) -> dict:
        # ── Step 1: LLM extraction ──────────────────────────────────
        mbl_data = extract_mbl(files["mbl"])
        pkl_data = extract_packing_list(files["packing_list"])
        inv_data = extract_invoice(files["invoice"])

        # ── Step 2: Cross-document validation ───────────────────────
        validation = validate(mbl_data, pkl_data, inv_data)

        # ── Step 3: Build outcome rows ──────────────────────────────
        rows = build_rows(mbl_data, pkl_data)

        # ── Summary stats ───────────────────────────────────────────
        containers = set(r["container_no"] for r in rows)
        total_bags = sum(r["pkg_qty"] for r in rows)
        total_net = sum(r["net_weight"] for r in rows)

        summary = {
            "mbl_no":           mbl_data.get("mbl_no", ""),
            "ref_no":           rows[0]["ref_no"] if rows else "",
            "delivery_no":      rows[0]["delivery_no"] if rows else "",
            "total_rows":       len(rows),
            "total_containers": len(containers),
            "total_bags":       total_bags,
            "total_net_weight": round(total_net, 4),
            "validation":       validation,
        }

        return {"rows": rows, "summary": summary}


# ═══════════════════════════════════════════════════════════════════════════
# BLUEPRINT
# ═══════════════════════════════════════════════════════════════════════════

_task = SabicInboundTask()

bp = Blueprint(
    "sabic_inbound",
    __name__,
    template_folder="templates",
    url_prefix="/app/sabic/inbound",
)


@bp.route("/")
def index():
    return render_template("sabic_inbound/index.html")


@bp.route("/process", methods=["POST"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def process():
    for doc in _task.required_documents:
        f = request.files.get(doc["key"])
        if not f or not f.filename:
            return jsonify({"error": f"Missing document: {doc['label']}"}), 400

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
        result = _task.process(saved, output_path)

        rows = result["rows"]
        summary = result["summary"]

        # ── Write Excel ─────────────────────────────────────────────
        write_excel(rows, COLUMN_CONFIG, output_path)

        # ── Log the job ─────────────────────────────────────────────
        log_job(session, g.user["user_id"], CLIENT_SLUG, TASK_SLUG, f"{job_id}/output.xlsx", "success")

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