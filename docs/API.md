# CDI Backend — API contract (v1)

Base URL: `http://localhost:8000` locally, the Render URL in production. Interactive docs: `/docs`.
All API routes are under `/api/v1`. No auth in v1 (except `/admin/*`). All timestamps are UTC ISO-8601 (`...Z`).

**Errors** always look like `{"detail": "...", "code": "machine_readable_code"}`.
Body validation errors: `422 {"detail": "invalid request", "code": "validation_error", "errors": [{"field", "message"}]}`.

> Changed vs. the master document (agreed during implementation):
> location is sent as **lgd_code**, never names; pincode lookup returns a **list of villages**;
> GPS is rejected in v1; `/locations` goes state → district → **block** → village.

---

## Location

### `GET /api/v1/locations/pincode/{pincode}`
A pincode covers many villages (median 28, max 616) and sometimes 2 districts. Show the list, let the citizen pick; offer **"Not listed"**.

```json
{
  "pincode": "301001",
  "state": "Rajasthan",
  "districts": [{"lgd_code": "87", "name": "Alwar"}, {"lgd_code": "782", "name": "Kotputli-Behror"}],
  "requires_district_choice": true,
  "village_count": 86,
  "unlisted_count": 1,
  "villages": [
    {"lgd_code": "...", "village": "...", "block": "...", "block_lgd_code": "...", "district": "Alwar", "district_lgd_code": "87"}
  ]
}
```
- `villages` sorted by name. Names can repeat — always send back `lgd_code`.
- `village_count` can be 0 (e.g. 342007) → go straight to "Not listed".
- 404 `unknown_pincode`, 422 `invalid_pincode`.

### `GET /api/v1/locations` — cascading dropdowns (manual entry)
| Query | Returns `items` of |
|---|---|
| *(none)* | states with data (`Rajasthan`) |
| `?state=Rajasthan` | 41 districts |
| `?district=<name or lgd_code>` | blocks of that district |
| `?block=<block lgd_code>` | villages of that block (block **code** only — block names repeat) |
| `?district=<...>&q=<2+ chars>` | village search within the district, with block names (typeahead; `limit` default 50) |

```json
{"level": "block", "parent": {"level": "district", "lgd_code": "92", "name": "Bhilwara", "state": "Rajasthan"},
 "items": [{"lgd_code": "629-B", "name": "Shahpura"}, ...]}
```
`lgd_code` is an opaque string (`"629-B"` exists). Never parse it as a number.

---

## Complaints

### `POST /api/v1/complaints` → 201
```json
{
  "input_type": "text",                      // "text" | "voice"
  "text": "No water for 3 months",           // required for text; for voice Flow B (the transcript)
  "audio_file": "<base64>",                  // voice Flow A only; "data:audio/...;base64," prefix OK; not stored
  "category": "water",                       // "water" | "roads"
  "severity": 4, "urgency": "high",          // OPTIONAL: both present = Flow B, both absent = Flow A
  "location": { ... see below ... },
  "language": "hi",
  "channel": "web",                          // "web" | "voice" | "messaging"
  "timestamp": "2026-09-28T09:14:00+05:30"   // optional, stored as client_timestamp
}
```
`location` — one of:
| Case | Body |
|---|---|
| Pincode, village picked | `{"method": "pincode", "pincode": "334305", "lgd_code": "<village>"}` |
| Pincode, "Not listed", 1 district | `{"method": "pincode", "pincode": "342007"}` |
| Pincode, "Not listed", 2 districts | `{"method": "pincode", "pincode": "301001", "district_lgd_code": "87"}` |
| Manual dropdowns | `{"method": "manual", "lgd_code": "<village>"}` |

Response:
```json
{
  "id": "REQ-000001", "category": "water", "severity": 4, "urgency": "high",
  "severity_source": "ai",                   // "ai" | "fallback" (AI failed -> 3 / medium)
  "location": {"lgd_code": "88357", "precision": "village", "village": "Aaidanpura", "block": "Barmer",
               "block_lgd_code": "582", "district": "Barmer", "district_lgd_code": "90", "state": "Rajasthan"},
  "location_resolution_status": "resolved",  // "resolved" | "low_confidence" (district precision)
  "status": "received",
  "created_at": "2026-09-28T03:44:00Z"
}
```
422 codes: `partial_ai_fields`, `text_required`, `audio_required`, `audio_too_large`, `gps_not_supported`,
`lgd_code_required`, `unknown_lgd_code`, `not_a_village`, `invalid_pincode`, `unknown_pincode`,
`village_not_in_pincode`, `district_choice_required`, `district_not_in_pincode`.

### `GET /api/v1/complaints/{id}`
```json
{"id": "REQ-000001", "status": "in_progress", "category": "water", "severity": 4, "urgency": "high",
 "description": "...", "language": "hi", "channel": "web", "location": {...}, "location_resolution_status": "resolved",
 "location_confidence": 1.0, "created_at": "...", "status_updated_at": "...",
 "history": [{"status": "in_progress", "previous_status": "open", "note": "team sent", "changed_at": "..."}]}
```
`status`: `open | in_progress | resolved | rejected`.

### `PATCH /api/v1/complaints/{id}/status`
Request `{"status": "resolved", "updated_by": "official_001", "note": "hand pump repaired"}` (`note` optional)
→ `{"id", "status", "previous_status", "updated_at"}`. Every change is kept in the history.

---

## Dashboard

### `GET /api/v1/categories` → `{"categories": ["water", "roads"]}`

### `GET /api/v1/hotspots?level=village|district|state&state=&district=&category=&limit=`
Only **active** complaints (`open`, `in_progress`) count. Sorted by `priority_score` desc. `category` omitted = both.
`district` accepts a name or lgd_code.

Village item:
```json
{"id": "88357", "level": "village", "name": "Aaidanpura", "block": "Barmer", "block_lgd_code": "582",
 "district": "Barmer", "district_lgd_code": "90", "state": "Rajasthan", "category": "water",
 "complaint_count": 2, "demand_score": 50.0, "infrastructure_gap": 82.83, "affected_population": 537,
 "population_imputed": false, "urgency_score": 45.0, "funding_gap_score": 50.0, "priority_score": 47.7,
 "computed_at": "..."}
```
District / state item (live rollup):
```json
{"id": "90", "level": "district", "name": "Barmer", "district": "Barmer", "state": "Rajasthan", "category": "water",
 "complaint_count": 4, "village_complaint_count": 4, "unassigned_complaint_count": 0, "hotspot_village_count": 2,
 "demand_score": 50.0, "infrastructure_gap": 82.83, "affected_population": 1290, "population_imputed": true,
 "urgency_score": 67.5, "funding_gap_score": 50.0, "priority_score": 59.95, "max_priority_score": 72.2}
```
- `unassigned_complaint_count` = complaints resolved only to the district ("Not listed").
- Scores are averages over village hotspots; `null` if a district has only unassigned complaints.
- Hotspots refresh a few seconds after each new complaint / status change.

### `GET /api/v1/recommendations/{lgd_code}?category=water`
Works for a **village** or a **district** lgd_code. Never waits on the AI.

| HTTP | `status` | Meaning / what to do |
|---|---|---|
| 200 | `ready` | Show it |
| 200 | `stale` | Show it; a refresh is running (`"refreshing": true`) — poll again later |
| 202 | `computing` | First time; numbers are in `evidence` already, text not yet. Poll after `retry_after_seconds` |
| 404 | `no_active_complaints` | Nothing active here for that category |
| 503 | `ai_unavailable` | AI failed recently, retry in a minute |

```json
{"lgd_code": "88357", "name": "Aaidanpura", "level": "village", "district": "Barmer", "state": "Rajasthan",
 "category": "water", "status": "ready", "priority_score": 47.7, "active_complaint_count": 2,
 "recommended_intervention": "...", "complaint_summary": "...",
 "evidence": [
   {"factor": "citizen_demand", "value": 50.0, "active_complaints": 2, "description": "..."},
   {"factor": "infrastructure_gap", "value": 82.83, "description": "..."},
   {"factor": "affected_population", "value": 537, "imputed": false, "description": "..."},
   {"factor": "urgency", "value": 45.0, "description": "..."},
   {"factor": "funding_gap", "value": 50.0, "is_synthetic": true, "description": "..."}
 ],
 "sample_size": 2, "is_mock": true, "generated_at": "..."}
```
`is_mock: true` = text came from the built-in mock AI, not Person 1's real one. Show a badge.

---

## For Person 1 (AI webhooks the backend calls)

**Process** (Flow A) — `POST $AI_PROCESS_URL`
`{"input_type", "text", "audio_file" (base64 or null), "language"}` → `{"text", "severity": 1-5, "urgency": "low|medium|high"}`
Timeout 10 s. Any error / invalid value → complaint saved with 3 / medium, `severity_source = "fallback"`.

**Narrate** — `POST $AI_NARRATE_URL`
`{"evidence": [...factors above...], "sample_descriptions": [≤30 most recent active], "context": {"lgd_code", "name", "level", "district", "state", "category", "priority_score"}}`
→ `{"summary", "recommended_intervention"}`. A one-element array response (n8n default) is also accepted.
Optional header via `AI_AUTH_HEADER="Header-Name: value"`.

## Admin (header `X-Admin-Key: $ADMIN_KEY`)
`POST /api/v1/admin/recompute` — recompute hotspots now.

`GET /api/v1/admin/complaints?status=&category=&district=&lgd_code=&q=&limit=50&offset=0` — officials' complaint list, newest first.
`district` = name or lgd_code; `lgd_code` = exact village/district; `q` searches id + description.
```json
{"total": 11, "by_status": {"open": 6, "in_progress": 5, "resolved": 0, "rejected": 0}, "limit": 50, "offset": 0,
 "complaints": [{"id": "REQ-000022", "status": "open", "category": "roads", "severity": 5, "urgency": "high",
   "severity_source": "ai", "description": "...", "lgd_code": "87", "location_name": "Alwar", "precision": "district",
   "district": "Alwar", "district_lgd_code": "87", "location_resolution_status": "low_confidence",
   "created_at": "...", "status_updated_at": null, "status_updated_by": null}]}
```
`by_status` counts ignore the `status` filter (use them for tab badges). Change a status with `PATCH /complaints/{id}/status`.
`GET /health` — DB reachable, migration v5 applied, AI mode (mock/external), last recompute.
