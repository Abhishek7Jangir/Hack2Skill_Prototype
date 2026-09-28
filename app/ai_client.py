"""The ONLY place the backend talks to Person 1's AI (n8n webhooks, or our own mock).

Two calls:
  process_complaint(...)  Flow A: raw text/audio -> {text, severity, urgency}
  narrate(...)            evidence + sample descriptions -> {summary, recommended_intervention}

Switching from the built-in mock to the real n8n webhooks is an env-var change:
  AI_PROCESS_URL, AI_NARRATE_URL, optional AI_AUTH_HEADER ("Header-Name: value").
Every call is logged to api_call_log ('success' | 'fallback' | 'error').
"""
import logging

import httpx

from . import db
from .config import settings
from .constants import URGENCIES

log = logging.getLogger(__name__)

API_PROCESS = "ai_process_complaint"
API_NARRATE = "ai_narrate"


class AIError(Exception):
    pass


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    raw = settings.ai_auth_header
    if raw and ":" in raw:
        name, value = raw.split(":", 1)
        headers[name.strip()] = value.strip()
    return headers


def _post(url: str, payload: dict) -> dict:
    try:
        resp = httpx.post(url, json=payload, headers=_headers(), timeout=settings.ai_timeout_seconds)
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException as exc:
        raise AIError(f"timeout after {settings.ai_timeout_seconds}s") from exc
    except httpx.HTTPStatusError as exc:
        raise AIError(f"HTTP {exc.response.status_code}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise AIError(f"{type(exc).__name__}: {exc}") from exc
    # n8n "Respond to Webhook" sometimes wraps the object in a one-element list
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        raise AIError("response is not a JSON object")
    return data


def log_call(api_name: str, status: str) -> None:
    """Write to api_call_log on its own connection so the log survives any other rollback."""
    try:
        with db.get_conn() as conn:
            conn.cursor().execute(
                "INSERT INTO api_call_log (api_name, status) VALUES (%s, %s)", (api_name, status)
            )
    except Exception:  # logging must never break a request
        log.exception("Could not write api_call_log row")


def _parse_severity(value) -> int:
    sev = int(float(value))
    if not 1 <= sev <= 5:
        raise ValueError(f"severity {sev} out of range 1-5")
    return sev


def _parse_urgency(value) -> str:
    urg = str(value).strip().lower()
    if urg not in URGENCIES:
        raise ValueError(f"urgency {value!r} not in {URGENCIES}")
    return urg


def process_complaint(input_type: str, text: str | None, audio_file: str | None, language: str | None) -> dict | None:
    """Flow A. Returns {'text', 'severity', 'urgency', 'mock'} or None if the AI failed
    (caller then applies the fallback defaults). Never raises."""
    payload = {"input_type": input_type, "text": text, "audio_file": audio_file, "language": language}
    try:
        data = _post(settings.ai_process_url, payload)
        out_text = data.get("text")
        if out_text is None or not str(out_text).strip():
            out_text = text  # AI may omit text for text input; keep the original
        if out_text is None or not str(out_text).strip():
            raise ValueError("AI returned no text/transcript")
        result = {
            "text": str(out_text).strip(),
            "severity": _parse_severity(data.get("severity")),
            "urgency": _parse_urgency(data.get("urgency")),
            "mock": bool(data.get("mock", False)),
        }
    except Exception as exc:
        log.warning("AI process-complaint failed, using fallback: %s", exc)
        log_call(API_PROCESS, "fallback")
        return None
    log_call(API_PROCESS, "success")
    return result


def narrate(evidence: list[dict], sample_descriptions: list[str], context: dict) -> dict:
    """Combined summary + recommendation. Raises AIError on failure (logged as 'error')."""
    payload = {"evidence": evidence, "sample_descriptions": sample_descriptions, "context": context}
    try:
        data = _post(settings.ai_narrate_url, payload)
        summary = data.get("summary")
        intervention = data.get("recommended_intervention")
        if not summary or not intervention:
            raise AIError("response missing 'summary' or 'recommended_intervention'")
        result = {
            "summary": str(summary).strip(),
            "recommended_intervention": str(intervention).strip(),
            "mock": bool(data.get("mock", False)),
        }
    except Exception as exc:
        log.warning("AI narrate failed: %s", exc)
        log_call(API_NARRATE, "error")
        raise exc if isinstance(exc, AIError) else AIError(str(exc)) from exc
    log_call(API_NARRATE, "success")
    return result
