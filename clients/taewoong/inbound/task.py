"""
Taewoong Inbound — task module.

Source documents: MBL (PDF, any carrier) + Packing List (PDF, Hyosung
layout) + optional Arrival Notice (PDF, only for Seal No when the MBL has none). UI inputs, applied to every row: External ID, ETA Date, Shipping
Line, Vessel (Ship) Name. See extractor.py for where every OP column comes
from and which MBL layouts are supported.
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

from clients.edf.inbound.arrival_notice import extract_seals

from .extractor import (
    SHIPPING_LINE_OPTIONS,
    build_rows,
    carrier_display,
    extract_mbl,
    extract_packing_list,
    validate,
    validate_seals,
)

CLIENT_SLUG = "taewoong"  # matches Client(name="Taewoong") -> slug in database/seed.py
TASK_SLUG = "inbound"

# ═══════════════════════════════════════════════════════════════════════════
# COLUMN CONFIG — same order as the older clients
# ═══════════════════════════════════════════════════════════════════════════

COLUMN_CONFIG = [
    {"header": "External ID",              "field_key": "external_id",        "width": 22},
    {"header": "External ID/Container No", "field_key": "external_container", "width": 32},
    {"header": "Container No",             "field_key": "container_no",       "width": 16},
    {"header": "Container Type",           "field_key": "container_type",     "width": 14},
    {"header": "Seal No",                  "field_key": "seal_no",            "width": 16},
    {"header": "Shipping Line",            "field_key": "shipping_line",      "width": 34},
    {"header": "Ship Name",                "field_key": "ship_name",          "width": 20},
    {"header": "Product",                  "field_key": "product",            "width": 16},
    {"header": "Lot",                      "field_key": "lot_no",             "width": 14},
    {"header": "Lot/Container No",         "field_key": "lot_container",      "width": 26},
    {"header": "Bags Qty",                 "field_key": "bags_qty",           "width": 12, "num_format": "#,##0"},
    {"header": "Pallet Qty",               "field_key": "pallet_count",       "width": 11, "num_format": "#,##0"},
    {"header": "Net Weight (KG)",          "field_key": "net_weight",         "width": 16, "num_format": "#,##0.00"},
    {"header": "Gross Weight (KG)",        "field_key": "gross_weight",       "width": 18, "num_format": "#,##0.00"},
    {"header": "ETA/Date",                 "field_key": "eta_date",           "width": 20},
]

OUTPUT_FILENAME = "Taewoong_Inbound_Outcome.xlsx"

# <input type="date"> submits "YYYY-MM-DD"; the OP column reads
# "YYYYMMDD 00:00:00" (same convention as every other Inbound task).
ETA_DATE_INPUT_FORMAT = "%Y-%m-%d"
ETA_DATE_OUTPUT_FORMAT = "%Y%m%d 00:00:00"


def format_eta_date(raw: str) -> str:
    return datetime.strptime(raw.strip(), ETA_DATE_INPUT_FORMAT).strftime(ETA_DATE_OUTPUT_FORMAT)


# ═══════════════════════════════════════════════════════════════════════════
# TASK CLASS
# ═══════════════════════════════════════════════════════════════════════════

class TaewoongInboundTask(BaseTask):
    client_slug = CLIENT_SLUG
    task_slug = TASK_SLUG
    label = "Taewoong Inbound"

    required_documents = [
        {"key": "mbl",          "label": "MBL (Bill of Lading)", "accept": ".pdf", "multiple": False},
        {"key": "packing_list", "label": "Packing List",         "accept": ".pdf", "multiple": False},
        # Optional: Seal No for containers whose seal the MBL doesn't give.
        {"key": "arrival_notice", "label": "Arrival Notice (PDF)", "accept": ".pdf", "multiple": False,
         "required": False},
    ]

    column_config = COLUMN_CONFIG
    writes_own_output = False

    def process(self, files: dict, output_path: str | None = None, external_id: str = "",
                shipping_line: str = "", ship_name: str = "", eta_date: str = "") -> dict:
        mbl_data = extract_mbl(files["mbl"])
        mbl_ids = [c["id"] for c in mbl_data.get("containers", []) if c.get("id")]
        pl_data = extract_packing_list(files["packing_list"], mbl_ids)

        arrival_notice = extract_seals(files["arrival_notice"]) if files.get("arrival_notice") else None

        validation = validate(mbl_data, pl_data) + validate_seals(mbl_data, pl_data, arrival_notice)
        rows = build_rows(mbl_data, pl_data, external_id, shipping_line, ship_name, eta_date,
                          an_seals=arrival_notice["seals"] if arrival_notice else None)

        summary = {
            "mbl_no":           mbl_data.get("mbl_no", ""),
            "carrier":          mbl_data.get("carrier", ""),
            "external_id":      external_id,
            "shipping_line":    shipping_line,
            "ship_name":        ship_name,
            "eta_date":         eta_date,
            "total_rows":       len(rows),
            "total_containers": len({r["container_no"] for r in rows}),
            "total_bags":       sum(r["bags_qty"] for r in rows),
            "total_net_weight": round(sum(r["net_weight"] for r in rows), 3),
            "validation":       validation,
        }
        return {"rows": rows, "summary": summary}


# ═══════════════════════════════════════════════════════════════════════════
# BLUEPRINT
# ═══════════════════════════════════════════════════════════════════════════

_task = TaewoongInboundTask()

bp = Blueprint(
    "taewoong_inbound",
    __name__,
    template_folder="templates",
    url_prefix="/app/taewoong/inbound",
)


@bp.route("/")
def index():
    return render_template("taewoong_inbound/index.html", shipping_lines=SHIPPING_LINE_OPTIONS)


@bp.route("/process", methods=["POST"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def process():
    mbl_file = request.files.get("mbl")
    if not mbl_file or not mbl_file.filename:
        return jsonify({"error": "Missing document: MBL (Bill of Lading)"}), 400
    pkl_file = request.files.get("packing_list")
    if not pkl_file or not pkl_file.filename:
        return jsonify({"error": "Missing document: Packing List"}), 400
    an_file = request.files.get("arrival_notice")
    has_an = bool(an_file and an_file.filename)
    checks = [(mbl_file, "MBL"), (pkl_file, "Packing List")]
    if has_an:
        checks.append((an_file, "Arrival Notice"))
    for f, label in checks:
        if not f.filename.lower().endswith(".pdf"):
            return jsonify({"error": f"{label} must be a PDF file."}), 400

    external_id = (request.form.get("external_id") or "").strip()
    if not external_id:
        return jsonify({"error": "External ID is required."}), 400

    shipping_line_alias = (request.form.get("shipping_line") or "").strip()
    if shipping_line_alias not in SHIPPING_LINE_OPTIONS:
        return jsonify({"error": "Select a valid Shipping Line."}), 400
    shipping_line = carrier_display(shipping_line_alias)  # alias -> full OP spelling

    ship_name = (request.form.get("ship_name") or "").strip()
    if not ship_name:
        return jsonify({"error": "Vessel Name is required."}), 400

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
        mbl_path = str(job_dir / secure_filename(mbl_file.filename))
        mbl_file.save(mbl_path)
        pkl_path = str(job_dir / secure_filename(pkl_file.filename))
        pkl_file.save(pkl_path)

        files = {"mbl": mbl_path, "packing_list": pkl_path}
        if has_an:
            an_path = str(job_dir / secure_filename(an_file.filename))
            an_file.save(an_path)
            files["arrival_notice"] = an_path

        output_path = str(job_output_path(job_id))
        result = _task.process(
            files, output_path,
            external_id=external_id, shipping_line=shipping_line,
            ship_name=ship_name, eta_date=eta_date,
        )
        rows = result["rows"]
        summary = result["summary"]

        write_excel(rows, COLUMN_CONFIG, output_path)

        reference_val, reference_count = build_reference([external_id])
        log_job(session, g.user["user_id"], CLIENT_SLUG, TASK_SLUG, f"{job_id}/output.xlsx", "success",
                reference=reference_val, source_filename=", ".join([mbl_file.filename, pkl_file.filename] + ([an_file.filename] if has_an else [])),
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
