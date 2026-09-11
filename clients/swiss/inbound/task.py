"""
Swiss Inbound — task module.

Follows the same two-part structure as clients/continental/inbound/task.py:
  1. SwissInboundTask(BaseTask)  — extraction, validation, Excel output
  2. Flask Blueprint              — index / process / download routes

The Packing List extraction path is chosen from the uploaded file's own
extension alone (never a UI-trusted flag) — same convention as Emvia
Inbound's routing:
  - .xlsx/.xls -> extract_packing_list_excel() (excel_extractor.py,
    pandas-based, no LLM).
  - .pdf       -> extract_packing_list_pdf() (extractor.py, Gemini-based,
    self-detects the SIDPEC vs ETHYDCO layout).

Only two source documents are REQUIRED (MBL + Packing List — no Invoice),
plus a third, OPTIONAL, batch upload: up to MAX_INBOUND_FILES "Inbound"
advice PDFs (one per product in the shipment, e.g. "EB101046.10_INBOUND.pdf"
— see extractor.extract_inbound_advice()). Each one states its own
per-product Reference (e.g. "EB101046.10") for one Material — matched
against the Packing List's Grade/product column to populate the "Product
Reference" output column and, in turn, the "Container/Ref" column (which
uses Product Reference in place of the global Reference wherever a match
was found — see extractor.build_rows()). If no Inbound files are uploaded
at all, Product Reference is blank on every row and Container/Ref falls
back to the global Reference, same as before this feature existed.

Reference, Ship Name, and ETA Date are all REQUIRED manual UI fields (like
Continental Inbound's Reference/ETA Date) — neither document carries a
usable shipment reference to fall back to. Shipping Line is NOT a UI field
— extractor.build_rows() derives it from the MBL's already-identified
carrier (see extractor.carrier_display()), so it can never disagree with
the MBL.
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
from .extractor import (
    build_product_reference_map,
    build_rows,
    carrier_display,
    extract_inbound_advice,
    extract_mbl,
    extract_packing_list_pdf,
    validate,
    validate_product_references,
)

EXCEL_EXTENSIONS = {".xlsx", ".xls"}

CLIENT_SLUG = "swiss"
TASK_SLUG = "inbound"

MAX_INBOUND_FILES = 15

# ═══════════════════════════════════════════════════════════════════════════
# COLUMN CONFIG
# ═══════════════════════════════════════════════════════════════════════════

COLUMN_CONFIG = [
    {"header": "Reference",        "field_key": "reference",      "width": 22},
    {"header": "Container No",     "field_key": "container_no",   "width": 16},
    {"header": "Container/Ref",    "field_key": "container_ref",  "width": 34},
    {"header": "Shipping Line",    "field_key": "shipping_line",  "width": 16},
    {"header": "Bl No",            "field_key": "mbl_no",         "width": 20},
    {"header": "Seal No",          "field_key": "seal_no",        "width": 14},
    {"header": "Container Type",   "field_key": "container_type", "width": 14},
    {"header": "Country Code",     "field_key": "country_code",   "width": 10},
    {"header": "Product",          "field_key": "product",        "width": 18},
    {"header": "Product Reference", "field_key": "product_reference", "width": 20},
    {"header": "Lot No",           "field_key": "lot_no",         "width": 16},
    {"header": "Bags",             "field_key": "bags",           "width": 10, "num_format": "#,##0"},
    {"header": "Net Weight (KG)",  "field_key": "net_weight",     "width": 16, "num_format": "#,##0"},
    {"header": "Gross Weight (KG)", "field_key": "gross_weight",  "width": 16, "num_format": "#,##0"},
    {"header": "Pallet Qty",       "field_key": "pallet_qty",     "width": 12, "num_format": "#,##0"},
    {"header": "Ship Name",        "field_key": "ship_name",      "width": 20},
    {"header": "ETA/Date",         "field_key": "eta_date",       "width": 20},
]

OUTPUT_FILENAME = "Swiss_Inbound_Outcome.xlsx"

# HTML <input type="date"> always submits "YYYY-MM-DD" regardless of browser
# locale; the OP column must read "YYYYMMDD 00:00:00" (same convention as
# Sabic/Vinmar/Emvia/Continental Inbound).
ETA_DATE_INPUT_FORMAT = "%Y-%m-%d"
ETA_DATE_OUTPUT_FORMAT = "%Y%m%d 00:00:00"


def format_eta_date(raw: str) -> str:
    """Convert a UI-submitted 'YYYY-MM-DD' ETA date into the OP format."""
    return datetime.strptime(raw.strip(), ETA_DATE_INPUT_FORMAT).strftime(ETA_DATE_OUTPUT_FORMAT)


# ═══════════════════════════════════════════════════════════════════════════
# TASK CLASS
# ═══════════════════════════════════════════════════════════════════════════

class SwissInboundTask(BaseTask):
    client_slug = "swiss"
    task_slug = "inbound"
    label = "Swiss Inbound"

    required_documents = [
        {"key": "mbl",          "label": "MBL (Bill of Lading)", "accept": ".pdf", "multiple": False},
        {"key": "packing_list", "label": "Packing List",         "accept": ".pdf,.xlsx,.xls", "multiple": False},
    ]

    column_config = COLUMN_CONFIG
    writes_own_output = False

    def process(self, files: dict, output_path: str | None = None, reference: str = "",
                eta_date: str = "", ship_name: str = "", inbound_files: list[str] = None) -> dict:
        # ── Step 1: extraction ──────────────────────────────────────
        # MBL extraction is a two-call pipeline internally (identify the
        # carrier, then dispatch to that carrier's own tuned prompt) — see
        # extractor.extract_mbl().
        mbl_data = extract_mbl(files["mbl"])

        pkl_path = files["packing_list"]
        if Path(pkl_path).suffix.lower() in EXCEL_EXTENSIONS:
            pkl_data = extract_packing_list_excel(pkl_path)
        else:
            mbl_container_ids = [c["id"] for c in mbl_data.get("containers", []) if c.get("id")]
            pkl_data = extract_packing_list_pdf(pkl_path, mbl_container_ids=mbl_container_ids)

        # Optional per-product Inbound advice PDFs — one Gemini call each,
        # {reference, material} extracted independently, then collapsed
        # into a {product code: reference} map (see extractor.
        # build_product_reference_map() for the code-matching rule).
        inbound_advices = [extract_inbound_advice(p) for p in (inbound_files or [])]
        product_reference_map = build_product_reference_map(inbound_advices)

        # ── Step 3: Build outcome rows ──────────────────────────────
        # reference/eta_date/ship_name are UI-entered (not extracted from
        # the documents) and apply uniformly to every row in this shipment.
        # Shipping Line is derived inside build_rows() from the MBL's own
        # already-identified carrier — not a UI input. Product Reference
        # (per row) comes from product_reference_map, built above.

        validation = validate(mbl_data, pkl_data)
        rows = build_rows(mbl_data, pkl_data, reference, eta_date, ship_name, product_reference_map)
        validation += validate_product_references(rows, len(inbound_advices))

        # ── Summary stats ───────────────────────────────────────────
        containers = set(r["container_no"] for r in rows)
        total_bags = sum(r["bags"] for r in rows)
        total_pallets = sum(r["pallet_qty"] for r in rows)
        total_net = sum(r["net_weight"] for r in rows)

        summary = {
            "eta_date":         eta_date,
            "shipping_line":    carrier_display(mbl_data.get("carrier", "")),
            "ship_name":        ship_name,
            "mbl_no":           mbl_data.get("mbl_no", ""),
            "carrier":          mbl_data.get("carrier", ""),
            "reference":        reference,
            "packing_list_source": pkl_data.get("packing_list_source", ""),
            "inbound_files_count": len(inbound_advices),
            "total_rows":       len(rows),
            "total_containers": len(containers),
            "total_bags":       total_bags,
            "total_pallets":    total_pallets,
            "total_net_weight": round(total_net, 3),
            "validation":       validation,
        }

        return {"rows": rows, "summary": summary}


# ═══════════════════════════════════════════════════════════════════════════
# BLUEPRINT
# ═══════════════════════════════════════════════════════════════════════════

_task = SwissInboundTask()

bp = Blueprint(
    "swiss_inbound",
    __name__,
    template_folder="templates",
    url_prefix="/app/swiss/inbound",
)


@bp.route("/")
def index():
    return render_template("swiss_inbound/index.html")


@bp.route("/process", methods=["POST"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def process():
    for doc in _task.required_documents:
        f = request.files.get(doc["key"])
        if not f or not f.filename:
            return jsonify({"error": f"Missing document: {doc['label']}"}), 400

    reference = (request.form.get("reference") or "").strip()
    if not reference:
        return jsonify({"error": "Reference is required."}), 400

    ship_name = (request.form.get("ship_name") or "").strip()
    if not ship_name:
        return jsonify({"error": "Ship Name is required."}), 400

    # Optional — a shipment with no Inbound files uploaded just gets a
    # blank Product Reference column (see extractor.build_rows()).
    inbound_uploads = [f for f in request.files.getlist("inbound_files") if f and f.filename]
    if len(inbound_uploads) > MAX_INBOUND_FILES:
        return jsonify({"error": f"Maximum {MAX_INBOUND_FILES} Inbound files allowed "
                                  f"({len(inbound_uploads)} uploaded)."}), 400

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
        saved = {}
        for doc in _task.required_documents:
            f = request.files[doc["key"]]
            path = str(job_dir / secure_filename(f.filename))
            f.save(path)
            saved[doc["key"]] = path

        inbound_paths = []
        for i, f in enumerate(inbound_uploads):
            # secure_filename() alone can collide if two Inbound files
            # share a name (unlikely, but each is a distinct Reference so a
            # silent overwrite would be a real data-loss bug) — prefixed
            # with its position to guarantee uniqueness.
            path = str(job_dir / f"inbound_{i}_{secure_filename(f.filename)}")
            f.save(path)
            inbound_paths.append(path)

        # ── Run the task ────────────────────────────────────────────
        output_path = str(job_output_path(job_id))
        result = _task.process(saved, output_path, reference=reference, eta_date=eta_date,
                                ship_name=ship_name, inbound_files=inbound_paths)

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
