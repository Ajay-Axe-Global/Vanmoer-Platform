"""
Generic Gemini call wrapper — the ONLY place in the codebase that talks to the
Gemini API. It knows nothing about any client's documents or prompts; those
live in each client task's own prompts.py. This replaces Carpenter's original
raw `requests` + manual base64 REST calls with the official google-generativeai
SDK, same retry/temperature/JSON-parsing behavior.
"""

import json
import os
import re
import time

import google.generativeai as genai
from flask import g, has_app_context

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
MAX_RETRIES = 2


def _record_usage(model: str, usage_metadata, call_label: str | None) -> None:
    """Appends this call's token counts to g.gemini_usage — a plain list
    living on Flask's per-request `g`, so every Gemini call made anywhere
    during one /process request accumulates here with zero changes needed
    at any of the ~40+ existing call_gemini() call sites across every
    client. helpers/jobs.log_job() reads this list once, at the end of the
    request, to write GeminiUsageLog rows (see helpers/billing.py).

    Guarded by has_app_context() so call_gemini() still works if ever
    invoked outside a Flask request (a script, a test) — usage just isn't
    tracked in that case, which is the correct behavior (there's no job to
    attach it to anyway).
    """
    if not has_app_context() or usage_metadata is None:
        return
    if not hasattr(g, "gemini_usage"):
        g.gemini_usage = []
    g.gemini_usage.append({
        "model": model,
        "call_label": call_label,
        "prompt_tokens": getattr(usage_metadata, "prompt_token_count", 0) or 0,
        "completion_tokens": getattr(usage_metadata, "candidates_token_count", 0) or 0,
        "total_tokens": getattr(usage_metadata, "total_token_count", 0) or 0,
    })

_configured = False


def _configure():
    global _configured
    if not _configured:
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set")
        genai.configure(api_key=GEMINI_API_KEY)
        _configured = True


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text


def call_gemini(
    prompt: str,
    pdf_path: str | None = None,
    pdf_bytes: bytes | None = None,
    mime_type: str = "application/pdf",
    model: str = DEFAULT_MODEL,
    temperature: float = 0.0,
    max_output_tokens: int = 8192,
    call_label: str | None = None,
):
    """
    Sends `prompt` (+ an optional file, as a path or raw bytes) to Gemini and
    returns the response parsed as JSON (dict or list). Raises RuntimeError if
    all retries are exhausted or the response isn't valid JSON.

    `pdf_path`/`pdf_bytes` accept any file Gemini supports (PDF, PNG, JPEG,
    ...) despite the name — pass `mime_type` to match (e.g. "image/png").
    Defaults to "application/pdf" so existing PDF-only callers are unaffected.

    `call_label` is optional and purely diagnostic (e.g. "carrier_id", "mbl",
    "packing_list") — recorded alongside this call's token usage (see
    _record_usage()) so a later billing breakdown can show cost per
    extraction step, not just per job. Every existing caller that doesn't
    pass it still works exactly as before; usage is still tracked, just
    without a step label.
    """
    _configure()
    gm = genai.GenerativeModel(model)

    parts: list = [prompt]
    if pdf_bytes is not None:
        parts.append({"mime_type": mime_type, "data": pdf_bytes})
    elif pdf_path is not None:
        with open(pdf_path, "rb") as f:
            parts.append({"mime_type": mime_type, "data": f.read()})

    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = gm.generate_content(
                parts,
                generation_config={
                    "temperature": temperature,
                    "max_output_tokens": max_output_tokens,
                },
            )
            _record_usage(model, getattr(response, "usage_metadata", None), call_label)
            text = _strip_code_fence(response.text)
            return json.loads(text)
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)

    raise RuntimeError(f"Gemini call failed after {MAX_RETRIES + 1} attempts: {last_err}")
