"""
Client: Carpenter Technology
Task: Inbound
Documents: Order List (Excel), Arrival Notice PDF(s) (optional), Packing List PDF(s)

Ported from the standalone Carpenter repo. carpenter_core.py holds the
orchestration/merge/validate logic verbatim; arrival_notice.py and
scanned_doc.py were migrated from raw REST Gemini calls to the shared
helpers/gemini_client.py SDK wrapper (see prompts.py for the two prompts).

Unlike Sabic, this task writes its own fully-styled Excel report inside
process_shipment() rather than returning row dicts — hence writes_own_output.
"""

from pathlib import Path

from flask import Blueprint, g, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from clients.carpenter.inbound.carpenter_core import process_shipment
from database.db import SessionLocal
from helpers.base_task import BaseTask
from helpers.decorators import task_access_required
from helpers.jobs import job_output_path, log_job, new_job_dir

CLIENT_SLUG = "carpenter"
TASK_SLUG = "inbound"


class CarpenterInboundTask(BaseTask):
    client_slug = CLIENT_SLUG
    task_slug = TASK_SLUG
    label = "Carpenter — Inbound"
    writes_own_output = True

    required_documents = [
        {"key": "order_list", "label": "Order List (Excel)", "accept": ".xlsx,.xls", "multiple": False},
        {"key": "arrival_notices", "label": "Arrival Notice PDF(s) (optional)", "accept": ".pdf", "multiple": True},
        {"key": "packing_lists", "label": "Packing List PDF(s)", "accept": ".pdf", "multiple": True},
    ]

    def process(self, files: dict, output_path: str | None = None) -> dict:
        order_path = files["order_list"]
        arrival_paths = files.get("arrival_notices", [])
        packing_paths = files["packing_lists"]
        summary = process_shipment(order_path, arrival_paths, packing_paths, output_path)
        return {"rows": [], "summary": summary}


# ── Flask Blueprint ──────────────────────────────────────────────────────────
bp = Blueprint(
    "carpenter_inbound", __name__,
    url_prefix=f"/app/{CLIENT_SLUG}/{TASK_SLUG}",
    template_folder="templates",
)

_task = CarpenterInboundTask()


@bp.route("/")
def index():
    # Namespaced under carpenter_inbound/ — see the matching comment in
    # clients/sabic/outbound/task.py for why this can't be a bare "index.html".
    return render_template("carpenter_inbound/index.html", label=_task.label)


@bp.route("/process", methods=["POST"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def process():
    if "order_list" not in request.files or not request.files["order_list"].filename:
        return jsonify({"error": "Order List Excel file is required."}), 400
    if "packing_lists" not in request.files:
        return jsonify({"error": "At least one Packing List PDF is required."}), 400

    job_id, job_dir = new_job_dir()
    session = SessionLocal()
    try:
        order_file = request.files["order_list"]
        if not order_file.filename.lower().endswith((".xlsx", ".xls")):
            return jsonify({"error": "Order List must be an Excel file (.xlsx)"}), 400
        order_path = str(job_dir / secure_filename(order_file.filename))
        order_file.save(order_path)

        arrival_paths = []
        for af in request.files.getlist("arrival_notices"):
            if af.filename.lower().endswith(".pdf"):
                p = str(job_dir / secure_filename(af.filename))
                af.save(p)
                arrival_paths.append(p)

        packing_paths = []
        for pf in request.files.getlist("packing_lists"):
            if pf.filename.lower().endswith(".pdf"):
                p = str(job_dir / secure_filename(pf.filename))
                pf.save(p)
                packing_paths.append(p)

        if not packing_paths:
            return jsonify({"error": "At least one valid Packing List PDF is required."}), 400

        output_path = str(job_output_path(job_id))
        result = _task.process(
            {"order_list": order_path, "arrival_notices": arrival_paths, "packing_lists": packing_paths},
            output_path=output_path,
        )

        log_job(session, g.user["user_id"], CLIENT_SLUG, TASK_SLUG, f"{job_id}/output.xlsx", "success")
        return jsonify({
            "success": True,
            "summary": result["summary"],
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
    return send_file(path, as_attachment=True, download_name="Shipment_Output.xlsx")
