"""Location lookup + resolution.

Facts from the data audit that shape this module:
  * pincode -> village is ONE-TO-MANY (974 pincodes, median 28 villages, max 616),
    and 159 pincodes span 2 districts. The citizen picks the village.
  * Invalid pincode rows (village_code_valid = false) have no village in geo_units,
    so they cannot be shown; they only contribute their district.
  * Village names repeat even inside one block, so clients always send lgd_code.
  * lgd_code is an opaque string (e.g. '629-B'); never cast it to int.
  * Rollups use geo_units.district (name) rather than walking parent_lgd_code.
"""
import re
from dataclasses import dataclass

from ..constants import CONFIDENCE_DISTRICT, CONFIDENCE_VILLAGE
from ..db import fetch_all, fetch_one
from ..errors import not_found, unprocessable

PINCODE_RE = re.compile(r"^\d{6}$")


# --------------------------------------------------------------------------
# Describing a unit (used in every response that shows a location)
# --------------------------------------------------------------------------
def describe_unit(conn, lgd_code: str | None) -> dict | None:
    if not lgd_code:
        return None
    row = fetch_one(
        conn,
        """
        SELECT u.lgd_code, u.level, u.name, u.state, u.district,
               b.lgd_code AS block_lgd_code, b.name AS block_name,
               d.lgd_code AS district_lgd_code
        FROM geo_units u
        LEFT JOIN geo_units b ON u.level = 'village' AND b.lgd_code = u.parent_lgd_code
        LEFT JOIN geo_units d ON d.level = 'district' AND d.name = u.district AND d.state = u.state
        WHERE u.lgd_code = %s
        """,
        (lgd_code,),
    )
    if row is None:
        return None
    level = row["level"]
    return {
        "lgd_code": row["lgd_code"],
        "precision": level,
        "village": row["name"] if level == "village" else None,
        "block": row["block_name"] if level == "village" else (row["name"] if level == "block" else None),
        "block_lgd_code": row["block_lgd_code"] if level == "village" else None,
        "district": row["district"] if level in ("village", "block", "district") else None,
        "district_lgd_code": row["district_lgd_code"],
        "state": row["state"],
    }


# --------------------------------------------------------------------------
# GET /locations/pincode/{pincode}
# --------------------------------------------------------------------------
def _pincode_rows(conn, pincode: str) -> list[dict]:
    return fetch_all(
        conn,
        """
        SELECT p.lgd_code, p.district_lgd_code, p.village_code_valid,
               v.name AS village, b.lgd_code AS block_lgd_code, b.name AS block,
               d.name AS district
        FROM pincode_directory p
        LEFT JOIN geo_units v ON v.lgd_code = p.lgd_code AND v.level = 'village'
        LEFT JOIN geo_units b ON b.lgd_code = v.parent_lgd_code
        LEFT JOIN geo_units d ON d.lgd_code = p.district_lgd_code
        WHERE p.pincode = %s
        """,
        (pincode,),
    )


def pincode_lookup(conn, pincode: str) -> dict:
    if not PINCODE_RE.match(pincode or ""):
        raise unprocessable("pincode must be exactly 6 digits", "invalid_pincode")
    rows = _pincode_rows(conn, pincode)
    if not rows:
        raise not_found(f"pincode {pincode} is not in the directory (pilot covers Rajasthan only)", "unknown_pincode")

    villages, seen = [], set()
    districts: dict[str, str] = {}
    unlisted = 0
    for r in rows:
        if r["district_lgd_code"]:
            districts[r["district_lgd_code"]] = r["district"]
        if r["village_code_valid"] and r["village"] and r["lgd_code"] not in seen:
            seen.add(r["lgd_code"])
            villages.append({
                "lgd_code": r["lgd_code"],
                "village": r["village"],
                "block": r["block"],
                "block_lgd_code": r["block_lgd_code"],
                "district": r["district"],
                "district_lgd_code": r["district_lgd_code"],
            })
        elif not r["village_code_valid"]:
            unlisted += 1
    villages.sort(key=lambda v: (v["village"].lower(), v["block"] or "", v["lgd_code"]))
    district_list = sorted(({"lgd_code": k, "name": v} for k, v in districts.items()), key=lambda d: d["name"] or "")
    return {
        "pincode": pincode,
        "state": "Rajasthan",
        "districts": district_list,
        "requires_district_choice": len(district_list) > 1,
        "village_count": len(villages),
        "unlisted_count": unlisted,
        "villages": villages,
    }


# --------------------------------------------------------------------------
# GET /locations  (cascading dropdowns: state > district > block > village)
# --------------------------------------------------------------------------
def _find_unit(conn, level: str, key: str, state: str | None = None) -> dict | None:
    """Match by lgd_code first, then by exact (case-insensitive) name."""
    row = fetch_one(conn, "SELECT lgd_code, name, state, district FROM geo_units WHERE level = %s AND lgd_code = %s",
                    (level, key))
    if row:
        return row
    if level == "block":
        return None  # block names repeat across districts; only codes are accepted
    return fetch_one(
        conn,
        """SELECT lgd_code, name, state, district FROM geo_units
           WHERE level = %s AND lower(name) = lower(%s) AND (%s::text IS NULL OR state = %s)
           ORDER BY lgd_code LIMIT 1""",
        (level, key, state, state),
    )


def list_locations(conn, state: str | None, district: str | None, block: str | None,
                   q: str | None, limit: int = 50) -> dict:
    if block:
        b = _find_unit(conn, "block", block)
        if not b:
            raise not_found(f"block {block!r} not found (send the block's lgd_code)", "unknown_block")
        items = fetch_all(conn, """SELECT lgd_code, name FROM geo_units
                                   WHERE level = 'village' AND parent_lgd_code = %s ORDER BY name, lgd_code""",
                          (b["lgd_code"],))
        return {"level": "village", "parent": {"level": "block", "lgd_code": b["lgd_code"], "name": b["name"],
                                                "district": b["district"]}, "items": items}

    if district:
        d = _find_unit(conn, "district", district, state)
        if not d:
            raise not_found(f"district {district!r} not found", "unknown_district")
        parent = {"level": "district", "lgd_code": d["lgd_code"], "name": d["name"], "state": d["state"]}
        if q:
            q = q.strip()
            if len(q) < 2:
                raise unprocessable("q must be at least 2 characters", "query_too_short")
            items = fetch_all(
                conn,
                """SELECT v.lgd_code, v.name, b.lgd_code AS block_lgd_code, b.name AS block
                   FROM geo_units v LEFT JOIN geo_units b ON b.lgd_code = v.parent_lgd_code
                   WHERE v.level = 'village' AND v.district = %s AND v.state = %s AND v.name ILIKE %s
                   ORDER BY (lower(v.name) = lower(%s)) DESC, (v.name ILIKE %s) DESC, v.name
                   LIMIT %s""",
                (d["name"], d["state"], f"%{q}%", q, f"{q}%", limit),
            )
            return {"level": "village", "parent": parent, "query": q, "items": items}
        items = fetch_all(conn, """SELECT lgd_code, name FROM geo_units
                                   WHERE level = 'block' AND parent_lgd_code = %s ORDER BY name, lgd_code""",
                          (d["lgd_code"],))
        return {"level": "block", "parent": parent, "items": items}

    if state:
        s = _find_unit(conn, "state", state)
        if not s:
            raise not_found(f"state {state!r} not found", "unknown_state")
        items = fetch_all(conn, """SELECT lgd_code, name FROM geo_units
                                   WHERE level = 'district' AND (parent_lgd_code = %s OR state = %s)
                                   ORDER BY name""", (s["lgd_code"], s["name"]))
        return {"level": "district", "parent": {"level": "state", "lgd_code": s["lgd_code"], "name": s["name"]},
                "items": items}

    # No filter: only states that actually have data loaded (the pilot: Rajasthan)
    items = fetch_all(conn, """SELECT s.lgd_code, s.name FROM geo_units s
                               WHERE s.level = 'state' AND EXISTS (
                                   SELECT 1 FROM geo_units d WHERE d.level = 'district' AND d.parent_lgd_code = s.lgd_code)
                               ORDER BY s.name""")
    return {"level": "state", "parent": None, "items": items}


# --------------------------------------------------------------------------
# Resolution used by POST /complaints
# --------------------------------------------------------------------------
@dataclass
class ResolvedLocation:
    lgd_code: str
    confidence: float
    status: str               # 'resolved' | 'low_confidence'
    method: str               # 'pincode' | 'manual'
    input_pincode: str | None
    precision: str            # 'village' | 'district'


def resolve_location(conn, method: str, pincode: str | None = None, lgd_code: str | None = None,
                     district_lgd_code: str | None = None) -> ResolvedLocation:
    if method == "gps":
        raise unprocessable("GPS location is not supported in v1; use method 'pincode' or 'manual'",
                            "gps_not_supported")

    if method == "manual":
        if not lgd_code:
            raise unprocessable("location.lgd_code (village) is required for method 'manual'", "lgd_code_required")
        row = fetch_one(conn, "SELECT level FROM geo_units WHERE lgd_code = %s", (lgd_code,))
        if not row:
            raise unprocessable(f"lgd_code {lgd_code!r} does not exist", "unknown_lgd_code")
        if row["level"] != "village":
            raise unprocessable(f"lgd_code {lgd_code!r} is a {row['level']}, manual entry needs a village",
                                "not_a_village")
        return ResolvedLocation(lgd_code, CONFIDENCE_VILLAGE, "resolved", "manual", None, "village")

    if method == "pincode":
        if not pincode or not PINCODE_RE.match(pincode):
            raise unprocessable("location.pincode must be exactly 6 digits", "invalid_pincode")
        rows = _pincode_rows(conn, pincode)
        if not rows:
            raise unprocessable(f"pincode {pincode} is not in the directory", "unknown_pincode")

        if lgd_code:
            valid = {r["lgd_code"] for r in rows if r["village_code_valid"] and r["village"]}
            if lgd_code not in valid:
                raise unprocessable(f"village {lgd_code!r} is not listed under pincode {pincode}",
                                    "village_not_in_pincode")
            return ResolvedLocation(lgd_code, CONFIDENCE_VILLAGE, "resolved", "pincode", pincode, "village")

        # "Not listed" -> district-level precision
        districts = sorted({r["district_lgd_code"] for r in rows if r["district_lgd_code"]})
        if district_lgd_code:
            if district_lgd_code not in districts:
                raise unprocessable(f"district {district_lgd_code!r} does not belong to pincode {pincode} "
                                    f"(expected one of {districts})", "district_not_in_pincode")
            chosen = district_lgd_code
        elif len(districts) == 1:
            chosen = districts[0]
        else:
            raise unprocessable(f"pincode {pincode} spans districts {districts}; send location.lgd_code (village) "
                                f"or location.district_lgd_code", "district_choice_required")
        return ResolvedLocation(chosen, CONFIDENCE_DISTRICT, "low_confidence", "pincode", pincode, "district")

    raise unprocessable(f"unknown location.method {method!r}", "invalid_location_method")
