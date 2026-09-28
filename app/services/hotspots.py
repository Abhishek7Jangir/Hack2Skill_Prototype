"""GET /hotspots  -- village rows are stored; district/state are live rollups (never stored).

District/state rollups:
  * complaint_count = village-resolved active complaints + district-level ("Not listed") ones
  * unassigned_complaint_count = the district-level ones on their own
  * priority/demand/urgency = averages over village hotspots only (v1 decision);
    null when a district has only unassigned complaints
  * infrastructure_gap / funding_gap = average over village hotspots, else the
    district's own government_indicators value
"""
from ..constants import ACTIVE_STATUSES, CATEGORIES
from ..db import fetch_all
from ..errors import unprocessable
from ..utils import as_int, iso, num

LEVELS = ("village", "district", "state")


def _unit_filter_sql(alias_state: str, alias_district_name: str, alias_district_code: str) -> str:
    return f"""
        AND (%(state)s::text IS NULL OR lower({alias_state}) = lower(%(state)s))
        AND (%(district)s::text IS NULL OR lower({alias_district_name}) = lower(%(district)s)
             OR {alias_district_code} = %(district)s)
    """


def list_hotspots(conn, level: str = "village", state: str | None = None, district: str | None = None,
                  category: str | None = None, limit: int = 200) -> dict:
    if level not in LEVELS:
        raise unprocessable(f"level must be one of {LEVELS}", "invalid_level")
    if category is not None and category not in CATEGORIES:
        raise unprocessable(f"category must be one of {CATEGORIES}", "invalid_category")
    params = {"state": state, "district": district, "category": category, "limit": limit,
              "active": ACTIVE_STATUSES}
    if level == "village":
        items = _village(conn, params)
    elif level == "district":
        items = _district(conn, params)
    else:
        items = _state(conn, params)
    return {"level": level, "count": len(items), "hotspots": items}


def _village(conn, p: dict) -> list[dict]:
    rows = fetch_all(conn, f"""
        SELECT h.*, v.name, v.state, v.district, b.lgd_code AS block_lgd_code, b.name AS block,
               d.lgd_code AS district_lgd_code
        FROM hotspots h
        JOIN geo_units v ON v.lgd_code = h.lgd_code
        LEFT JOIN geo_units b ON b.lgd_code = v.parent_lgd_code
        LEFT JOIN geo_units d ON d.level = 'district' AND d.name = v.district AND d.state = v.state
        WHERE (%(category)s::text IS NULL OR h.category = %(category)s)
        {_unit_filter_sql("v.state", "v.district", "d.lgd_code")}
        ORDER BY h.priority_score DESC NULLS LAST, h.lgd_code
        LIMIT %(limit)s
    """, p)
    return [{
        "id": r["lgd_code"],
        "level": "village",
        "name": r["name"],
        "block": r["block"],
        "block_lgd_code": r["block_lgd_code"],
        "district": r["district"],
        "district_lgd_code": r["district_lgd_code"],
        "state": r["state"],
        "category": r["category"],
        "complaint_count": as_int(r["complaint_count"]),
        "demand_score": num(r["demand_score"]),
        "infrastructure_gap": num(r["infrastructure_gap"]),
        "affected_population": as_int(r["affected_population"]),
        "population_imputed": bool(r.get("population_imputed")),
        "urgency_score": num(r["urgency_score"]),
        "funding_gap_score": num(r["funding_gap_score"]),
        "priority_score": num(r["priority_score"]),
        "computed_at": iso(r["computed_at"]),
    } for r in rows]


# Shared CTEs for the rollups -----------------------------------------------
_ROLLUP_CTES = """
    WITH unassigned AS (          -- active complaints resolved only to a district ("Not listed")
        SELECT d.lgd_code AS district_lgd_code, d.name AS district, d.state, c.category, COUNT(*) AS n
        FROM complaints c
        JOIN geo_units d ON d.lgd_code = c.resolved_lgd_code AND d.level = 'district'
        WHERE c.complaint_status IN %(active)s
          AND (%(category)s::text IS NULL OR c.category = %(category)s)
        GROUP BY d.lgd_code, d.name, d.state, c.category
    ),
    vh AS (                       -- village hotspots with their district
        SELECT h.*, v.state, v.district, d.lgd_code AS district_lgd_code
        FROM hotspots h
        JOIN geo_units v ON v.lgd_code = h.lgd_code AND v.level = 'village'
        JOIN geo_units d ON d.level = 'district' AND d.name = v.district AND d.state = v.state
        WHERE (%(category)s::text IS NULL OR h.category = %(category)s)
    )
"""

_AGG_COLS = """
    COALESCE(SUM(vh.complaint_count), 0)      AS village_complaint_count,
    COUNT(vh.lgd_code)                        AS hotspot_village_count,
    AVG(vh.demand_score)                      AS demand_score,
    AVG(vh.priority_score)                    AS priority_score,
    MAX(vh.priority_score)                    AS max_priority_score,
    SUM(vh.affected_population)               AS affected_population,
    BOOL_OR(vh.population_imputed)            AS population_imputed,
    AVG(vh.urgency_score)                     AS urgency_score,
    AVG(vh.infrastructure_gap)                AS infrastructure_gap,
    AVG(vh.funding_gap_score)                 AS funding_gap_score
"""


def _rollup_item(r: dict, level: str) -> dict:
    village_n = as_int(r["village_complaint_count"]) or 0
    unassigned_n = as_int(r["unassigned_complaint_count"]) or 0
    item = {
        "id": r["lgd_code"],
        "level": level,
        "name": r["name"],
        "state": r["state"],
        "category": r["category"],
        "complaint_count": village_n + unassigned_n,
        "village_complaint_count": village_n,
        "unassigned_complaint_count": unassigned_n,
        "hotspot_village_count": as_int(r["hotspot_village_count"]) or 0,
        "demand_score": num(r["demand_score"]),
        "infrastructure_gap": num(r["infrastructure_gap"]),
        "affected_population": as_int(r["affected_population"]),
        "population_imputed": bool(r["population_imputed"]),
        "urgency_score": num(r["urgency_score"]),
        "funding_gap_score": num(r["funding_gap_score"]),
        "priority_score": num(r["priority_score"]),
        "max_priority_score": num(r["max_priority_score"]),
    }
    if level == "district":
        item["district"] = r["name"]
    return item


def district_rollup_rows(conn, p: dict) -> list[dict]:
    return fetch_all(conn, _ROLLUP_CTES + f"""
        , keys AS (
            SELECT DISTINCT district_lgd_code, category FROM vh
            UNION SELECT district_lgd_code, category FROM unassigned
        ), agg AS (
            SELECT k.district_lgd_code, k.category, {_AGG_COLS}
            FROM keys k
            LEFT JOIN vh ON vh.district_lgd_code = k.district_lgd_code AND vh.category = k.category
            GROUP BY k.district_lgd_code, k.category
        )
        SELECT d.lgd_code, d.name, d.state, a.category,
               a.village_complaint_count, a.hotspot_village_count, a.demand_score, a.priority_score,
               a.max_priority_score, a.affected_population, COALESCE(a.population_imputed, FALSE) AS population_imputed,
               a.urgency_score,
               COALESCE(a.infrastructure_gap, ig.value) AS infrastructure_gap,
               COALESCE(a.funding_gap_score, fg.value)  AS funding_gap_score,
               COALESCE(u.n, 0) AS unassigned_complaint_count
        FROM agg a
        JOIN geo_units d ON d.lgd_code = a.district_lgd_code
        LEFT JOIN unassigned u ON u.district_lgd_code = a.district_lgd_code AND u.category = a.category
        LEFT JOIN LATERAL (SELECT value FROM government_indicators
                           WHERE lgd_code = d.lgd_code AND category = a.category
                             AND indicator_name = 'infrastructure_gap'
                           ORDER BY data_year DESC, id DESC LIMIT 1) ig ON TRUE
        LEFT JOIN LATERAL (SELECT value FROM government_indicators
                           WHERE lgd_code = d.lgd_code AND category = a.category AND indicator_name = 'funding_gap'
                           ORDER BY data_year DESC, id DESC LIMIT 1) fg ON TRUE
        WHERE TRUE {_unit_filter_sql("d.state", "d.name", "d.lgd_code")}
        ORDER BY a.priority_score DESC NULLS LAST, (a.village_complaint_count + COALESCE(u.n, 0)) DESC, d.name
        LIMIT %(limit)s
    """, p)


def _district(conn, p: dict) -> list[dict]:
    return [_rollup_item(r, "district") for r in district_rollup_rows(conn, p)]


def _state(conn, p: dict) -> list[dict]:
    rows = fetch_all(conn, _ROLLUP_CTES + f"""
        , keys AS (
            SELECT DISTINCT state, category FROM vh
            UNION SELECT state, category FROM unassigned
        ), agg AS (
            SELECT k.state, k.category, {_AGG_COLS}
            FROM keys k
            LEFT JOIN vh ON vh.state = k.state AND vh.category = k.category
            GROUP BY k.state, k.category
        ), un AS (SELECT state, category, SUM(n) AS n FROM unassigned GROUP BY state, category)
        SELECT s.lgd_code, s.name, s.name AS state, a.category,
               a.village_complaint_count, a.hotspot_village_count, a.demand_score, a.priority_score,
               a.max_priority_score, a.affected_population, COALESCE(a.population_imputed, FALSE) AS population_imputed,
               a.urgency_score, a.infrastructure_gap, a.funding_gap_score,
               COALESCE(un.n, 0) AS unassigned_complaint_count
        FROM agg a
        JOIN geo_units s ON s.level = 'state' AND s.name = a.state
        LEFT JOIN un ON un.state = a.state AND un.category = a.category
        WHERE (%(state)s::text IS NULL OR lower(s.name) = lower(%(state)s) OR s.lgd_code = %(state)s)
        ORDER BY a.priority_score DESC NULLS LAST, s.name
        LIMIT %(limit)s
    """, p)
    return [_rollup_item(r, "state") for r in rows]
