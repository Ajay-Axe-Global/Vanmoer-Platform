"""
Client: SABIC
Task: Outbound
Document: Dispatch Advice PDF (one or more per run)

Ported as-is from Vanmoer/template_generator/clients/sabic_outbound.py — pure
regex extraction, no Gemini involved, this client never needed AI.
"""

import re
from pathlib import Path

from flask import Blueprint, g, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from database.db import SessionLocal
from helpers.base_task import BaseTask
from helpers.decorators import task_access_required
from helpers.excel_writer import write_excel
from helpers.jobs import job_output_path, log_job, new_job_dir
from helpers.pdf_utils import extract_text

CLIENT_SLUG = "sabic"
TASK_SLUG = "outbound"

EU_CODES = {
    "AT", "BE", "BG", "CH", "CY", "CZ", "DE", "DK", "EE", "ES",
    "FI", "FR", "GR", "HR", "HU", "IE", "IT", "LT", "LU",
    "LV", "MT", "NL", "NO", "PL", "PT", "RO", "SE", "SI", "SK", "NI",
}
# GB handled separately via Mallusk check — not in EU_CODES

BIGBAG_PRODUCTS = {s.upper() for s in {
    "CYCOLAC MG94F NA1001",
    "FORTIFY B0563T BB",
    "LDPE HP0330NN",
    "FORTIFY C1085",
    "PO compound P1600A 00900",
    "HDPE M80064E BB",
    "PP 510P BB",
    "FORTIFY C1055T",
    "PMMA 17OP A",
    "PMMA 20HR A",
    "FORTIFY C5070T",
    "FORTIFY C1055D",
    "PC0703R GC9AT",
    "PMMA P15OE",
    "COHERE 8600X",
}}

IO_MAP = {
    ("loading bulk/vrac from bigbag", "PO compound P1600A 00900"): "MET DE HAND LADEN",
    ("loading bulk/vrac from bigbag", "PP Compound G3135X 10000"): "MET DE HAND LADEN",
    ("loading bulk/vrac from bigbag", "PP Compound G3220A 00900"): "MET DE HAND LADEN",
    ("loading bulk/vrac from bigbag", "PP Compound G3230A 10000"): "MET DE HAND LADEN",
    ("loading bulk/vrac from bigbag", "PP Compound G3230AE 10000"): "MET DE HAND LADEN",
    ("loading bulk/vrac from bigbag", "PP Compound G3240A 10000"): "MET DE HAND LADEN",
    ("loading bulk/vrac from bigbag", "LLDPE MG200024"): "MET DE HAND LADEN",
    ("loading bulk/vrac from bigbag", "LLDPE MG500026"): "MET DE HAND LADEN",
    ("loading bulk/vrac from bag", "PO compound P1600A 00900"): "MET DE HAND LADEN",
    ("loading bulk/vrac from bag", "PP Compound G3135X 10000"): "MET DE HAND LADEN",
    ("loading bulk/vrac from bag", "PP Compound G3220A 00900"): "MET DE HAND LADEN",
    ("loading bulk/vrac from bag", "PP Compound G3230A 10000"): "MET DE HAND LADEN",
    ("loading bulk/vrac from bag", "PP Compound G3230AE 10000"): "MET DE HAND LADEN",
    ("loading bulk/vrac from bag", "PP Compound G3240A 10000"): "MET DE HAND LADEN",
    ("loading bulk/vrac from bag", "LLDPE MG200024"): "MET DE HAND LADEN",
    ("loading bulk/vrac from bag", "LLDPE MG500026"): "MET DE HAND LADEN",
    ("loading into truck", "PMMA 17OP A"): "NIET STAPELEN",
    ("loading into truck", "PMMA 20HR A"): "NIET STAPELEN",
    ("loading into truck", "FORTIFY™ C5070T"): "NIET STAPELEN",
    ("loading into truck", "FORTIFY™ C1085"): "NIET STAPELEN",
    ("loading into truck", "LDPE HP0330NN"): "NIET STAPELEN",
    ("loading into truck", "FORTIFY™ C0570D"): "NIET STAPELEN",
    ("loading into truck", "PO compound P1600A 00900"): "NIET STAPELEN",
    ("loading into truck", "PC0703R GC9AT"): "NIET STAPELEN",
    ("loading into truck", "COHERE™ 8600X"): "NIET STAPELEN",
    ("loading into truck", "PMMA P20MH"): "NIET STAPELEN",
    ("loading into truck", "FORTIFY™ C1055T"): "NIET STAPELEN",
    ("loading into truck", "EPDM 657"): "NIET STAPELEN",
    ("loading into truck", "CYCOLAC MG94F NA1001"): "REPACK PRODUCT",
}

SILO_SUFFIX = "BULK-REINIGINGSCERTIFICAAT-WEGEN / EX CONT.:   EX LOC"

FIXED_CLIENT = "SABIC PETROCHEMICAL BV (K0013323)"
FIXED_COST_CENTER = "O-VMROKAAI1793"


class SabicOutboundTask(BaseTask):
    client_slug = CLIENT_SLUG
    task_slug = TASK_SLUG
    label = "SABIC — Outbound"

    required_documents = [
        {"key": "dispatch_advice", "label": "Dispatch Advice PDF(s)", "accept": ".pdf", "multiple": True},
    ]

    column_config = [
        {"header": "Client", "field_key": "client", "width": 35},
        {"header": "External ID", "field_key": "external_id", "width": 16},
        {"header": "Cost center", "field_key": "cost_center", "width": 18},
        {"header": "Product code", "field_key": "product_code", "width": 25},
        {"header": "Net weight", "field_key": "net_weight", "width": 14},
        {"header": "Description", "field_key": "description", "width": 22},
        {"header": "Operation type", "field_key": "operation_type", "width": 30},
        {"header": "IO Description", "field_key": "io_description", "width": 22},
        {"header": "Ref - PO Number", "field_key": "ref_po_number", "width": 18},
        {"header": "Ref - Delivery no", "field_key": "ref_delivery_no", "width": 18},
        {"header": "Ref - Customer Material", "field_key": "ref_cust_material", "width": 24},
        {"header": "Public ID", "field_key": "public_id", "width": 24},
        {"header": "Destination Country code", "field_key": "dest_country_code", "width": 24},
        {"header": "Transport Type Name", "field_key": "transport_type", "width": 18},
        {"header": "Planned Date", "field_key": "planned_date", "width": 20},
        {"header": "Remarks", "field_key": "remarks", "width": 18},
    ]

    # ── Parsers ───────────────────────────────────────────────────────────────
    def _external_id(self, t):
        m = re.search(r"dispatch shipment[:\s]+(\d+)/", t, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    def _carrier_name(self, t):
        m = re.search(r"Carrier:\s*T\d+\s*\n(.+)", t, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    def _delivery_no(self, t):
        m = re.search(r"Delivery:\s*\n(\d+)", t, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    def _customer_po(self, t):
        m = re.search(r"Customer PO:\s*\n([\s\S]{1,200}?)(?=\n\w[\w ]*:|\Z)", t, re.IGNORECASE)
        if not m:
            return ""
        val = " ".join(line.strip() for line in m.group(1).splitlines() if line.strip())
        return val if val and not re.match(r"(Delivery|Transfer|STO|Ship)", val, re.IGNORECASE) else ""

    def _customer_material(self, t):
        m = re.search(r"Customer Material:\s*\n(.+)", t, re.IGNORECASE)
        if not m:
            return ""
        val = m.group(1).strip()
        return "" if re.match(r"(Transfer|STO|Ship|Payer|Sales|Importer)[\s:]", val, re.IGNORECASE) else val

    def _transport_zone(self, t):
        m = re.search(r"Transportation zone:\s*\n([A-Z]{2})", t, re.IGNORECASE)
        return m.group(1).upper() if m else ""

    _ITEM_PATTERN = re.compile(
        r"(\d{6})\s*\n(\d{7,9})\s*\n(.+?)\s*\n(.+?)\s*\n(.+?)\s*\n\s*([\d.,]+)\s*KG",
        re.DOTALL | re.IGNORECASE,
    )

    def _item_blocks(self, t):
        # The "Country of origin / Commodity / Gross price / Per" table header
        # only appears once per document even when it lists multiple items
        # (000010, 000020, ...), so anchor on it once and then scan for every
        # item row after it instead of re.search-ing for a single match.
        header = re.search(r"Country of origin\s+Commodity\s+Gross price\s+Per\s*\n", t, re.IGNORECASE)
        if not header:
            return []
        body = t[header.end():]
        return [
            {
                "product_description": m.group(4).strip(),
                "packaging": m.group(5).strip(),
                "net_weight_raw": m.group(6).strip(),
            }
            for m in self._ITEM_PATTERN.finditer(body)
        ]

    def _decimal_separator(self, t: str) -> str:
        """
        Net/Gross weight values show up in two different number formats
        depending on the customer/locale that generated the PDF: US-style
        (5,500 = thousands comma) or European-style (10.500 = thousands dot).
        The Gross price field on the same document can NOT be trusted as a
        stand-in for this — it's formatted per the sales org's currency
        locale independently of how the weight columns are formatted, and
        the two disagree on some documents (e.g. "2.012,73 EUR" alongside
        "5,500 KG" on the same page). So the anchor must come from a weight
        value itself: one that shows both separators together — e.g. a gross
        weight like "13.756,500 KG" — where the rightmost separator is always
        the decimal point. Falls back to "." (comma = thousands separator)
        when no such anchor exists among the weight values, since a bare
        single-separator weight (e.g. "5,500 KG") is always a whole-kilogram
        thousands grouping in practice — dispatch weights are never reported
        to fractional-kilogram precision.
        """
        m = re.search(r"\d+[.,]\d{3}[.,]\d{1,3}\s*KG", t, re.IGNORECASE)
        if not m:
            return "."
        tok = m.group(0)
        return "," if tok.rfind(",") > tok.rfind(".") else "."

    def _net_weight(self, raw, decimal_sep="."):
        raw = raw.strip().replace(" ", "")
        thousands_sep = "," if decimal_sep == "." else "."
        raw = raw.replace(thousands_sep, "")
        if decimal_sep != ".":
            raw = raw.replace(decimal_sep, ".")
        val = float(raw)
        return f"{int(val):,} KG" if val == int(val) else f"{val:,.1f} KG"

    def _loading_date(self, t):
        m = re.search(r'loading\s+date[:\s]+(\d{2})\.(\d{2})\.(\d{4})', t, re.IGNORECASE)
        if m:
            return f"{m.group(3)}{m.group(2)}{m.group(1)} 00:00:00"
        return ""

    def _is_mallusk(self, t):
        return bool(re.search(r'(?i)mallusk', t))

    def _norm_product(self, s):
        return re.sub(r'[®™�]', '', s).strip()

    def _strip_suffix(self, desc):
        return re.sub(r'\s+\d{3}$', '', desc.strip())

    def _operation_type(self, packaging, transport_zone, product_desc=""):
        is_eu = transport_zone in EU_CODES
        is_silo = "SILO" in packaging.upper()

        if not is_silo:
            return "Loading into truck" if is_eu else "Loading Truck export"

        key = self._norm_product(self._strip_suffix(product_desc)).upper()
        is_bb = key in BIGBAG_PRODUCTS
        if is_bb:
            base = "Loading Bulk/Vrac from Bigbag"
            return base if is_eu else "Loading Bulk/Vrac from Bigbag Export"
        else:
            base = "Loading Bulk/Vrac from Bag"
            return base if is_eu else "Loading Bulk/Vrac from Bag Export"

    def _io_description(self, operation_type, product_description):
        op_key = operation_type.lower().replace(" export", "").strip()
        desc_key = self._norm_product(self._strip_suffix(product_description)).upper()
        is_silo = "bulk" in op_key

        extra_remark = ""
        for (op, desc), val in IO_MAP.items():
            if op == op_key and desc.upper() == desc_key:
                extra_remark = val
                break
        if not extra_remark:
            for (op, desc), val in IO_MAP.items():
                if desc.upper() == desc_key:
                    extra_remark = val
                    break

        if is_silo:
            return f"{extra_remark} - {SILO_SUFFIX}" if extra_remark else SILO_SUFFIX
        return extra_remark

    def _extract_rows(self, text: str) -> list[dict]:
        # Document-level fields apply once per dispatch advice, regardless of
        # how many line items (000010, 000020, ...) it lists.
        transport_zone = self._transport_zone(text)
        if transport_zone == "GB":
            transport_zone = "NI" if self._is_mallusk(text) else "GB"

        decimal_sep = self._decimal_separator(text)
        external_id = self._external_id(text)
        ref_po_number = self._customer_po(text)
        ref_delivery_no = self._delivery_no(text)
        ref_cust_material = self._customer_material(text)
        public_id = self._carrier_name(text) or "FCA"
        planned_date = self._loading_date(text)

        rows = []
        for item in self._item_blocks(text):
            packaging = item.get("packaging", "")
            product_desc = item.get("product_description", "")
            net_weight = self._net_weight(item.get("net_weight_raw", "0"), decimal_sep)

            operation_type = self._operation_type(packaging, transport_zone, product_desc)
            io_description = self._io_description(operation_type, product_desc)

            rows.append({
                "client": FIXED_CLIENT,
                "external_id": external_id,
                "cost_center": FIXED_COST_CENTER,
                "product_code": product_desc,
                "net_weight": net_weight,
                "description": f"{net_weight} - {product_desc}",
                "operation_type": operation_type,
                "io_description": io_description,
                "ref_po_number": ref_po_number,
                "ref_delivery_no": ref_delivery_no,
                "ref_cust_material": ref_cust_material,
                "public_id": public_id,
                "dest_country_code": transport_zone,
                "transport_type": "Silo-Truck" if "SILO" in packaging.upper() else "Truck",
                "planned_date": planned_date,
                "remarks": "EXPORT DOCUMENT" if "export" in operation_type.lower() else "",
            })
        return rows

    # ── BaseTask entry point ────────────────────────────────────────────────
    def process(self, files: dict, output_path: str | None = None) -> dict:
        paths = files.get("dispatch_advice", [])
        if isinstance(paths, str):
            paths = [paths]
        rows = []
        for p in paths:
            rows.extend(self._extract_rows(extract_text(p)))
        return {"rows": rows, "summary": {"documents_processed": len(paths)}}


# ── Flask Blueprint ──────────────────────────────────────────────────────────
bp = Blueprint(
    "sabic_outbound", __name__,
    url_prefix=f"/app/{CLIENT_SLUG}/{TASK_SLUG}",
    template_folder="templates",
)

_task = SabicOutboundTask()


@bp.route("/")
def index():
    # Namespaced under sabic_outbound/ — Flask's template loader is global across
    # all blueprints, so a bare "index.html" would collide with other clients'
    # same-named templates and silently serve the wrong one.
    return render_template("sabic_outbound/index.html", label=_task.label, documents=_task.required_documents)


@bp.route("/process", methods=["POST"])
@task_access_required(CLIENT_SLUG, TASK_SLUG)
def process():
    files = request.files.getlist("dispatch_advice")
    if not files:
        return jsonify({"error": "At least one Dispatch Advice PDF is required."}), 400

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

        result = _task.process({"dispatch_advice": saved_paths})
        write_excel(result["rows"], _task.column_config, str(job_output_path(job_id)))

        log_job(session, g.user["user_id"], CLIENT_SLUG, TASK_SLUG, f"{job_id}/output.xlsx", "success")
        return jsonify({
            "success": True,
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
    return send_file(path, as_attachment=True, download_name="Sabic_Outbound_Output.xlsx")
