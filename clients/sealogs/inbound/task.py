"""
Sealogs Inbound — task module.

Source documents: MBL (PDF, any carrier / house BL) + Packing List (PDF, any
layout) + Arrival Notice (PDF, optional but recommended: cleanest container
ids and per-container figures). UI inputs applied to every row: External ID,
Shipping Line, Ship Name, ETA Date. See extractor.py for where each OP column
comes from.
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

from .extractor import (
    EMPTY_PACKING_LIST,
    PACKING_LIST_EXTENSIONS,
    SHIPPING_LINE_OPTIONS,
    build_rows,
    carrier_display,
    extract_arrival_notice,
    extract_mbl,
    extract_packing_list,
    validate,
)

CLIENT_SLUG = "sealogs"  # matches Client(name="Sealogs") -> slug in database/seed.py
TASK_SLUG = "inbound"

COLUMN_CONFIG = [
    {"header": "External ID",              "field_key": "external_id",        "width": 22},
    {"header": "External ID/Container No", "field_key": "external_container", "width": 32},
    {"header": "Container No",             "field_key": "container_no",       "width": 16},
    {"header": "Shipping Line",            "field_key": "shipping_line",      "width": 34},
    {"header": "Ship Name",                "field_key": "ship_name",          "width": 20},
    {"header": "Product",                  "field_key": "product",            "width": 14},
    {"header": "Goods Type",               "field_key": "goods_type",         "width": 13},
    {"header": "Qty",                      "field_key": "qty",                "width": 10, "num_format": "#,##0"},
    {"header": "Gross Weight (KG)",        "field_key": "gross_weight",       "width": 18, "num_format": "#,##0.00"},
    {"header": "Net Weight (KG)",          "field_key": "net_weight",         "width": 16, "num_format": "#,##0.00"},
    {"header": "ETA/Date",                 "field_key": "eta_date",           "width": 20},
]

OUTPUT_FILENAME = "Sealogs_Inbound_Outcome.xlsx"

# <input type="date"> submits "YYYY-MM-DD"; the OP column reads
# "YYYYMMDD 00:00:00" (same convention as every other Inbound task).
ETA_DATE_INPUT_FORMAT = "%Y-%m-%d"
ETA_DATE_OUTPUT_FORMAT = "%Y%m%d 00:00:00"


def format_eta_date(raw: str) -> str:
    return datetime.strptime(raw.strip(), ETA_DATE_INPUT_FORMAT).strftime(ETA_DATE_OUTPUT_FORMAT)


class SealogsInboundTask(BaseTask):
    client_slug = CLIENT_SLUG
    task_slug = TASK_SLUG
    label = "Sealogs Inbound"

    required_documents = [
        {"key": "mbl",            "label": "MBL (Bill of Lading)", "accept": ".pdf", "multiple": False},
        {"key": "packing_list",   "label": "Packing List (PDF / image)", "accept": ".pdf,.jpg,.jpeg,.png",
         "multiple": False, "required": False},
        {"key": "arrival_notice", "label": "Arrival Notice (PDF)", "accept": ".pdf", "multiple": False,
         "required": False},
    ]

    column_config = COLUMN_CONFIG
    writes_own_output = False

    def process(self, files: dict, output_path: str | None = None, external_id: str = "",
                shipping_line: str = "", ship_name: str = "", eta_date: str = "") -> dict:
        mbl_data = extract_mbl(files["mbl"])
        an_data = extract_arrival_notice(files["arrival_notice"]) if files.get("arrival_notice") else None
        ref_ids = [c["id"] for c in (an_data or mbl_data).get("containers", []) if c.get("id")]
        pl_data = extract_packing_list(files["packing_list"], ref_ids) if files.get("packing_list") else dict(EMPTY_PACKING_LIST)

        validation = validate(mbl_data, pl_data, an_data)
        rows = build_rows(mbl_data, pl_data, an_data, external_id, shipping_line, ship_name, eta_date)

        summary = {
            "mbl_no":           mbl_data.get("mbl_no", ""),
            "external_id":      external_id,
            "shipping_line":    shipping_line,
            "ship_name":        ship_name,
            "eta_date":         eta_date,
            "total_rows":       len(rows),
            "total_containers": len({r["container_no"] for r in rows}),
            "total_qty":        sum(r["qty"] for r in rows),
            "total_gross":      round(sum(r["gross_weight"] for r in rows), 3),
            "total_net":        round(sum(r["net_weight"] for r in rows), 3),
            "validation":       validation,
        }
        return {"rows": rows, "summary": summary}


_task = SealogsInboundTask()

bp = Blueprint(
    "sealogs_inbound",
    __name__,
    template_folder="templates",
    url_prefix="/app/sealogs/inbound",
)


@bp.route("/")
def index():
    return render_template("sealogs_inbound/index.html", shipping_lines=SHIPPING_LINE_OPTIONS)


@bp.route("/process", methods=["POST"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def process():
    mbl_file = request.files.get("mbl")
    if not mbl_file or not mbl_file.filename:
        return jsonify({"error": "Missing document: MBL (Bill of Lading)"}), 400
    pkl_file = request.files.get("packing_list")
    has_pkl = bool(pkl_file and pkl_file.filename)   # packing list is optional
    an_file = request.files.get("arrival_notice")
    has_an = bool(an_file and an_file.filename)
    checks = [(mbl_file, "MBL")]
    if has_an:
        checks.append((an_file, "Arrival Notice"))
    for f, label in checks:
        if not f.filename.lower().endswith(".pdf"):
            return jsonify({"error": f"{label} must be a PDF file."}), 400
    if has_pkl and not pkl_file.filename.lower().endswith(PACKING_LIST_EXTENSIONS):
        return jsonify({"error": "Packing List must be a PDF or an image (JPG / PNG)."}), 400

    external_id = (request.form.get("external_id") or "").strip()
    if not external_id:
        return jsonify({"error": "External ID is required."}), 400

    shipping_line_alias = (request.form.get("shipping_line") or "").strip()
    if shipping_line_alias not in SHIPPING_LINE_OPTIONS:
        return jsonify({"error": "Select a valid Shipping Line."}), 400
    shipping_line = carrier_display(shipping_line_alias)  # alias -> full OP spelling

    ship_name = (request.form.get("ship_name") or "").strip()
    if not ship_name:
        return jsonify({"error": "Ship Name is required."}), 400

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
        files = {}
        for key, f in (("mbl", mbl_file), ("packing_list", pkl_file if has_pkl else None)):
            if f is None:
                continue
            files[key] = str(job_dir / secure_filename(f.filename))
            f.save(files[key])
        if has_an:
            files["arrival_notice"] = str(job_dir / secure_filename(an_file.filename))
            an_file.save(files["arrival_notice"])

        output_path = str(job_output_path(job_id))
        result = _task.process(files, output_path, external_id=external_id, shipping_line=shipping_line,
                               ship_name=ship_name, eta_date=eta_date)
        rows = result["rows"]
        summary = result["summary"]

        write_excel(rows, COLUMN_CONFIG, output_path)

        reference_val, reference_count = build_reference([external_id])
        names = [mbl_file.filename] + ([pkl_file.filename] if has_pkl else []) + ([an_file.filename] if has_an else [])
        log_job(session, g.user["user_id"], CLIENT_SLUG, TASK_SLUG, f"{job_id}/output.xlsx", "success",
                reference=reference_val, source_filename=", ".join(names),
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
