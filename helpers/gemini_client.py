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

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
MAX_RETRIES = 2

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
    model: str = DEFAULT_MODEL,
    temperature: float = 0.0,
    max_output_tokens: int = 8192,
):
    """
    Sends `prompt` (+ an optional PDF, as a path or raw bytes) to Gemini and
    returns the response parsed as JSON (dict or list). Raises RuntimeError if
    all retries are exhausted or the response isn't valid JSON.
    """
    _configure()
    gm = genai.GenerativeModel(model)

    parts: list = [prompt]
    if pdf_bytes is not None:
        parts.append({"mime_type": "application/pdf", "data": pdf_bytes})
    elif pdf_path is not None:
        with open(pdf_path, "rb") as f:
            parts.append({"mime_type": "application/pdf", "data": f.read()})

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
            text = _strip_code_fence(response.text)
            return json.loads(text)
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)

    raise RuntimeError(f"Gemini call failed after {MAX_RETRIES + 1} attempts: {last_err}")
