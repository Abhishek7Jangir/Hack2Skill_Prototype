"""Hotspot aggregation + priority scoring (the background job).

This is build_hotspots.py turned into a job, with the decisions agreed after it
was validated:
  * active = complaint_status IN ('open','in_progress')   (was: != 'resolved')
  * only VILLAGE-resolved complaints form hotspots; district-level ("Not listed")
    complaints are counted only in the district rollup
  * min-max normalisation is PER CATEGORY and gives 50 when the range is zero
    (was: across all buckets, 100 when equal) -- see normalize()
  * missing/0 Census population -> median of villages (pop > 0) in the same block,
    falling back to the district median; flagged hotspots.population_imputed.
    Imputed values are never written to government_indicators.
  * buckets that no longer have active complaints are DELETED from hotspots
    (the old script only upserted, so a fully-resolved village kept its row forever)
  * set-based queries (a handful per run) instead of 3-4 queries per bucket

Unchanged from the validated script: the weights, urgency weights, the
district-fallback for infrastructure_gap / funding_gap, and the synthetic
placeholder 50 when an indicator is missing (water funding until NRDWP arrives).
"""
import logging
import time
from collections import defaultdict

from ..constants import ACTIVE_STATUSES
from ..db import dict_cursor, fetch_all

log = logging.getLogger(__name__)

WEIGHTS = {
    "demand_score": 0.35,
    "infrastructure_gap": 0.25,
    "affected_population_normalized": 0.20,
    "urgency_score": 0.10,
    "funding_gap_score": 0.10,
}
URGENCY_WEIGHT = {"low": 1.0, "medium": 1.5, "high": 2.0}
MAX_URGENCY_RAW = 5 * 2.0          # severity 5 x weight 2
SYNTHETIC_PLACEHOLDER = 50.0       # used only when no real indicator exists
DEFAULT_SEVERITY = 3               # for legacy rows with NULL severity
DEFAULT_URGENCY = "medium"

_JOB_LOCK_KEY = 4_200_151          # pg advisory lock: one hotspot job at a time across all workers


# --------------------------------------------------------------------------
# Pure functions (unit-tested; revise the scoring policy here only)
# --------------------------------------------------------------------------
def normalize(values: list[float], equal_value: float = 50.0) -> list[float]:
    """Min-max scale to 0-100 within ONE category's buckets.
    If every value is identical (incl. a single bucket) return `equal_value` for all,
    so nothing divides by zero and a lone bucket isn't pinned to 0 or 100."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [equal_value] * len(values)
    return [(v - lo) / (hi - lo) * 100.0 for v in values]


def urgency_score(avg_urgency_raw: float) -> float:
    return min(100.0, avg_urgency_raw / MAX_URGENCY_RAW * 100.0)


def priority(demand: float, infra_gap: float, pop_norm: float, urgency: float, funding_gap: float) -> float:
    return (WEIGHTS["demand_score"] * demand
            + WEIGHTS["infrastructure_gap"] * infra_gap
            + WEIGHTS["affected_population_normalized"] * pop_norm
            + WEIGHTS["urgency_score"] * urgency
            + WEIGHTS["funding_gap_score"] * funding_gap)


# --------------------------------------------------------------------------
# Data loading (set-based)
# --------------------------------------------------------------------------
def _load_buckets(conn) -> list[dict]:
    return fetch_all(
        conn,
        """
        SELECT c.resolved_lgd_code AS lgd_code, c.category,
               COUNT(*) AS complaint_count,
               AVG(COALESCE(c.severity, %(def_sev)s) *
                   CASE COALESCE(c.urgency, %(def_urg)s)
                        WHEN 'low' THEN %(w_low)s WHEN 'medium' THEN %(w_med)s WHEN 'high' THEN %(w_high)s
                        ELSE %(w_low)s END) AS avg_urgency_raw,
               v.parent_lgd_code AS block_lgd_code, v.district, v.state
        FROM complaints c
        JOIN geo_units v ON v.lgd_code = c.resolved_lgd_code AND v.level = 'village'
        WHERE c.complaint_status IN %(active)s
        GROUP BY c.resolved_lgd_code, c.category, v.parent_lgd_code, v.district, v.state
        """,
        {"def_sev": DEFAULT_SEVERITY, "def_urg": DEFAULT_URGENCY, "w_low": URGENCY_WEIGHT["low"],
         "w_med": URGENCY_WEIGHT["medium"], "w_high": URGENCY_WEIGHT["high"], "active": ACTIVE_STATUSES},
    )


def _load_indicators(conn, village_codes: list[str]) -> tuple[dict, dict]:
    """Returns (village_level, district_level) dicts keyed by
    (lgd_code|(state,district), category, indicator_name) -> (value, is_synthetic).
    Latest data_year wins if an indicator was loaded twice."""
    names = ("infrastructure_gap", "funding_gap")
    village = {}
    for r in fetch_all(conn, """SELECT lgd_code, category, indicator_name, value, is_synthetic
                                FROM government_indicators
                                WHERE indicator_name IN %s AND lgd_code = ANY(%s)
                                ORDER BY data_year, created_at, id""", (names, village_codes)):
        village[(r["lgd_code"], r["category"], r["indicator_name"])] = (float(r["value"]), bool(r["is_synthetic"]))
    district = {}
    for r in fetch_all(conn, """SELECT d.state, d.name AS district, gi.category, gi.indicator_name,
                                       gi.value, gi.is_synthetic
                                FROM government_indicators gi
                                JOIN geo_units d ON d.lgd_code = gi.lgd_code AND d.level = 'district'
                                WHERE gi.indicator_name IN %s
                                ORDER BY gi.data_year, gi.created_at, gi.id""", (names,)):
        district[((r["state"], r["district"]), r["category"], r["indicator_name"])] = (
            float(r["value"]), bool(r["is_synthetic"]))
    return village, district


def get_indicator(village_ind: dict, district_ind: dict, lgd_code: str, state: str, district: str,
                  category: str, name: str) -> tuple[float, bool, str]:
    """Village row first (none exist in v1), then the district row, then the placeholder.
    Returns (value, is_synthetic, level_used)."""
    hit = village_ind.get((lgd_code, category, name))
    if hit:
        return hit[0], hit[1], "village"
    hit = district_ind.get(((state, district), category, name))
    if hit:
        return hit[0], hit[1], "district"
    return SYNTHETIC_PLACEHOLDER, True, "placeholder"


def _load_population(conn, buckets: list[dict]) -> dict[str, tuple[int, bool]]:
    """lgd_code -> (population, imputed?)"""
    codes = sorted({b["lgd_code"] for b in buckets})
    if not codes:
        return {}
    own = {r["lgd_code"]: float(r["value"]) for r in fetch_all(
        conn, """SELECT lgd_code, MAX(value) AS value FROM government_indicators
                 WHERE category = 'population' AND indicator_name = 'total_population'
                   AND lgd_code = ANY(%s) GROUP BY lgd_code""", (codes,)) if r["value"] is not None}

    missing = [b for b in buckets if own.get(b["lgd_code"], 0) <= 0]
    block_med, district_med = {}, {}
    if missing:
        blocks = sorted({b["block_lgd_code"] for b in missing if b["block_lgd_code"]})
        block_med = {r["k"]: float(r["med"]) for r in fetch_all(
            conn, """SELECT v.parent_lgd_code AS k,
                            percentile_cont(0.5) WITHIN GROUP (ORDER BY gi.value) AS med
                     FROM geo_units v
                     JOIN government_indicators gi ON gi.lgd_code = v.lgd_code
                          AND gi.category = 'population' AND gi.indicator_name = 'total_population' AND gi.value > 0
                     WHERE v.level = 'village' AND v.parent_lgd_code = ANY(%s)
                     GROUP BY v.parent_lgd_code""", (blocks,))}
        dists = sorted({b["district"] for b in missing if b["district"]})
        district_med = {r["k"]: float(r["med"]) for r in fetch_all(
            conn, """SELECT v.district AS k,
                            percentile_cont(0.5) WITHIN GROUP (ORDER BY gi.value) AS med
                     FROM geo_units v
                     JOIN government_indicators gi ON gi.lgd_code = v.lgd_code
                          AND gi.category = 'population' AND gi.indicator_name = 'total_population' AND gi.value > 0
                     WHERE v.level = 'village' AND v.district = ANY(%s)
                     GROUP BY v.district""", (dists,))}

    out = {}
    for b in buckets:
        code = b["lgd_code"]
        pop = own.get(code, 0)
        if pop > 0:
            out[code] = (int(round(pop)), False)
        else:
            med = block_med.get(b["block_lgd_code"]) or district_med.get(b["district"]) or 0
            out[code] = (int(round(med)), True)
    return out


# --------------------------------------------------------------------------
# The job
# --------------------------------------------------------------------------
def compute_hotspots(conn) -> list[dict]:
    """Read-only: returns the hotspot rows that should exist right now."""
    buckets = _load_buckets(conn)
    if not buckets:
        return []
    village_ind, district_ind = _load_indicators(conn, sorted({b["lgd_code"] for b in buckets}))
    population = _load_population(conn, buckets)

    rows = []
    for b in buckets:
        infra, _, _ = get_indicator(village_ind, district_ind, b["lgd_code"], b["state"], b["district"],
                                    b["category"], "infrastructure_gap")
        funding, funding_synth, _ = get_indicator(village_ind, district_ind, b["lgd_code"], b["state"],
                                                  b["district"], b["category"], "funding_gap")
        pop, imputed = population[b["lgd_code"]]
        rows.append({
            "lgd_code": b["lgd_code"],
            "category": b["category"],
            "complaint_count": int(b["complaint_count"]),
            "infrastructure_gap": infra,
            "funding_gap_score": funding,
            "funding_is_synthetic": funding_synth,
            "affected_population": pop,
            "population_imputed": imputed,
            "urgency_score": urgency_score(float(b["avg_urgency_raw"])),
        })

    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
    for cat_rows in by_cat.values():
        demand = normalize([r["complaint_count"] for r in cat_rows])
        pop_norm = normalize([r["affected_population"] for r in cat_rows])
        for r, d, p in zip(cat_rows, demand, pop_norm):
            r["demand_score"] = d
            r["population_normalized"] = p
            r["priority_score"] = priority(d, r["infrastructure_gap"], p, r["urgency_score"], r["funding_gap_score"])
    return rows


def run_hotspot_job(conn) -> dict:
    """Recompute and replace the hotspots table in one transaction.
    Caller commits (db.get_conn does). Serialised across processes by an advisory lock."""
    t0 = time.perf_counter()
    cur = dict_cursor(conn)
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_JOB_LOCK_KEY,))
    rows = compute_hotspots(conn)

    keys_l = [r["lgd_code"] for r in rows]
    keys_c = [r["category"] for r in rows]
    cur.execute(
        """DELETE FROM hotspots h
           WHERE NOT EXISTS (SELECT 1 FROM unnest(%s::text[], %s::text[]) AS k(lgd_code, category)
                             WHERE k.lgd_code = h.lgd_code AND k.category = h.category)""",
        (keys_l, keys_c),
    )
    deleted = cur.rowcount
    if rows:
        cur.execute(
            """
            INSERT INTO hotspots (lgd_code, category, demand_score, infrastructure_gap, affected_population,
                                  urgency_score, funding_gap_score, priority_score, complaint_count,
                                  population_imputed, computed_at)
            SELECT * , NOW() FROM unnest(%s::text[], %s::text[], %s::numeric[], %s::numeric[], %s::int[],
                                         %s::numeric[], %s::numeric[], %s::numeric[], %s::int[], %s::boolean[])
            ON CONFLICT (lgd_code, category) DO UPDATE SET
                demand_score = EXCLUDED.demand_score,
                infrastructure_gap = EXCLUDED.infrastructure_gap,
                affected_population = EXCLUDED.affected_population,
                urgency_score = EXCLUDED.urgency_score,
                funding_gap_score = EXCLUDED.funding_gap_score,
                priority_score = EXCLUDED.priority_score,
                complaint_count = EXCLUDED.complaint_count,
                population_imputed = EXCLUDED.population_imputed,
                computed_at = NOW()
            """,
            (keys_l, keys_c,
             [r["demand_score"] for r in rows], [r["infrastructure_gap"] for r in rows],
             [r["affected_population"] for r in rows], [r["urgency_score"] for r in rows],
             [r["funding_gap_score"] for r in rows], [r["priority_score"] for r in rows],
             [r["complaint_count"] for r in rows], [r["population_imputed"] for r in rows]),
        )
    ms = round((time.perf_counter() - t0) * 1000)
    summary = {
        "hotspots": len(rows),
        "removed": deleted,
        "by_category": {c: sum(1 for r in rows if r["category"] == c) for c in sorted(set(keys_c))},
        "population_imputed": sum(1 for r in rows if r["population_imputed"]),
        "duration_ms": ms,
    }
    log.info("Hotspot job: %s", summary)
    return summary
