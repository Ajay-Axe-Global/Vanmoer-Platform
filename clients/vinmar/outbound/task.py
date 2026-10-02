"""
Client: VINMAR (Tegral / Axia Plastics — Van Moer loading releases)
Task: Outbound
Document: Delivery / Release Note PDF(s) (one or more per run)

Pure regex extraction, same pattern as clients/sabic/outbound/task.py — no AI
involved. Vinmar's release notes come in 3 slightly different letterheads
(Axia Plastics "Instructions - SR...", Tegral/Profine "Delivery / Release
Note", and a plain "360 Plastics"-style note) but all 3 share the same
underlying field layout: a Sold-to/Ship-to/Trucker header block, a Release
Instruction box (Release No. / Delivery No.), a shipment table (Final Place
of Delivery / Loading Point / packages), an Item table (Item No. / Product
Description / Batch / Quantity / Unit), and — when the loading point is Van
Moer Kallo — a Loading Info page carrying a fixed "IMPORTANT !!!!****...
****!!!" warning line. One set of regexes below handles all 3 layouts.
"""

import re
from datetime import datetime

from flask import Blueprint, g, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from database.db import SessionLocal
from database.models import Client, EmailJob, OrderTracking, ScreenshotJob, Task as TaskModel
from helpers.base_task import BaseTask
from helpers.dates import period_range, utc_iso
from helpers.decorators import task_access_required
from helpers.email_queue import request_emails, retry_failed_emails
from helpers.excel_writer import write_excel
from helpers.jobs import build_reference, job_output_path, log_job, new_job_dir, upsert_order_tracking
from helpers.pdf_utils import extract_text
from helpers.screenshot_queue import (
    SCREENSHOTS_DIR, list_screenshot_files, reference_dirname, request_screenshots, retry_failed,
)

CLIENT_SLUG = "vinmar"
TASK_SLUG = "outbound"

OUTPUT_FILENAME = "Vinmar_Outbound_Output.xlsx"

# Common European country names as they're spelled out in full on the 360
# Plastics-style layout, which — unlike the other two — doesn't print a
# trailing 2-letter ISO code next to the Ship-to postal code.
COUNTRY_NAME_TO_CODE = {
    "NETHERLANDS": "NL", "GERMANY": "DE", "BELGIUM": "BE", "FRANCE": "FR",
    "LUXEMBOURG": "LU", "UNITED KINGDOM": "GB", "SPAIN": "ES", "ITALY": "IT",
    "POLAND": "PL", "AUSTRIA": "AT", "SWITZERLAND": "CH", "DENMARK": "DK",
    "SWEDEN": "SE", "NORWAY": "NO", "IRELAND": "IE", "PORTUGAL": "PT",
    "CZECH REPUBLIC": "CZ", "HUNGARY": "HU", "SLOVAKIA": "SK", "SLOVENIA": "SI",
}

# One item row per line: "<item no> <description> <batch/PO> <qty> <unit>".
# Description and batch are both variable-length tokens, so the description
# is matched non-greedy and lets the regex engine backtrack until batch/qty/
# unit line up at the end of the line (batch commonly contains a dash, e.g.
# "3610667-01"; quantity never does, which is what makes the split
# unambiguous). Deliberately single-line (no DOTALL): letting "." cross line
# breaks here would let the non-greedy description swallow unrelated header
# text from earlier in the document (e.g. "Ship-to Party...") whenever THAT
# line also starts with a word-ish token — it did, in an earlier version of
# this regex. Line-wrapped product descriptions (see ITEM_TABLE_HEADER_RE /
# _item_table_lines() below) are repaired BEFORE this regex ever runs, so it
# can stay strictly single-line and safe.
ITEM_ROW_RE = re.compile(
    r"^\s*([\w\-]+)\s+(.+?)\s+([\w\-]+)\s+([\d.,]+)\s+(T|KG|KGS|MT)\s*$",
    re.MULTILINE,
)

# Marks the start of the Item table — wording/case differs slightly across
# the 3 letterheads ("PRODUCT DESCRIPTION" vs "Product Description").
ITEM_TABLE_HEADER_RE = re.compile(
    r"Item No\.?\s+Product Description\s+Batch\s*\(PO#\)\s+Quantity\s+UNIT",
    re.IGNORECASE,
)

# Matches a complete "<qty> <unit>" token, used to re-split the flattened
# item-table block back into one reconstructed line per item.
_QTY_UNIT_RE = re.compile(r"[\d.,]+\s+(?:KGS|KG|MT|T)\b", re.IGNORECASE)


def _item_table_lines(text: str) -> str:
    """Returns the Item table's rows as one real line per item, repairing
    any product description that got line-wrapped mid-word in the source PDF
    (e.g. Axia Plastics' "ExxonMobil(TM) 7033N (Legacy Name -\\nExxonMo
    3611070-01 27.500 T") — this only touches the Item table block, so it
    can safely flatten every whitespace run (including the wrap) without
    risking bleeding into unrelated sections of the document the way a
    document-wide DOTALL regex would."""
    header = ITEM_TABLE_HEADER_RE.search(text)
    if not header:
        return text  # unrecognized layout — let ITEM_ROW_RE try the raw text as-is

    end = re.search(r"\n\s*(?:Additional Information|Loading Info\.)", text[header.end():], re.IGNORECASE)
    block = text[header.end():header.end() + end.start()] if end else text[header.end():]

    flat = re.sub(r"\s+", " ", block).strip()
    # Insert a real newline right after each item's own "<qty> <unit>" — from
    # there, every character up to the NEXT item's own number starts a fresh
    # reconstructed line, regardless of how many source lines it was wrapped
    # across.
    return _QTY_UNIT_RE.sub(lambda m: m.group(0) + "\n", flat)


def _release_no(t: str) -> str:
    m = re.search(r"Release\s*No\.?\s*:\s*(\S+)", t, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _delivery_no(t: str) -> str:
    m = re.search(r"Delivery\s*No\.?\s*:\s*(\S+)", t, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _trucker_name(t: str) -> str:
    # First non-blank line right after the "Trucker" label — the company
    # name always comes first, any address lines follow on subsequent lines.
    m = re.search(r"Trucker\s*\n\s*(.+?)\s*\n", t, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _ship_to_block(t: str) -> str:
    m = re.search(r"Ship-to Party\s*\n(.*?)\n\s*Trucker", t, re.IGNORECASE | re.DOTALL)
    return m.group(1) if m else ""


def _country_code(t: str) -> str:
    block = _ship_to_block(t)
    # Layouts that print a trailing ISO code next to the postal code, e.g.
    # "6181 MA, NL" or "66954, DE".
    m = re.search(r",\s*([A-Z]{2})\s*$", block.strip())
    if m:
        return m.group(1)
    # Fallback: the country spelled out in full (e.g. "NETHERLANDS").
    for name, code in COUNTRY_NAME_TO_CODE.items():
        if re.search(rf"\b{name}\b", block, re.IGNORECASE):
            return code
    return ""


def _remark(t: str) -> str:
    # Van Moer's Loading Info page wraps its one must-not-miss instruction in
    # "IMPORTANT !!!!****...****!!!" markers (e.g. the anti-slip-mats line).
    # Layouts with no Loading Info page (e.g. 360 Plastics) simply have none
    # of this — remark is left blank rather than guessed.
    m = re.search(r"IMPORTANT\s*!+\s*\*+\s*(.*?)\s*\*+\s*!+", t, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()


# HTML <input type="date"> always submits "YYYY-MM-DD" regardless of browser
# locale; the OP column reads "YYYYMMDD 00:00:00" (same convention as Sabic
# Outbound's planned_date / Vinmar Inbound's eta_date).
PLANNED_DATE_INPUT_FORMAT = "%Y-%m-%d"
PLANNED_DATE_OUTPUT_FORMAT = "%Y%m%d 00:00:00"


def format_planned_date(raw: str) -> str:
    return datetime.strptime(raw.strip(), PLANNED_DATE_INPUT_FORMAT).strftime(PLANNED_DATE_OUTPUT_FORMAT)


def _weight_kg(raw: str, unit: str) -> int:
    val = float(raw.strip().replace(",", ""))
    if unit.upper() in ("T", "MT"):
        val *= 1000
    return int(round(val))


class VinmarOutboundTask(BaseTask):
    client_slug = CLIENT_SLUG
    task_slug = TASK_SLUG
    label = "Vinmar — Outbound"

    required_documents = [
        {"key": "release_note", "label": "Delivery / Release Note PDF(s)", "accept": ".pdf", "multiple": True},
    ]

    column_config = [
        {"header": "Reference",    "field_key": "reference",    "width": 24},
        {"header": "Description",  "field_key": "description",  "width": 34},
        {"header": "Planned Date", "field_key": "planned_date", "width": 20},
        {"header": "Public ID",    "field_key": "public_id",    "width": 34},
        {"header": "Remarks",      "field_key": "remark",       "width": 40},
        {"header": "Country Code", "field_key": "country_code", "width": 12},
    ]

    def _extract_rows(self, text: str, planned_date: str) -> list[dict]:
        # One output row per release note document — even when it lists
        # several item lines (e.g. a LTL note combining 2 products), they all
        # belong to the same shipment/reference, so they're joined as
        # separate lines within ONE Description cell rather than exploded
        # into multiple rows.
        reference = "/".join(v for v in (_release_no(text), _delivery_no(text)) if v)
        public_id = _trucker_name(text)
        remark = _remark(text)
        country_code = _country_code(text)

        # Grouped by product name — batch/PO number is deliberately NOT part
        # of the grouping key: the same product split across several batches
        # (e.g. two lots of "PP J340") collapses into one summed-weight line,
        # and two different products that happen to share one batch/PO still
        # get their own separate lines, keyed only by product.
        item_lines = _item_table_lines(text)
        weight_by_product: dict[str, int] = {}
        for m in ITEM_ROW_RE.finditer(item_lines):
            _item_no, product, _batch, qty_raw, unit = m.groups()
            product = product.strip()
            weight_by_product[product] = weight_by_product.get(product, 0) + _weight_kg(qty_raw, unit)

        if not weight_by_product:
            return []

        descriptions = [f"{weight} KGS - {product}" for product, weight in weight_by_product.items()]

        return [{
            "reference": reference,
            "description": "\n".join(descriptions),
            "planned_date": planned_date,
            "public_id": public_id,
            "remark": remark,
            "country_code": country_code,
        }]

    # ── BaseTask entry point ────────────────────────────────────────────────
    def process(self, files: dict, output_path: str | None = None, planned_date: str = "") -> dict:
        paths = files.get("release_note", [])
        if isinstance(paths, str):
            paths = [paths]
        rows = []
        for p in paths:
            rows.extend(self._extract_rows(extract_text(p), planned_date))
        return {"rows": rows, "summary": {"documents_processed": len(paths)}}


# ── Flask Blueprint ──────────────────────────────────────────────────────────
bp = Blueprint(
    "vinmar_outbound", __name__,
    url_prefix=f"/app/{CLIENT_SLUG}/{TASK_SLUG}",
    template_folder="templates",
)

_task = VinmarOutboundTask()


@bp.route("/")
def index():
    # Namespaced under vinmar_outbound/ — Flask's template loader is global
    # across all blueprints, so a bare "index.html" would collide with other
    # clients' same-named templates and silently serve the wrong one.
    return render_template("vinmar_outbound/index.html", label=_task.label, documents=_task.required_documents)


@bp.route("/process", methods=["POST"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def process():
    files = request.files.getlist("release_note")
    if not files:
        return jsonify({"error": "At least one Delivery / Release Note PDF is required."}), 400

    planned_date_raw = (request.form.get("planned_date") or "").strip()
    if not planned_date_raw:
        return jsonify({"error": "Planned Date is required."}), 400
    try:
        planned_date = format_planned_date(planned_date_raw)
    except ValueError:
        return jsonify({"error": "Invalid Planned Date."}), 400

    job_id, job_dir = new_job_dir()
    session = SessionLocal()
    try:
        saved_paths = []
        for f in files:
            if not f.filename.lower().endswith(".pdf"):
                continue
            p = job_dir / secure_filename(f.filename)
            f.save(p)
            saved_paths.append(str(p))

        if not saved_paths:
            return jsonify({"error": "No valid PDF files uploaded."}), 400

        result = _task.process({"release_note": saved_paths}, planned_date=planned_date)
        write_excel(result["rows"], _task.column_config, str(job_output_path(job_id)))

        reference, reference_count = build_reference(r.get("reference") for r in result["rows"])
        source_filename = ", ".join(f.filename for f in files if f.filename.lower().endswith(".pdf"))
        job = log_job(session, g.user["user_id"], CLIENT_SLUG, TASK_SLUG, f"{job_id}/output.xlsx", "success",
                      reference=reference, source_filename=source_filename, row_count=len(result["rows"]),
                      reference_count=reference_count)
        upsert_order_tracking(session, CLIENT_SLUG, TASK_SLUG,
                               (r.get("reference") for r in result["rows"]), job.id)

        return jsonify({
            "success": True,
            "job_id": job_id,
            "summary": result["summary"],
            "rows": result["rows"],
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
    return send_file(path, as_attachment=True, download_name=OUTPUT_FILENAME)


def _order_row_json(r: OrderTracking) -> dict:
    return {
        "id": r.id,
        "reference": r.reference,
        "date": utc_iso(r.created_at),
        "status": r.status,
        "itos_number": r.itos_number,
        "screenshot_status": r.screenshot_status,
        "screenshot_error": r.screenshot_error,
        "email_status": r.email_status,
        "email_error": r.email_error,
    }


@bp.route("/orders")
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def list_orders():
    """Backs the 'Screenshot' tracking panel — one row per distinct
    reference for this client+task, optionally scoped to a calendar period
    (Today/This week/This month), newest first."""
    period = request.args.get("period", "all")
    tz_name = request.args.get("tz")

    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=CLIENT_SLUG).first()
        task = session.query(TaskModel).filter_by(slug=TASK_SLUG).first()
        q = session.query(OrderTracking).filter_by(client_id=client.id, task_id=task.id)

        if period != "all":
            try:
                since, until = period_range(period, tz_name=tz_name)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            q = q.filter(OrderTracking.created_at >= since, OrderTracking.created_at < until)

        rows = q.order_by(OrderTracking.created_at.desc()).all()
        return jsonify({"orders": [_order_row_json(r) for r in rows]})
    finally:
        session.close()


@bp.route("/orders", methods=["PATCH"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def update_orders():
    """Batch save — one request updates the ITOS number for any number of
    rows (a single-row save is just a 1-item list), so the panel's Save
    button always writes in one DB round trip regardless of how many rows
    were in edit mode."""
    data = request.get_json(silent=True) or {}
    updates = data.get("updates")
    if not isinstance(updates, list) or not updates:
        return jsonify({"error": "updates must be a non-empty list"}), 400

    session = SessionLocal()
    try:
        client = session.query(Client).filter_by(slug=CLIENT_SLUG).first()
        task = session.query(TaskModel).filter_by(slug=TASK_SLUG).first()

        ids = [u.get("id") for u in updates if u.get("id") is not None]
        rows_by_id = {
            r.id: r for r in
            session.query(OrderTracking)
            .filter_by(client_id=client.id, task_id=task.id)
            .filter(OrderTracking.id.in_(ids))
            .all()
        }

        updated = []
        skipped = []
        for u in updates:
            row = rows_by_id.get(u.get("id"))
            if not row:
                continue
            if row.screenshot_status in ("queued", "processing"):
                skipped.append({"id": row.id, "reason": "Screenshot request in progress for this row."})
                continue

            # itos_number is purely an optional admin-entered label now — the
            # automation searches/files by `reference` instead (see
            # helpers/screenshot_queue.py), so saving it has no effect on
            # screenshot_status/status.
            itos_number = (u.get("itos_number") or "").strip() or None
            row.itos_number = itos_number
            row.updated_by = g.user["user_id"]
            updated.append(row)

        if not updated and not skipped:
            return jsonify({"error": "No matching orders for this client/task."}), 404

        session.commit()
        return jsonify({
            "orders": [_order_row_json(r) for r in updated],
            "skipped": skipped,
        })
    finally:
        session.close()


@bp.route("/screenshots/request", methods=["POST"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def request_order_screenshots():
    """Queues a screenshot automation run for the given rows (single or
    batch, per the caller's checkbox selection). Returns immediately —
    the actual automation runs later, one row at a time, on the single
    background worker (helpers/screenshot_worker.py); this endpoint only
    ever inserts queue rows."""
    data = request.get_json(silent=True) or {}
    ids = data.get("order_tracking_ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "order_tracking_ids must be a non-empty list"}), 400

    session = SessionLocal()
    try:
        result = request_screenshots(session, CLIENT_SLUG, TASK_SLUG, ids, g.user["user_id"])
        return jsonify(result)
    finally:
        session.close()


@bp.route("/screenshots/retry", methods=["POST"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def retry_order_screenshots():
    """Re-queues rows whose screenshot automation previously failed after
    exhausting its automatic retries — a manual 'Retry' click."""
    data = request.get_json(silent=True) or {}
    ids = data.get("order_tracking_ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "order_tracking_ids must be a non-empty list"}), 400

    session = SessionLocal()
    try:
        result = retry_failed(session, CLIENT_SLUG, TASK_SLUG, ids, g.user["user_id"])
        return jsonify(result)
    finally:
        session.close()


def _order_tracking_row_or_404(session, order_tracking_id):
    client = session.query(Client).filter_by(slug=CLIENT_SLUG).first()
    task = session.query(TaskModel).filter_by(slug=TASK_SLUG).first()
    return session.query(OrderTracking).filter_by(
        id=order_tracking_id, client_id=client.id, task_id=task.id
    ).first()


@bp.route("/screenshots/<int:order_tracking_id>/files")
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def list_order_screenshot_files(order_tracking_id):
    """Backs the Source column's viewer — the filenames for one row's
    captured screenshots (empty list if none exist yet, e.g. still queued
    or never requested)."""
    session = SessionLocal()
    try:
        row = _order_tracking_row_or_404(session, order_tracking_id)
        if not row:
            return jsonify({"error": "Not found"}), 404
        filenames = list_screenshot_files(CLIENT_SLUG, row.reference)
        return jsonify({
            "itos_number": row.itos_number,
            "files": [
                {"filename": f,
                 "url": f"/app/{CLIENT_SLUG}/{TASK_SLUG}/screenshots/{order_tracking_id}/image/{f}"}
                for f in filenames
            ],
        })
    finally:
        session.close()


@bp.route("/screenshots/<int:order_tracking_id>/image/<path:filename>")
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def get_order_screenshot_image(order_tracking_id, filename):
    """Serves one screenshot PNG. filename is re-sanitized and re-resolved
    against this row's own reference folder (never trusted as a raw path)
    so a crafted filename can't escape screenshots/<client>/<reference>/."""
    session = SessionLocal()
    try:
        row = _order_tracking_row_or_404(session, order_tracking_id)
        if not row:
            return jsonify({"error": "Not found"}), 404
        reference = row.reference
    finally:
        session.close()

    directory = (SCREENSHOTS_DIR / CLIENT_SLUG / reference_dirname(reference)).resolve()
    path = (directory / secure_filename(filename)).resolve()
    if directory not in path.parents or not path.is_file():
        return jsonify({"error": "Not found"}), 404
    return send_file(path)


@bp.route("/orders/<int:order_tracking_id>/revoke", methods=["POST"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def revoke_order(order_tracking_id):
    """Manually reverts a Done row back to Pending — e.g. the captured
    screenshots turned out wrong and the shipment needs re-requesting.
    itos_number and any existing screenshot files are left untouched (the
    old files just stop being the row's "current" result); status, the
    screenshot lifecycle, AND the email lifecycle all reset — an email
    approval tied to the previous (now-superseded) screenshots shouldn't
    still show as available/sent once those screenshots are being redone."""
    session = SessionLocal()
    try:
        row = _order_tracking_row_or_404(session, order_tracking_id)
        if not row:
            return jsonify({"error": "Not found"}), 404
        if row.status != "done":
            return jsonify({"error": "Only a Done row can be reverted to Pending."}), 400
        if row.email_status in ("queued", "processing"):
            return jsonify({"error": "Cannot revoke while an email send is in progress."}), 409

        row.status = "pending"
        row.screenshot_status = None
        row.screenshot_error = None
        row.email_status = None
        row.email_error = None
        row.updated_by = g.user["user_id"]
        session.commit()
        return jsonify({"order": _order_row_json(row)})
    finally:
        session.close()


@bp.route("/orders/<int:order_tracking_id>", methods=["DELETE"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def delete_order(order_tracking_id):
    """Removes a row entirely. Blocked while a screenshot OR email job is
    actually in flight for it — deleting out from under a background worker
    mid-run is exactly what causes it to crash trying to save its result
    back to a row that's no longer there (see helpers/screenshot_worker.py,
    helpers/email_worker.py). Any ScreenshotJob/EmailJob history for the row
    is deleted too (FK cleanup); the screenshot PNG files on disk are left
    alone — deleting a tracking row is not the same as deciding the
    captured evidence (or a record of an email having been sent) should be
    destroyed."""
    session = SessionLocal()
    try:
        row = _order_tracking_row_or_404(session, order_tracking_id)
        if not row:
            return jsonify({"error": "Not found"}), 404
        if row.screenshot_status in ("queued", "processing"):
            return jsonify({"error": "Cannot delete while a screenshot request is in progress."}), 409
        if row.email_status in ("queued", "processing"):
            return jsonify({"error": "Cannot delete while an email send is in progress."}), 409

        session.query(ScreenshotJob).filter_by(order_tracking_id=order_tracking_id).delete()
        session.query(EmailJob).filter_by(order_tracking_id=order_tracking_id).delete()
        session.delete(row)
        session.commit()
        return jsonify({"deleted": order_tracking_id})
    finally:
        session.close()


@bp.route("/emails/request", methods=["POST"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def request_order_emails():
    """Queues an Outlook forward-with-screenshots send for the given rows
    (single or batch). Returns immediately — the actual send happens later,
    one row at a time, on the dedicated email worker
    (helpers/email_worker.py); this endpoint only ever inserts queue rows."""
    data = request.get_json(silent=True) or {}
    ids = data.get("order_tracking_ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "order_tracking_ids must be a non-empty list"}), 400

    session = SessionLocal()
    try:
        result = request_emails(session, CLIENT_SLUG, TASK_SLUG, ids, g.user["user_id"])
        return jsonify(result)
    finally:
        session.close()


@bp.route("/emails/retry", methods=["POST"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def retry_order_emails():
    """Re-queues rows whose email send previously failed — a manual Retry
    click. There is no automatic retry for emails (see
    helpers/email_worker.py for why), so this is the only way a failed
    send gets attempted again."""
    data = request.get_json(silent=True) or {}
    ids = data.get("order_tracking_ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "order_tracking_ids must be a non-empty list"}), 400

    session = SessionLocal()
    try:
        result = retry_failed_emails(session, CLIENT_SLUG, TASK_SLUG, ids, g.user["user_id"])
        return jsonify(result)
    finally:
        session.close()
