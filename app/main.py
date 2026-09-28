"""FastAPI entry point.

Run locally:   uvicorn app.main:app --reload --port 8000
Docs:          http://localhost:8000/docs
"""
import logging
from contextlib import asynccontextmanager

import psycopg2
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import db
from .config import settings
from .errors import ApiError
from .routers import api, mock_ai
from .services import recompute

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cdi")

REQUIRED_V5_COLUMNS = {("complaints", "severity_source"), ("complaints", "client_timestamp"),
                       ("hotspots", "population_imputed")}


def _schema_check() -> dict:
    with db.get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""SELECT table_name, column_name FROM information_schema.columns
                       WHERE table_schema = 'public' AND table_name IN ('complaints', 'hotspots')""")
        cols = set(cur.fetchall())
        cur.execute("SELECT to_regclass('public.complaint_status_history') IS NOT NULL, "
                    "to_regclass('public.complaint_id_seq') IS NOT NULL")
        has_history, has_seq = cur.fetchone()
    missing = sorted(f"{t}.{c}" for t, c in REQUIRED_V5_COLUMNS - cols)
    if not has_history:
        missing.append("complaint_status_history")
    if not has_seq:
        missing.append("complaint_id_seq")
    return {"schema_v5": not missing, "missing": missing}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_pool()
    try:
        check = _schema_check()
        if not check["schema_v5"]:
            log.error("Database is missing v5 objects %s -- run migrations/migration_v4_to_v5.sql", check["missing"])
    except Exception:
        log.exception("Startup schema check failed")
    log.info("AI process URL: %s | narrate URL: %s", settings.ai_process_url, settings.ai_narrate_url)
    yield
    db.close_pool()


app = FastAPI(
    title="Citizen Development Intelligence - Backend",
    version="1.0.0",
    description="Complaint ingestion, location resolution, hotspot scoring and recommendations (Rajasthan pilot).",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError):
    return JSONResponse(status_code=exc.status_code, content=exc.to_body())


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, exc: RequestValidationError):
    errors = [{"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": "invalid request", "code": "validation_error",
                                                  "errors": errors})


@app.exception_handler(psycopg2.Error)
async def db_error_handler(_: Request, exc: psycopg2.Error):
    log.exception("Database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database error, please retry", "code": "db_error"})


app.include_router(api.router)
app.include_router(mock_ai.router)


@app.get("/", include_in_schema=False)
def root():
    return {"service": "cdi-backend", "docs": "/docs", "health": "/health"}


@app.get("/health", tags=["meta"])
def health():
    try:
        check = _schema_check()
        db_ok = True
    except Exception as exc:
        check, db_ok = {"error": f"{type(exc).__name__}"}, False
    return {
        "status": "ok" if db_ok and check.get("schema_v5") else "degraded",
        "database": db_ok,
        **check,
        "ai_mode": {
            "process": "mock" if "/mock/ai/" in settings.ai_process_url else "external",
            "narrate": "mock" if "/mock/ai/" in settings.ai_narrate_url else "external",
        },
        "last_recompute": recompute.last_result,
        "last_recompute_error": recompute.last_error,
    }
