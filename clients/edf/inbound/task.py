"""
EDF Inbound (ED&F Man) — task module.

Source: the Packing List Excel (.xlsx/.xls) in either the OLD layout (lots in
the sheet) or the NEW layout (Input_Data + Notice of Arrival sheets; lots and
bags come from a lot-table image — uploaded, else the one embedded in the
workbook). The optional Arrival Notice PDF gives Seal No. Reference, Shipping Line, Ship Name and ETA Date are REQUIRED UI
fields (see extractor.py for why Shipping Line is a dropdown here).
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

from .arrival_notice import extract_seals
from pathlib import Path

from .excel_extractor import extract_embedded_image, extract_packing_list_excel
from .extractor import (
    FALLBACK_FIELDS,
    SHIPPING_LINE_OPTIONS,
    build_rows,
    carrier_display,
    validate,
    validate_fallbacks,
    validate_seals,
)
from .lot_image import apply_lot_image, extract_lot_image

CLIENT_SLUG = "edf"
TASK_SLUG = "inbound"

COLUMN_CONFIG = [
    {"header": "Reference",          "field_key": "reference",      "width": 22},
    {"header": "Container No",       "field_key": "container_no",   "width": 16},
    {"header": "MBL / Container No", "field_key": "mbl_container",  "width": 32},
    {"header": "Seal No",            "field_key": "seal_no",        "width": 14},
    {"header": "Container Type",     "field_key": "container_type", "width": 14},
    {"header": "Shipping Line",      "field_key": "shipping_line",  "width": 34},
    {"header": "Ship Name",          "field_key": "ship_name",      "width": 20},
    {"header": "Product",            "field_key": "product",        "width": 34},
    {"header": "Lot",                "field_key": "lot_no",         "width": 16},
    {"header": "Bags Qty",           "field_key": "bags_qty",       "width": 12, "num_format": "#,##0"},
    {"header": "ETA/Date",           "field_key": "eta_date",       "width": 20},
    {"header": "Bill of Lading",     "field_key": "bill_of_lading",    "width": 18},
    {"header": "Origin",             "field_key": "origin",            "width": 16},
    {"header": "Formal processor",   "field_key": "formal_processor",  "width": 22},
    {"header": "Purchase Contract",  "field_key": "purchase_contract", "width": 18},
    {"header": "Origin Cuba/Other",  "field_key": "origin_cuba_other", "width": 18},
]

OUTPUT_FILENAME = "EDF_Inbound_Outcome.xlsx"

# HTML <input type="date"> submits "YYYY-MM-DD"; OP column reads
# "YYYYMMDD 00:00:00" (same convention as every other Inbound task).
ETA_DATE_INPUT_FORMAT = "%Y-%m-%d"
ETA_DATE_OUTPUT_FORMAT = "%Y%m%d 00:00:00"


def format_eta_date(raw: str) -> str:
    return datetime.strptime(raw.strip(), ETA_DATE_INPUT_FORMAT).strftime(ETA_DATE_OUTPUT_FORMAT)


class EdfInboundTask(BaseTask):
    client_slug = CLIENT_SLUG
    task_slug = TASK_SLUG
    label = "EDF Inbound"

    required_documents = [
        {"key": "packing_list", "label": "Packing List (Excel)", "accept": ".xlsx,.xls", "multiple": False},
        # New format only: lot table image (falls back to the one embedded in the Excel).
        {"key": "lot_image", "label": "Lot table image", "accept": ".png,.jpg,.jpeg,.webp", "multiple": False,
         "required": False},
        # Optional: only source of the Seal No column.
        {"key": "arrival_notice", "label": "Arrival Notice (PDF)", "accept": ".pdf", "multiple": False,
         "required": False},
    ]

    column_config = COLUMN_CONFIG
    writes_own_output = False

    def process(self, files: dict, output_path: str | None = None, reference: str = "",
                shipping_line: str = "", ship_name: str = "", eta_date: str = "",
                fallbacks: dict | None = None) -> dict:
        fallbacks = fallbacks or {}
        packing_list = extract_packing_list_excel(files["packing_list"])

        image_path = files.get("lot_image")
        if not image_path and packing_list["format"] == "new":
            embedded = str(Path(files["packing_list"]).parent / "embedded_lot_image.png")
            if not extract_embedded_image(files["packing_list"], embedded):
                raise ValueError("New-format Packing List needs the lot table image: upload it "
                                 "(none is embedded in the Excel).")
            image_path = embedded
        image_validation = []
        if image_path:
            image_validation = apply_lot_image(packing_list, extract_lot_image(image_path))

        arrival_notice = extract_seals(files["arrival_notice"]) if files.get("arrival_notice") else None
        validation = (image_validation + validate(packing_list) + validate_fallbacks(packing_list, fallbacks)
                      + validate_seals(packing_list, arrival_notice))
        rows = build_rows(packing_list, reference, shipping_line, ship_name, eta_date,
                          seals=arrival_notice["seals"] if arrival_notice else None, fallbacks=fallbacks)

        summary = {
            "reference":        reference,
            "shipping_line":    shipping_line,
            "ship_name":        ship_name,
            "eta_date":         eta_date,
            "total_rows":       len(rows),
            "total_containers": len({r["container_no"] for r in rows}),
            "total_bags":       sum(r["bags_qty"] for r in rows),
            "validation":       validation,
        }
        return {"rows": rows, "summary": summary}


_task = EdfInboundTask()

bp = Blueprint(
    "edf_inbound",
    __name__,
    template_folder="templates",
    url_prefix="/app/edf/inbound",
)


@bp.route("/")
def index():
    return render_template("edf_inbound/index.html", shipping_lines=SHIPPING_LINE_OPTIONS)


@bp.route("/process", methods=["POST"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def process():
    f = request.files.get("packing_list")
    if not f or not f.filename:
        return jsonify({"error": "Missing document: Packing List (Excel)"}), 400
    if not f.filename.lower().endswith((".xlsx", ".xls")):
        return jsonify({"error": "Packing List must be an Excel file (.xlsx / .xls)."}), 400

    an = request.files.get("arrival_notice")
    has_an = bool(an and an.filename)
    if has_an and not an.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Arrival Notice must be a PDF file."}), 400

    img = request.files.get("lot_image")
    has_img = bool(img and img.filename)
    if has_img and not img.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
        return jsonify({"error": "Lot table image must be .png, .jpg, .jpeg or .webp."}), 400

    # Optional: used only where the Excel has no column / a blank cell.
    fallbacks = {k: (request.form.get(k) or "").strip() for k in FALLBACK_FIELDS}

    reference = (request.form.get("reference") or "").strip()
    if not reference:
        return jsonify({"error": "Reference is required."}), 400

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
        path = str(job_dir / secure_filename(f.filename))
        f.save(path)

        files = {"packing_list": path}
        if has_an:
            an_path = str(job_dir / secure_filename(an.filename))
            an.save(an_path)
            files["arrival_notice"] = an_path

        if has_img:
            img_path = str(job_dir / secure_filename(img.filename))
            img.save(img_path)
            files["lot_image"] = img_path

        output_path = str(job_output_path(job_id))
        result = _task.process(files, output_path, reference=reference,
                                shipping_line=shipping_line, ship_name=ship_name, eta_date=eta_date,
                                fallbacks=fallbacks)

        rows = result["rows"]
        summary = result["summary"]

        write_excel(rows, COLUMN_CONFIG, output_path)

        reference_val, reference_count = build_reference([summary.get("reference") or ""])
        log_job(session, g.user["user_id"], CLIENT_SLUG, TASK_SLUG, f"{job_id}/output.xlsx", "success",
                reference=reference_val, source_filename=f.filename,
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
