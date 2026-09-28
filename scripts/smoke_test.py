"""End-to-end HTTP smoke test against a RUNNING server.

    uvicorn app.main:app --port 8000          (terminal 1)
    python scripts/smoke_test.py               (terminal 2)
    python scripts/smoke_test.py https://your-app.onrender.com

It writes a few real complaints (text starts with "[SMOKE TEST]") and, at the end,
PATCHes them to 'rejected' so they never count toward scores. To delete them fully:

    DELETE FROM complaint_status_history WHERE complaint_id IN (SELECT id FROM complaints_raw WHERE raw_text LIKE '[SMOKE TEST]%');
    DELETE FROM complaints WHERE id IN (SELECT id FROM complaints_raw WHERE raw_text LIKE '[SMOKE TEST]%');
    DELETE FROM complaints_raw WHERE raw_text LIKE '[SMOKE TEST]%';
"""
import os
import sys
import time

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BASE_URL", "http://127.0.0.1:8000")).rstrip("/")
API = f"{BASE}/api/v1"
ADMIN_KEY = os.environ.get("ADMIN_KEY")
TAG = "[SMOKE TEST]"

client = httpx.Client(timeout=30)
created: list[str] = []
passed = 0


def check(cond, msg, payload=None):
    global passed
    if not cond:
        print(f"  FAIL - {msg}")
        if payload is not None:
            print(f"         {payload}")
        cleanup()
        sys.exit(1)
    passed += 1
    print(f"  ok   - {msg}")


def cleanup():
    for cid in created:
        client.patch(f"{API}/complaints/{cid}/status",
                     json={"status": "rejected", "updated_by": "smoke_test", "note": "smoke test cleanup"})
    if created:
        print(f"\n  cleanup: {len(created)} smoke complaints set to 'rejected': {', '.join(created)}")


print(f"Smoke test against {BASE}\n")

print("== health / meta")
r = client.get(f"{BASE}/health")
check(r.status_code == 200 and r.json()["database"], "health: database reachable", r.text)
check(r.json().get("schema_v5"), "health: migration v5 applied", r.json().get("missing"))
print(f"         ai_mode = {r.json()['ai_mode']}")
r = client.get(f"{API}/categories")
check(r.json() == {"categories": ["water", "roads"]}, "categories")

print("== locations")
r = client.get(f"{API}/locations/pincode/334305")
check(r.status_code == 200 and r.json()["village_count"] == 95, "pincode 334305 -> 95 listed villages")
r = client.get(f"{API}/locations/pincode/301001")
check(r.json()["requires_district_choice"], "pincode 301001 -> requires district choice")
r = client.get(f"{API}/locations/pincode/342007")
j = r.json()
check(j["village_count"] == 0 and j["districts"][0]["name"] == "Jodhpur", "pincode 342007 -> Jodhpur only")
check(client.get(f"{API}/locations/pincode/999999").status_code == 404, "unknown pincode -> 404")
r = client.get(f"{API}/locations")
check([i["name"] for i in r.json()["items"]] == ["Rajasthan"], "states list")
check(len(client.get(f"{API}/locations", params={"state": "Rajasthan"}).json()["items"]) == 41, "41 districts")
blocks = client.get(f"{API}/locations", params={"district": "Bhilwara"}).json()["items"]
check(any(b["lgd_code"] == "629-B" for b in blocks), "Bhilwara blocks include Shahpura (629-B)")
check(len(client.get(f"{API}/locations", params={"block": "629-B"}).json()["items"]) == 103, "Shahpura -> 103 villages")

print("== complaints")
body = {"input_type": "text", "text": f"{TAG} no water supply", "category": "water",
        "location": {"method": "manual", "lgd_code": "88357"}, "language": "en", "channel": "web",
        "timestamp": "2026-09-28T09:14:00+05:30"}
r = client.post(f"{API}/complaints", json=body)
check(r.status_code == 201 and r.json()["id"].startswith("REQ-"), "Flow A (backend calls AI) -> 201", r.text)
flow_a = r.json(); created.append(flow_a["id"])
print(f"         {flow_a['id']} severity={flow_a['severity']} urgency={flow_a['urgency']} source={flow_a['severity_source']}")

r = client.post(f"{API}/complaints", json={**body, "severity": 4, "urgency": "high"})
check(r.status_code == 201 and r.json()["severity"] == 4, "Flow B (pre-processed) -> 201 keeps severity", r.text)
created.append(r.json()["id"])

pin = client.get(f"{API}/locations/pincode/334305").json()["villages"][0]
r = client.post(f"{API}/complaints", json={**body, "category": "roads", "text": f"{TAG} potholes",
                                            "location": {"method": "pincode", "pincode": "334305", "lgd_code": pin["lgd_code"]}})
check(r.status_code == 201 and r.json()["location"]["precision"] == "village", "pincode + picked village", r.text)
created.append(r.json()["id"])

r = client.post(f"{API}/complaints", json={**body, "location": {"method": "pincode", "pincode": "301001", "district_lgd_code": "87"}})
check(r.status_code == 201 and r.json()["location_resolution_status"] == "low_confidence", "pincode + Not listed -> district", r.text)
created.append(r.json()["id"])

r = client.post(f"{API}/complaints", json={**body, "location": {"method": "pincode", "pincode": "301001"}})
check(r.status_code == 422 and r.json()["code"] == "district_choice_required", "2-district pincode without choice -> 422")
r = client.post(f"{API}/complaints", json={**body, "location": {"method": "gps", "lat": 26.9, "lng": 75.8}})
check(r.status_code == 422 and r.json()["code"] == "gps_not_supported", "gps -> 422")
r = client.post(f"{API}/complaints", json={**body, "category": "power"})
check(r.status_code == 422 and r.json()["code"] == "validation_error", "bad category -> 422 validation_error")

r = client.get(f"{API}/complaints/{flow_a['id']}")
check(r.status_code == 200 and r.json()["status"] == "open", "GET complaint -> open")
r = client.patch(f"{API}/complaints/{flow_a['id']}/status",
                 json={"status": "in_progress", "updated_by": "smoke_test", "note": "checking"})
check(r.status_code == 200 and r.json()["previous_status"] == "open", "PATCH status -> in_progress")
check(client.get(f"{API}/complaints/{flow_a['id']}").json()["history"][0]["note"] == "checking", "status history stored")
check(client.get(f"{API}/complaints/REQ-NOPE").status_code == 404, "unknown complaint -> 404")

print("== hotspots (waiting for the debounced recompute)")
if ADMIN_KEY:
    r = client.post(f"{API}/admin/recompute", headers={"X-Admin-Key": ADMIN_KEY})
    check(r.status_code == 200, "admin recompute", r.text)
else:
    time.sleep(8)
r = client.get(f"{API}/hotspots", params={"level": "village", "category": "water", "district": "Barmer"})
hs = {h["id"]: h for h in r.json()["hotspots"]}
check("88357" in hs, "88357 is a water hotspot", r.json())
r = client.get(f"{API}/hotspots", params={"level": "district"})
check(r.status_code == 200 and r.json()["count"] > 0, "district rollup")
alwar = [h for h in r.json()["hotspots"] if h["id"] == "87" and h["category"] == "water"]
check(alwar and alwar[0]["unassigned_complaint_count"] >= 1, "Alwar water shows the unassigned complaint")
check(client.get(f"{API}/hotspots", params={"level": "state"}).status_code == 200, "state rollup")

print("== recommendations")
r = client.get(f"{API}/recommendations/88357", params={"category": "water"})
check(r.status_code in (200, 202), f"recommendation first call -> {r.status_code} {r.json().get('status')}")
for _ in range(15):
    if r.status_code == 200 and r.json()["status"] == "ready":
        break
    time.sleep(1)
    r = client.get(f"{API}/recommendations/88357", params={"category": "water"})
check(r.status_code == 200 and r.json()["recommended_intervention"], "recommendation ready", r.text)
print(f"         is_mock={r.json()['is_mock']} priority={r.json()['priority_score']} "
      f"summary={r.json()['complaint_summary'][:70]!r}")
check(client.get(f"{API}/recommendations/88357", params={"category": "power"}).status_code == 422, "bad category -> 422")

cleanup()
print(f"\nALL {passed} CHECKS PASSED")
