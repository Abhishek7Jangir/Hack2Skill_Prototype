# CDI Backend — Citizen Development Intelligence (Rajasthan pilot)

FastAPI + Supabase Postgres. Turns citizen complaints into village hotspots, priority scores and
AI-narrated recommendations. API contract for frontend/AI: **[docs/API.md](docs/API.md)**.

## 1. One-time database step

Run `migrations/migration_v4_to_v5.sql` in the Supabase SQL editor (safe to re-run). It adds
`complaint_id_seq`, `complaints.severity_source`, `complaints.client_timestamp`,
`hotspots.population_imputed` and `complaint_status_history`. The last query prints 5 × `true`.

## 2. Run locally (Windows PowerShell, Python 3.12)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
copy .env.example .env          # then put your DATABASE_URL in .env
uvicorn app.main:app --reload --port 8000
```
Open http://localhost:8000/docs and http://localhost:8000/health (`"status": "ok"` means DB reachable + v5 applied).

Tests:
```powershell
pytest                              # unit tests, no DB needed
python scripts/smoke_test.py        # full HTTP test against the running server (writes a few
                                    # "[SMOKE TEST]" complaints, then sets them to 'rejected')
python scripts/recompute_hotspots.py   # replaces build_hotspots.py; prints the scored table
```

> If you run on a port other than 8000, set `PORT` too (the mock AI URL is built from it).

**Test console:** open `demo/index.html` in a browser (double-click it). It talks to the Render URL by default
(change it in the header, e.g. `http://127.0.0.1:8000`). Enter the admin key top-right for the Admin tab.
Every request/response is visible in the *Request log* tab.

## 3. Deploy on Render

1. Push this folder to a GitHub repo (`.env` is git-ignored).
2. Render → New → Blueprint → pick the repo (uses `render.yaml`).
3. Set `DATABASE_URL` to the Supabase **Session pooler** string (Render can't reach the IPv6-only direct host).
4. `ADMIN_KEY` is generated for you (Render → Environment).
5. When Person 1's n8n webhooks exist, set `AI_PROCESS_URL`, `AI_NARRATE_URL` (and `AI_AUTH_HEADER` if needed). No code change.

## 4. How it works

```
POST /complaints ─► validate ─► resolve location ─► (Flow A) AI webhook, 10 s, fallback 3/medium ─► insert
                                                                                         │
                                                  debounced recompute (5 s) ◄────────────┘   also after PATCH status
                                                           │
                                     hotspots table (village × category, active complaints only)
                                                           │
             GET /hotspots (village rows; district/state = live rollups)   GET /recommendations (lazy AI narration)
```

| Area | File |
|---|---|
| Routes (thin) | `app/routers/api.py`, `app/routers/mock_ai.py` |
| Complaint create / status | `app/services/complaints.py` |
| Pincode + dropdowns + resolution | `app/services/locations.py` |
| Scoring job (was build_hotspots.py) | `app/services/scoring.py` |
| Rollups | `app/services/hotspots.py` |
| Recommendations + evidence | `app/services/recommendations.py` |
| All AI calls | `app/ai_client.py` |
| Debounced recompute | `app/services/recompute.py` |

### Scoring (unchanged weights)
`priority = 0.35·demand + 0.25·infra_gap + 0.20·pop_norm + 0.10·urgency + 0.10·funding_gap`

Decisions applied on top of the validated script:
- **Active** = `complaint_status IN ('open','in_progress')` everywhere (hotspots, rollups, AI sample).
- Only village-resolved complaints form hotspots; district-level ("Not listed") ones appear in rollups as `unassigned_complaint_count`.
- Min-max normalisation **per category**, `50` when all values are equal — one function, `normalize()`.
- Missing / 0 Census population → median of the block's villages (pop > 0), else district median; `population_imputed = true`. Never written to `government_indicators`.
- Funding gap: real PMGSY for roads, synthetic 50 for water until NRDWP arrives.
- Buckets with no active complaints left are **deleted** from `hotspots`.

### Numbers after the switch
On the 8 test complaints the scores change from the old table (per-category normalisation + imputed population):

| Village | Cat | Old priority | New |
|---|---|---|---|
| 947158 Barmer | water | 69.7 | 72.2 (pop 753, imputed) |
| 88357 Barmer | water | 81.4 | 47.7 |
| 94813 Bhilwara | roads | 23.3 | 40.8 |
| 947046 Balotra | roads | 14.5 | 32.0 (pop 593, imputed) |
| 947045 Balotra | roads | 12.5 | 30.0 (pop 593, imputed) |

All demand scores are 50 because every village in a category has the same count (2 or 1), which is correct under the new rule.

## 5. Known limits (v1)
- GPS location → 422 `gps_not_supported`.
- Voice audio is not stored; if the AI fails on a voice complaint, `raw_text` is a placeholder and the row has `severity_source='fallback'`.
- Recommendations are generated lazily on first view; the stored text is flagged stale when the underlying complaints or scores change.
- Render free tier sleeps after ~15 min idle (first request then takes ~30-60 s).
