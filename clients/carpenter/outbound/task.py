"""
Client: Carpenter Technology
Task: Outbound
Document: order-table screenshot (PNG/JPG) — a screenshot of the shipping
system's order grid (16 REF, ORDER#, INCO, VOY, VESSEL, ..., CONSIGNEE,
TCS REF).

Column mapping:
    Reference  <- TCS REF column (fallback: CONSIGNEE)
    Doc Type   <- INCO column: DAP -> T1, DDP -> IMAH

See outbound.py for the extraction-to-row grouping rule (one row per
distinct Doc Type; collapses to a single row when the sheet only has one).
"""

import re

from flask import Blueprint, g, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from clients.carpenter.outbound.outbound import build_rows
from clients.carpenter.outbound.scanner import scan_table_image
from database.db import SessionLocal
from helpers.base_task import BaseTask
from helpers.decorators import task_access_required
from helpers.excel_writer import write_excel
from helpers.jobs import job_output_path, log_job, new_job_dir

CLIENT_SLUG = "carpenter"
TASK_SLUG = "outbound"


class CarpenterOutboundTask(BaseTask):
    client_slug = CLIENT_SLUG
    task_slug = TASK_SLUG
    label = "Carpenter — Outbound"

    required_documents = [
        {"key": "table_image", "label": "Order Table Screenshot (PNG/JPG)", "accept": ".png,.jpg,.jpeg", "multiple": False},
    ]

    column_config = [
        {"header": "Reference", "field_key": "reference", "width": 24},
        {"header": "Doc Type", "field_key": "doc_type", "width": 14},
    ]

    def process(self, files: dict, output_path: str | None = None) -> dict:
        image_path = files["table_image"]
        raw_rows = scan_table_image(image_path)
        rows = build_rows(raw_rows)
        return {
            "rows": rows,
            "summary": {"source_rows": len(raw_rows), "output_rows": len(rows)},
        }


# ── Flask Blueprint ──────────────────────────────────────────────────────────
bp = Blueprint(
    "carpenter_outbound", __name__,
    url_prefix=f"/app/{CLIENT_SLUG}/{TASK_SLUG}",
    template_folder="templates",
)

_task = CarpenterOutboundTask()


@bp.route("/")
def index():
    # Namespaced under carpenter_outbound/ — see the matching comment in
    # clients/sabic/outbound/task.py for why this can't be a bare "index.html".
    return render_template("carpenter_outbound/index.html", label=_task.label)


def _download_name(rows: list[dict]) -> str:
    refs = "_".join(r["reference"] for r in rows if r.get("reference"))
    safe = re.sub(r"[^\w\-]", "_", refs).strip("_") or "Carpenter_Outbound_Output"
    return f"{safe}.xlsx"


@bp.route("/process", methods=["POST"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def process():
    f = request.files.get("table_image")
    if not f or not f.filename:
        return jsonify({"error": "A table screenshot (PNG/JPG) is required."}), 400
    if not f.filename.lower().endswith((".png", ".jpg", ".jpeg")):
        return jsonify({"error": "File must be a PNG or JPG image."}), 400

    job_id, job_dir = new_job_dir()
    session = SessionLocal()
    try:
        image_path = str(job_dir / secure_filename(f.filename))
        f.save(image_path)

        result = _task.process({"table_image": image_path})
        rows = result["rows"]
        if not rows:
            raise RuntimeError("Could not read any reference/Doc Type rows from the image.")

        write_excel(rows, _task.column_config, str(job_output_path(job_id)))

        log_job(session, g.user["user_id"], CLIENT_SLUG, TASK_SLUG, f"{job_id}/output.xlsx", "success")
        return jsonify({
            "success": True,
            "summary": result["summary"],
            "rows": rows,
            "download_url": f"/app/{CLIENT_SLUG}/{TASK_SLUG}/download/{job_id}",
            "download_name": _download_name(rows),
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
    return send_file(path, as_attachment=True, download_name="Carpenter_Outbound_Output.xlsx")
