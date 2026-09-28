"""POST /complaints, GET /complaints/{id}, PATCH /complaints/{id}/status."""
import logging

from .. import ai_client
from ..config import settings
from ..constants import (ACTIVE_STATUSES, CATEGORIES, COMPLAINT_STATUSES, FALLBACK_SEVERITY,
                         FALLBACK_URGENCY)
from ..db import dict_cursor, fetch_all, fetch_one, get_conn
from ..errors import not_found, unprocessable
from ..schemas import ComplaintIn, StatusUpdateIn
from ..utils import iso, num
from .locations import describe_unit, resolve_location
from .recompute import request_recompute

log = logging.getLogger(__name__)

VOICE_PLACEHOLDER = "[voice complaint - transcription unavailable]"

# 'REQ-000001' ... 'REQ-999999', then 'REQ-1000000' (never truncated; fits VARCHAR(20))
_NEXT_ID_SQL = """SELECT 'REQ-' || CASE WHEN n < 1000000 THEN lpad(n::text, 6, '0') ELSE n::text END AS id
                  FROM (SELECT nextval('complaint_id_seq') AS n) s"""


def _strip_data_url(audio: str) -> str:
    # "data:audio/webm;base64,AAAA" -> "AAAA"
    return audio.split(",", 1)[1] if audio.startswith("data:") and "," in audio else audio


def create_complaint(body: ComplaintIn) -> dict:
    flow_b = body.severity is not None or body.urgency is not None
    text = (body.text or "").strip() or None
    audio = _strip_data_url(body.audio_file.strip()) if body.audio_file and body.audio_file.strip() else None

    # ---- 1. validate the request shape before any DB/AI work --------------
    if flow_b and (body.severity is None or body.urgency is None):
        raise unprocessable("send BOTH severity and urgency (already processed by AI, Flow B) or NEITHER (Flow A)",
                            "partial_ai_fields")
    if flow_b and not text:
        raise unprocessable("text is required when severity/urgency are provided (Flow B)", "text_required")
    if not flow_b:
        if body.input_type == "text" and not text:
            raise unprocessable("text is required for input_type 'text'", "text_required")
        if body.input_type == "voice" and not (audio or text):
            raise unprocessable("audio_file (base64) is required for input_type 'voice'", "audio_required")
    if audio and len(audio) > settings.max_audio_base64_chars:
        raise unprocessable("audio_file is too large", "audio_too_large")

    # ---- 2. resolve location (fail fast, before spending an AI call) -------
    loc_in = body.location
    with get_conn() as conn:
        loc = resolve_location(conn, loc_in.method, pincode=loc_in.pincode, lgd_code=loc_in.lgd_code,
                               district_lgd_code=loc_in.district_lgd_code)

    # ---- 3. severity/urgency: Flow B as given, Flow A via the AI -----------
    if flow_b:
        severity, urgency, source = body.severity, body.urgency, "ai"
    else:
        ai = ai_client.process_complaint(body.input_type, text, audio if body.input_type == "voice" else None,
                                         body.language)
        if ai is not None:
            text, severity, urgency, source = ai["text"], ai["severity"], ai["urgency"], "ai"
        else:
            severity, urgency, source = FALLBACK_SEVERITY, FALLBACK_URGENCY, "fallback"
            text = text or VOICE_PLACEHOLDER  # voice audio is not stored in v1

    # ---- 4. store -----------------------------------------------------------
    with get_conn() as conn:
        cur = dict_cursor(conn)
        cur.execute(_NEXT_ID_SQL)
        cid = cur.fetchone()["id"]
        cur.execute("""INSERT INTO complaints_raw (id, raw_text, language, channel, created_at)
                       VALUES (%s, %s, %s, %s, NOW())""", (cid, text, body.language, body.channel))
        cur.execute(
            """INSERT INTO complaints (id, category, severity, urgency, description, location_method,
                                       input_pincode, input_lat, input_lng, resolved_lgd_code, resolved_confidence,
                                       location_resolution_status, complaint_status, severity_source,
                                       client_timestamp, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', %s, %s, NOW())
               RETURNING created_at""",
            (cid, body.category, severity, urgency, text, loc.method, loc.input_pincode, loc_in.lat, loc_in.lng,
             loc.lgd_code, loc.confidence, loc.status, source, body.timestamp),
        )
        created_at = cur.fetchone()["created_at"]
        location = describe_unit(conn, loc.lgd_code)

    request_recompute()
    return {
        "id": cid,
        "category": body.category,
        "severity": severity,
        "urgency": urgency,
        "severity_source": source,
        "location": location,
        "location_resolution_status": loc.status,
        "status": "received",
        "created_at": iso(created_at),
    }


def get_complaint(complaint_id: str) -> dict:
    with get_conn() as conn:
        row = fetch_one(conn, """SELECT c.*, r.language, r.channel FROM complaints c
                                 JOIN complaints_raw r ON r.id = c.id WHERE c.id = %s""", (complaint_id,))
        if row is None:
            raise not_found(f"complaint {complaint_id!r} not found", "unknown_complaint")
        history = fetch_all(conn, """SELECT old_status, new_status, note, changed_at
                                     FROM complaint_status_history WHERE complaint_id = %s
                                     ORDER BY changed_at, id""", (complaint_id,))
        location = describe_unit(conn, row["resolved_lgd_code"])
    return {
        "id": row["id"],
        "status": row["complaint_status"],
        "category": row["category"],
        "severity": row["severity"],
        "urgency": row["urgency"],
        "description": row["description"],
        "language": row["language"],
        "channel": row["channel"],
        "location": location,
        "location_resolution_status": row["location_resolution_status"],
        "location_confidence": num(row["resolved_confidence"]),
        "created_at": iso(row["created_at"]),
        "status_updated_at": iso(row["status_updated_at"]),
        "history": [{"status": h["new_status"], "previous_status": h["old_status"], "note": h["note"],
                     "changed_at": iso(h["changed_at"])} for h in history],
    }


def list_complaints(conn, status: str | None, category: str | None, district: str | None,
                    lgd_code: str | None, q: str | None, limit: int = 50, offset: int = 0) -> dict:
    """Admin list, newest first. `by_status` counts use every filter except `status`."""
    if status is not None and status not in COMPLAINT_STATUSES:
        raise unprocessable(f"status must be one of {COMPLAINT_STATUSES}", "invalid_status")
    if category is not None and category not in CATEGORIES:
        raise unprocessable(f"category must be one of {CATEGORIES}", "invalid_category")
    params = {"category": category, "district": district, "lgd_code": lgd_code,
              "q": f"%{q.strip()}%" if q and q.strip() else None, "status": status,
              "limit": limit, "offset": offset}
    base = """
        FROM complaints c
        LEFT JOIN geo_units u ON u.lgd_code = c.resolved_lgd_code
        LEFT JOIN geo_units d ON d.level = 'district' AND d.name = u.district AND d.state = u.state
        WHERE (%(category)s::text IS NULL OR c.category = %(category)s)
          AND (%(district)s::text IS NULL OR lower(u.district) = lower(%(district)s) OR d.lgd_code = %(district)s)
          AND (%(lgd_code)s::text IS NULL OR c.resolved_lgd_code = %(lgd_code)s)
          AND (%(q)s::text IS NULL OR c.id ILIKE %(q)s OR c.description ILIKE %(q)s)
    """
    counts = {r["complaint_status"]: int(r["n"]) for r in fetch_all(
        conn, f"SELECT c.complaint_status, COUNT(*) AS n {base} GROUP BY c.complaint_status", params)}
    rows = fetch_all(conn, f"""
        SELECT c.id, c.category, c.severity, c.urgency, c.severity_source, c.description,
               c.complaint_status, c.location_resolution_status, c.resolved_lgd_code,
               u.name AS unit_name, u.level AS unit_level, u.district, d.lgd_code AS district_lgd_code,
               c.created_at, c.status_updated_at, c.status_updated_by
        {base} AND (%(status)s::text IS NULL OR c.complaint_status = %(status)s)
        ORDER BY c.created_at DESC, c.id DESC
        LIMIT %(limit)s OFFSET %(offset)s""", params)
    return {
        "total": counts.get(status, 0) if status else sum(counts.values()),
        "by_status": {s: counts.get(s, 0) for s in COMPLAINT_STATUSES},
        "limit": limit,
        "offset": offset,
        "complaints": [{
            "id": r["id"],
            "status": r["complaint_status"],
            "category": r["category"],
            "severity": r["severity"],
            "urgency": r["urgency"],
            "severity_source": r["severity_source"],
            "description": r["description"],
            "lgd_code": r["resolved_lgd_code"],
            "location_name": r["unit_name"],
            "precision": r["unit_level"],
            "district": r["district"],
            "district_lgd_code": r["district_lgd_code"],
            "location_resolution_status": r["location_resolution_status"],
            "created_at": iso(r["created_at"]),
            "status_updated_at": iso(r["status_updated_at"]),
            "status_updated_by": r["status_updated_by"],
        } for r in rows],
    }


def update_status(complaint_id: str, body: StatusUpdateIn) -> dict:
    with get_conn() as conn:
        cur = dict_cursor(conn)
        cur.execute("SELECT complaint_status FROM complaints WHERE id = %s FOR UPDATE", (complaint_id,))
        row = cur.fetchone()
        if row is None:
            raise not_found(f"complaint {complaint_id!r} not found", "unknown_complaint")
        old = row["complaint_status"]
        cur.execute("""UPDATE complaints SET complaint_status = %s, status_updated_at = NOW(), status_updated_by = %s
                       WHERE id = %s RETURNING status_updated_at""", (body.status, body.updated_by, complaint_id))
        updated_at = cur.fetchone()["status_updated_at"]
        cur.execute("""INSERT INTO complaint_status_history (complaint_id, old_status, new_status, updated_by, note)
                       VALUES (%s, %s, %s, %s, %s)""", (complaint_id, old, body.status, body.updated_by, body.note))

    if (old in ACTIVE_STATUSES) != (body.status in ACTIVE_STATUSES):
        request_recompute()  # only activeness changes the scores
    return {"id": complaint_id, "status": body.status, "previous_status": old, "updated_at": iso(updated_at)}
