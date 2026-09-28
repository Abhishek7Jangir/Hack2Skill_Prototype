"""All /api/v1 routes. Thin: validate input, call a service, shape the HTTP response.

Route handlers are plain `def` (not async): psycopg2 is blocking, so FastAPI runs
them in its threadpool and the event loop stays free (the mock AI endpoints, which
the backend calls on itself by default, are async and never wait on the pool).
"""
from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

from ..config import settings
from ..constants import CATEGORIES
from ..db import get_conn
from ..errors import ApiError
from ..schemas import ComplaintIn, StatusUpdateIn
from ..services import complaints, hotspots, locations, recommendations, recompute

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------- complaints
@router.post("/complaints", status_code=201, tags=["complaints"])
def create_complaint(body: ComplaintIn):
    """Flow A (no severity/urgency: backend calls the AI) or Flow B (AI already filled them)."""
    return complaints.create_complaint(body)


@router.get("/complaints/{complaint_id}", tags=["complaints"])
def get_complaint(complaint_id: str):
    return complaints.get_complaint(complaint_id)


@router.patch("/complaints/{complaint_id}/status", tags=["complaints"])
def update_status(complaint_id: str, body: StatusUpdateIn):
    return complaints.update_status(complaint_id, body)


# ---------------------------------------------------------------- locations
@router.get("/locations/pincode/{pincode}", tags=["locations"])
def pincode_lookup(pincode: str):
    with get_conn() as conn:
        return locations.pincode_lookup(conn, pincode)


@router.get("/locations", tags=["locations"])
def list_locations(
    state: str | None = Query(None, description="State name or lgd_code -> returns its districts"),
    district: str | None = Query(None, description="District name or lgd_code -> returns its blocks"),
    block: str | None = Query(None, description="Block lgd_code -> returns its villages"),
    q: str | None = Query(None, description="With district: search villages by name (typeahead)"),
    limit: int = Query(50, ge=1, le=200),
):
    with get_conn() as conn:
        return locations.list_locations(conn, state, district, block, q, limit)


# ---------------------------------------------------------------- categories
@router.get("/categories", tags=["meta"])
def list_categories():
    return {"categories": list(CATEGORIES)}


# ---------------------------------------------------------------- hotspots
@router.get("/hotspots", tags=["hotspots"])
def list_hotspots(
    level: str = Query("village", description="village | district | state"),
    state: str | None = None,
    district: str | None = Query(None, description="District name or lgd_code"),
    category: str | None = Query(None, description="water | roads (omit for both)"),
    limit: int = Query(200, ge=1, le=5000),
):
    with get_conn() as conn:
        return hotspots.list_hotspots(conn, level, state, district, category, limit)


# ---------------------------------------------------------------- recommendations
@router.get("/recommendations/{lgd_code}", tags=["recommendations"])
def get_recommendation(lgd_code: str, category: str = Query(..., description="water | roads")):
    """200 ready | 200 stale (refreshing in background) | 202 computing | 404 no active complaints."""
    status_code, body = recommendations.get_recommendation(lgd_code, category)
    return JSONResponse(status_code=status_code, content=body)


# ---------------------------------------------------------------- admin
@router.post("/admin/recompute", tags=["admin"])
def admin_recompute(x_admin_key: str | None = Header(None)):
    if not settings.admin_key:
        raise ApiError(503, "admin endpoints are disabled: set ADMIN_KEY", "admin_disabled")
    if x_admin_key != settings.admin_key:
        raise ApiError(401, "missing or wrong X-Admin-Key header", "unauthorized")
    return {"status": "ok", "result": recompute.run_now()}
