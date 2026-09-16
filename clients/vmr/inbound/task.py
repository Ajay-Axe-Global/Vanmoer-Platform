"""
VMR Clients Inbound — task module.

ONE Client ("VMR Clients" / slug "vmr") + ONE task ("inbound"), covering
THREE underlying customers picked from a UI dropdown (see CUSTOMERS below)
rather than three separate clients — confirmed with the user: Dashbach and
Hakotrans share identical extraction/output logic, only Karl Gross differs
(no Packing List, fewer output columns). The dropdown value drives:

  - which documents are required (packing_list only for Dashbach/Hakotrans)
  - which output columns are written (COLUMN_CONFIG_KARL_GROSS vs
    COLUMN_CONFIG_FULL)

See clients/vmr/inbound/extractor.py for the full extraction-logic writeup
(carrier-specific MBL prompts, Packing List's per-container TOTAL-row-only
reading, Public ID computation, the Net=Gross convention).

Reference, Ship Name, ETA Date, ETD Date are all REQUIRED manual UI
fields — neither document carries a usable shipment reference, ship name, or
either date, same convention as Continental/Swiss Inbound's UI-picked
fields, applied uniformly to every row. Shipping Line is NOT a UI field —
build_rows() derives it from the MBL's own already-identified carrier.
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

from .extractor import build_rows, carrier_display, extract_mbl, extract_packing_list, validate

CLIENT_SLUG = "vmr_clients"  # matches Client(name="VMR Clients") -> _slugify() in database/seed.py
TASK_SLUG = "inbound"

# ═══════════════════════════════════════════════════════════════════════════
# CUSTOMER CONFIG
# ═══════════════════════════════════════════════════════════════════════════

CUSTOMERS = {
    "karl_gross": {"label": "Karl Gross", "needs_packing_list": False},
    "dashbach":   {"label": "Dashbach",   "needs_packing_list": True},
    "hakotrans":  {"label": "Hakotrans",  "needs_packing_list": True},
}

# ═══════════════════════════════════════════════════════════════════════════
# COLUMN CONFIG — two variants, picked per request by CUSTOMERS[..]["needs_packing_list"]
# ═══════════════════════════════════════════════════════════════════════════

COLUMN_CONFIG_BASE = [
    {"header": "Reference",     "field_key": "reference",     "width": 22},
    {"header": "Container No",  "field_key": "container_no",  "width": 16},
    {"header": "Container/Ref", "field_key": "container_ref", "width": 30},
    {"header": "Public ID",     "field_key": "public_id",     "width": 20},
    {"header": "Seal No",       "field_key": "seal_no",       "width": 14},
    {"header": "Shipping Line", "field_key": "shipping_line", "width": 16},
    {"header": "Ship Name",     "field_key": "ship_name",     "width": 20},
    {"header": "ETA/Date",      "field_key": "eta_date",      "width": 20},
    {"header": "ETD/Date",      "field_key": "etd_date",      "width": 20},
]

COLUMN_CONFIG_KARL_GROSS = COLUMN_CONFIG_BASE

COLUMN_CONFIG_FULL = COLUMN_CONFIG_BASE + [
    {"header": "Product",              "field_key": "product",       "width": 22},
    {"header": "Product Qty",          "field_key": "product_qty",   "width": 12, "num_format": "#,##0"},
    {"header": "Net Weight (KG)",      "field_key": "net_weight",    "width": 16, "num_format": "#,##0"},
    {"header": "Gross Weight (KG)",    "field_key": "gross_weight",  "width": 16, "num_format": "#,##0"},
]

# HTML <input type="date"> always submits "YYYY-MM-DD" regardless of browser
# locale; the OP column must read "YYYYMMDD 00:00:00" (same convention as
# Sabic/Vinmar/Emvia/Continental/Swiss Inbound) — applied to both ETA and
# ETD here.
DATE_INPUT_FORMAT = "%Y-%m-%d"
DATE_OUTPUT_FORMAT = "%Y%m%d 00:00:00"


def format_date(raw: str) -> str:
    """Convert a UI-submitted 'YYYY-MM-DD' date into the OP format."""
    return datetime.strptime(raw.strip(), DATE_INPUT_FORMAT).strftime(DATE_OUTPUT_FORMAT)


# ═══════════════════════════════════════════════════════════════════════════
# TASK CLASS
# ═══════════════════════════════════════════════════════════════════════════

class VmrInboundTask(BaseTask):
    client_slug = CLIENT_SLUG
    task_slug = TASK_SLUG
    label = "VMR Clients Inbound"

    # Packing List is NEVER hard-required — for Karl Gross it's simply
    # unused (no product/qty/net/gross columns exist for that customer at
    # all), and for Dashbach/Hakotrans it's an OPTIONAL upload: Product
    # still comes from the MBL either way, but Product Qty/Net/Gross are
    # left blank on every row when it's skipped (see extractor.build_rows()).
    required_documents = [
        {"key": "mbl", "label": "MBL (Bill of Lading)", "accept": ".pdf", "multiple": False},
        {"key": "packing_list", "label": "Packing List (optional for Dashbach/Hakotrans)", "accept": ".pdf", "multiple": False},
    ]

    column_config = COLUMN_CONFIG_FULL
    writes_own_output = False

    def process(self, files: dict, output_path: str | None = None, customer: str = "",
                reference: str = "", ship_name: str = "", eta_date: str = "", etd_date: str = "") -> dict:
        customer_cfg = CUSTOMERS[customer]
        needs_packing_list = customer_cfg["needs_packing_list"]

        # ── Step 1: extraction ──────────────────────────────────────
        mbl_data = extract_mbl(files["mbl"])
        pkl_data = extract_packing_list(files["packing_list"]) if needs_packing_list and files.get("packing_list") else None

        # ── Step 2: validation + row-building ───────────────────────
        validation = validate(mbl_data, pkl_data, needs_packing_list)
        rows = build_rows(mbl_data, pkl_data, needs_packing_list, reference, ship_name, eta_date, etd_date)

        # ── Summary stats ───────────────────────────────────────────
        containers = set(r["container_no"] for r in rows)
        summary = {
            "customer":         customer_cfg["label"],
            "eta_date":         eta_date,
            "etd_date":         etd_date,
            "shipping_line":    carrier_display(mbl_data.get("carrier", "")),
            "ship_name":        ship_name,
            "mbl_no":           mbl_data.get("mbl_no", ""),
            "carrier":          mbl_data.get("carrier", ""),
            "product":          mbl_data.get("product", ""),
            "reference":        reference,
            "total_rows":       len(rows),
            "total_containers": len(containers),
            "validation":       validation,
        }
        if needs_packing_list:
            # `product_qty`/`net_weight` are "" (blank) on every row when no
            # Packing List was uploaded — `or 0` coerces both that and a
            # real 0 to 0 for the summary tiles without erroring on str+int.
            summary["total_product_qty"] = sum((r.get("product_qty") or 0) for r in rows)
            summary["total_net_weight"] = round(sum((r.get("net_weight") or 0) for r in rows), 3)

        return {"rows": rows, "summary": summary}


# ═══════════════════════════════════════════════════════════════════════════
# BLUEPRINT
# ═══════════════════════════════════════════════════════════════════════════

_task = VmrInboundTask()

bp = Blueprint(
    "vmr_inbound",
    __name__,
    template_folder="templates",
    url_prefix="/app/vmr_clients/inbound",
)


@bp.route("/")
def index():
    return render_template("vmr_inbound/index.html")


@bp.route("/process", methods=["POST"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def process():
    customer = (request.form.get("customer") or "").strip().lower()
    if customer not in CUSTOMERS:
        return jsonify({"error": "Please select a Customer."}), 400
    customer_cfg = CUSTOMERS[customer]
    needs_packing_list = customer_cfg["needs_packing_list"]

    mbl_file = request.files.get("mbl")
    if not mbl_file or not mbl_file.filename:
        return jsonify({"error": "Missing document: MBL (Bill of Lading)"}), 400

    # Packing List is OPTIONAL for Dashbach/Hakotrans (Karl Gross never uses
    # it at all) — skipping it just leaves Product Qty/Net/Gross blank on
    # every row (see extractor.build_rows()), never a validation error.
    pkl_file = request.files.get("packing_list")
    has_pkl = bool(pkl_file and pkl_file.filename)

    reference = (request.form.get("reference") or "").strip()
    if not reference:
        return jsonify({"error": "Reference is required."}), 400

    ship_name = (request.form.get("ship_name") or "").strip()
    if not ship_name:
        return jsonify({"error": "Ship Name is required."}), 400

    eta_date_raw = (request.form.get("eta_date") or "").strip()
    if not eta_date_raw:
        return jsonify({"error": "ETA Date is required."}), 400
    etd_date_raw = (request.form.get("etd_date") or "").strip()
    if not etd_date_raw:
        return jsonify({"error": "ETD Date is required."}), 400
    try:
        eta_date = format_date(eta_date_raw)
        etd_date = format_date(etd_date_raw)
    except ValueError:
        return jsonify({"error": "Invalid ETA/ETD Date."}), 400

    job_id, job_dir = new_job_dir()
    session = SessionLocal()
    try:
        # ── Save uploaded files ─────────────────────────────────────
        saved = {}
        mbl_path = str(job_dir / secure_filename(mbl_file.filename))
        mbl_file.save(mbl_path)
        saved["mbl"] = mbl_path

        if has_pkl:
            pkl_path = str(job_dir / secure_filename(pkl_file.filename))
            pkl_file.save(pkl_path)
            saved["packing_list"] = pkl_path

        # ── Run the task ────────────────────────────────────────────
        output_path = str(job_output_path(job_id))
        result = _task.process(saved, output_path, customer=customer, reference=reference,
                                ship_name=ship_name, eta_date=eta_date, etd_date=etd_date)

        rows = result["rows"]
        summary = result["summary"]

        # ── Write Excel ─────────────────────────────────────────────
        column_config = COLUMN_CONFIG_FULL if needs_packing_list else COLUMN_CONFIG_KARL_GROSS
        write_excel(rows, column_config, output_path)

        # ── Log the job ─────────────────────────────────────────────
        reference_val, reference_count = build_reference([summary.get("reference") or ""])
        source_filename = mbl_file.filename + (f", {pkl_file.filename}" if has_pkl else "")
        log_job(session, g.user["user_id"], CLIENT_SLUG, TASK_SLUG, f"{job_id}/output.xlsx", "success",
                reference=reference_val, source_filename=source_filename,
                row_count=len(rows), reference_count=reference_count)

        output_filename = f"VMR_{customer_cfg['label'].replace(' ', '')}_Inbound_Outcome.xlsx"
        return jsonify({
            "success": True,
            "job_id": job_id,
            "summary": summary,
            "output_file": output_filename,
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
    return send_file(path, as_attachment=True, download_name="VMR_Inbound_Outcome.xlsx")
