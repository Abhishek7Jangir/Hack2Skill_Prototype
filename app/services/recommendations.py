"""GET /recommendations/{lgd_code}  +  the recommendation-generation job.

Rules (Section 5.1 / 9.10 of the master doc):
  * The score and evidence are computed by us, numerically. The AI only writes the
    summary + recommended_intervention text.
  * The AI sees at most AI_SAMPLE_SIZE (30) ACTIVE complaint descriptions, most recent first.
  * Always regenerated from scratch, never appended to.
  * Never block the request on the AI: return the stored row, or 202 'computing' and
    generate in the background.

Staleness: every stored row carries a fingerprint of (active complaint ids + rounded
evidence values). If the live fingerprint differs, or the row is older than
RECOMMENDATION_MAX_AGE_HOURS, the stored row is returned with status 'stale' and a
refresh is started in the background.

Works for a VILLAGE (uses its hotspot row) or a DISTRICT (uses the live rollup,
and samples complaints from all its villages plus its unassigned complaints).
"""
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from psycopg2.extras import Json

from .. import ai_client
from ..config import settings
from ..constants import ACTIVE_STATUSES, CATEGORIES
from ..db import dict_cursor, fetch_all, fetch_one, get_conn
from ..errors import ApiError, not_found, unprocessable
from ..utils import iso, num
from .hotspots import district_rollup_rows
from .locations import describe_unit

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="recommend")
_inflight: set[tuple[str, str]] = set()
_failed_until: dict[tuple[str, str], float] = {}
_lock = threading.Lock()
FAILURE_COOLDOWN_SECONDS = 60


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------
def _funding_is_synthetic(conn, state: str, district: str, category: str) -> bool:
    row = fetch_one(conn, """SELECT gi.is_synthetic FROM government_indicators gi
                             JOIN geo_units d ON d.lgd_code = gi.lgd_code AND d.level = 'district'
                             WHERE d.name = %s AND d.state = %s AND gi.category = %s
                               AND gi.indicator_name = 'funding_gap'
                             ORDER BY gi.data_year DESC, gi.id DESC LIMIT 1""", (district, state, category))
    return True if row is None else bool(row["is_synthetic"])


def _scope_sql(unit: dict) -> tuple[str, dict]:
    """WHERE fragment selecting the complaints that belong to this unit."""
    if unit["precision"] == "village":
        return "c.resolved_lgd_code = %(code)s", {"code": unit["lgd_code"]}
    return ("(c.resolved_lgd_code = %(code)s OR c.resolved_lgd_code IN ("
            "SELECT lgd_code FROM geo_units WHERE level = 'village' AND district = %(district)s AND state = %(state)s))",
            {"code": unit["lgd_code"], "district": unit["district"], "state": unit["state"]})


def _active_ids(conn, unit: dict, category: str) -> list[str]:
    where, params = _scope_sql(unit)
    rows = fetch_all(conn, f"""SELECT c.id FROM complaints c
                              WHERE {where} AND c.category = %(category)s AND c.complaint_status IN %(active)s
                              ORDER BY c.id""", {**params, "category": category, "active": ACTIVE_STATUSES})
    return [r["id"] for r in rows]


def sample_descriptions(conn, unit: dict, category: str, limit: int) -> list[str]:
    where, params = _scope_sql(unit)
    rows = fetch_all(conn, f"""SELECT COALESCE(NULLIF(c.description, ''), r.raw_text) AS text
                              FROM complaints c JOIN complaints_raw r ON r.id = c.id
                              WHERE {where} AND c.category = %(category)s AND c.complaint_status IN %(active)s
                              ORDER BY c.created_at DESC, c.id DESC
                              LIMIT %(limit)s""",
                     {**params, "category": category, "active": ACTIVE_STATUSES, "limit": limit})
    return [r["text"] for r in rows if r["text"]]


def build_evidence(conn, unit: dict, category: str) -> dict | None:
    """Current numeric evidence for (unit, category), or None if nothing active."""
    if unit["precision"] == "village":
        h = fetch_one(conn, "SELECT * FROM hotspots WHERE lgd_code = %s AND category = %s",
                      (unit["lgd_code"], category))
        if h is None:
            return None
        n = int(h["complaint_count"])
        s = {"demand": h["demand_score"], "infra": h["infrastructure_gap"], "pop": h["affected_population"],
             "imputed": bool(h.get("population_imputed")), "urgency": h["urgency_score"],
             "funding": h["funding_gap_score"], "priority": h["priority_score"]}
        extra = {}
    else:
        rows = district_rollup_rows(conn, {"state": None, "district": unit["lgd_code"], "category": category,
                                           "limit": 1, "active": ACTIVE_STATUSES})
        if not rows:
            return None
        r = rows[0]
        n = int(r["village_complaint_count"] or 0) + int(r["unassigned_complaint_count"] or 0)
        s = {"demand": r["demand_score"], "infra": r["infrastructure_gap"], "pop": r["affected_population"],
             "imputed": bool(r["population_imputed"]), "urgency": r["urgency_score"],
             "funding": r["funding_gap_score"], "priority": r["priority_score"]}
        extra = {"hotspot_village_count": int(r["hotspot_village_count"] or 0),
                 "unassigned_complaint_count": int(r["unassigned_complaint_count"] or 0),
                 "max_priority_score": num(r["max_priority_score"])}

    funding_synth = _funding_is_synthetic(conn, unit["state"], unit["district"], category)
    factors = [
        {"factor": "citizen_demand", "value": num(s["demand"]), "active_complaints": n,
         "description": "Active complaints, min-max normalised within the category (0-100)"},
        {"factor": "infrastructure_gap", "value": num(s["infra"]),
         "description": "Coverage gap % (JJM for water, PMGSY for roads; district-level in v1)"},
        {"factor": "affected_population", "value": None if s["pop"] is None else int(s["pop"]),
         "imputed": s["imputed"],
         "description": "Census 2011 population" + (" (median of nearby villages; Census value missing)"
                                                     if s["imputed"] else "")},
        {"factor": "urgency", "value": num(s["urgency"]),
         "description": "Average severity x urgency weight, scaled 0-100"},
        {"factor": "funding_gap", "value": num(s["funding"]), "is_synthetic": funding_synth,
         "description": "PMGSY sanctioned-vs-actual cost gap %" if not funding_synth
         else "Synthetic placeholder (50) until NRDWP funding data arrives"},
    ]
    ids = _active_ids(conn, unit, category)
    fp_src = json.dumps({"ids": ids, "values": [f["value"] for f in factors], "p": num(s["priority"], 1)},
                        sort_keys=True, default=str)
    return {
        "level": unit["precision"],
        "priority_score": num(s["priority"]),
        "active_complaint_count": n,
        "factors": factors,
        **extra,
        "fingerprint": hashlib.sha1(fp_src.encode()).hexdigest(),
    }


# --------------------------------------------------------------------------
# Generation (background)
# --------------------------------------------------------------------------
def generate(lgd_code: str, category: str) -> bool:
    """Build evidence, call the AI, upsert. Returns True on success. Holds no DB
    connection while waiting on the AI."""
    with get_conn() as conn:
        unit = describe_unit(conn, lgd_code)
        evidence = build_evidence(conn, unit, category) if unit else None
        if evidence is None:
            return False
        samples = sample_descriptions(conn, unit, category, settings.ai_sample_size)

    context = {"lgd_code": lgd_code, "name": unit["village"] or unit["district"], "level": unit["precision"],
               "district": unit["district"], "state": unit["state"], "category": category,
               "priority_score": evidence["priority_score"]}
    try:
        ai = ai_client.narrate(evidence["factors"], samples, context)
    except ai_client.AIError:
        with _lock:
            _failed_until[(lgd_code, category)] = time.time() + FAILURE_COOLDOWN_SECONDS
        return False

    stored = {**evidence, "sample_size": len(samples), "ai_mock": ai["mock"]}
    with get_conn() as conn:
        dict_cursor(conn).execute(
            """INSERT INTO recommendations (lgd_code, category, recommended_intervention, evidence,
                                            complaint_summary, generated_at)
               VALUES (%s, %s, %s, %s, %s, NOW())
               ON CONFLICT (lgd_code, category) DO UPDATE SET
                   recommended_intervention = EXCLUDED.recommended_intervention,
                   evidence = EXCLUDED.evidence,
                   complaint_summary = EXCLUDED.complaint_summary,
                   generated_at = NOW()""",
            (lgd_code, category, ai["recommended_intervention"], Json(stored), ai["summary"]),
        )
    with _lock:
        _failed_until.pop((lgd_code, category), None)
    return True


def _run(key: tuple[str, str]) -> None:
    try:
        generate(*key)
    except Exception:
        log.exception("Recommendation generation failed for %s", key)
        with _lock:
            _failed_until[key] = time.time() + FAILURE_COOLDOWN_SECONDS
    finally:
        with _lock:
            _inflight.discard(key)


def schedule(lgd_code: str, category: str) -> bool:
    """Start a background generation unless one is already running. Returns True if started."""
    key = (lgd_code, category)
    with _lock:
        if key in _inflight:
            return False
        if _failed_until.get(key, 0) > time.time():
            return False
        _inflight.add(key)
    _executor.submit(_run, key)
    return True


# --------------------------------------------------------------------------
# GET /recommendations/{lgd_code}?category=
# --------------------------------------------------------------------------
def _age_hours(generated_at: datetime) -> float:
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - generated_at).total_seconds() / 3600


def _body(unit: dict, category: str, row: dict, status: str) -> dict:
    ev = row["evidence"] or {}
    body = {
        "lgd_code": unit["lgd_code"],
        "name": unit["village"] or unit["district"],
        "level": unit["precision"],
        "district": unit["district"],
        "state": unit["state"],
        "category": category,
        "status": status,
        "priority_score": ev.get("priority_score"),
        "active_complaint_count": ev.get("active_complaint_count"),
        "recommended_intervention": row["recommended_intervention"],
        "complaint_summary": row["complaint_summary"],
        "evidence": ev.get("factors", []),
        "sample_size": ev.get("sample_size"),
        "is_mock": bool(ev.get("ai_mock")),
        "generated_at": iso(row["generated_at"]),
    }
    for k in ("hotspot_village_count", "unassigned_complaint_count", "max_priority_score"):
        if k in ev:
            body[k] = ev[k]
    return body


def get_recommendation(lgd_code: str, category: str) -> tuple[int, dict]:
    if category not in CATEGORIES:
        raise unprocessable(f"category must be one of {CATEGORIES}", "invalid_category")
    with get_conn() as conn:
        unit = describe_unit(conn, lgd_code)
        if unit is None:
            raise not_found(f"lgd_code {lgd_code!r} not found", "unknown_lgd_code")
        if unit["precision"] not in ("village", "district"):
            raise unprocessable("recommendations are available for villages and districts only", "unsupported_level")
        evidence = build_evidence(conn, unit, category)
        row = fetch_one(conn, "SELECT * FROM recommendations WHERE lgd_code = %s AND category = %s",
                        (lgd_code, category))
        if evidence is None and row is not None:  # everything got resolved/rejected: drop the outdated text
            dict_cursor(conn).execute("DELETE FROM recommendations WHERE lgd_code = %s AND category = %s",
                                      (lgd_code, category))
    # raised only after the with-block has committed the delete above
    if evidence is None:
        raise not_found(f"no active {category} complaints for {lgd_code}", "no_active_complaints")

    if row is not None:
        stored_fp = (row["evidence"] or {}).get("fingerprint")
        fresh = stored_fp == evidence["fingerprint"] and _age_hours(row["generated_at"]) < settings.recommendation_max_age_hours
        if fresh:
            return 200, _body(unit, category, row, "ready")
        schedule(lgd_code, category)
        body = _body(unit, category, row, "stale")
        body["refreshing"] = True
        return 200, body

    started = schedule(lgd_code, category)
    with _lock:
        in_cooldown = _failed_until.get((lgd_code, category), 0) > time.time()
    if in_cooldown and not started:
        raise ApiError(503, "the AI narration service is unavailable; retry in a minute", "ai_unavailable")
    return 202, {"lgd_code": lgd_code, "category": category, "status": "computing",
                 "priority_score": evidence["priority_score"], "evidence": evidence["factors"],
                 "retry_after_seconds": 5}
