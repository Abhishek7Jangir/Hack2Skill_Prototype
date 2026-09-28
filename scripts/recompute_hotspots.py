"""Command-line replacement for build_hotspots.py: runs the same job the API runs.

    python scripts/recompute_hotspots.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.services.scoring import run_hotspot_job  # noqa: E402

with db.get_conn() as conn:
    summary = run_hotspot_job(conn)
print(summary)

with db.get_conn() as conn:
    rows = db.fetch_all(conn, """
        SELECT h.lgd_code, v.name, v.district, h.category, h.complaint_count, h.demand_score,
               h.infrastructure_gap, h.affected_population, h.population_imputed, h.urgency_score,
               h.funding_gap_score, h.priority_score
        FROM hotspots h JOIN geo_units v ON v.lgd_code = h.lgd_code
        ORDER BY h.category, h.priority_score DESC LIMIT 50""")
print(f"\n{'lgd_code':<9} {'village':<22} {'district':<14} {'cat':<6} {'n':>3} {'demand':>6} {'infra':>6} "
      f"{'pop':>7} {'urg':>5} {'fund':>5} {'prio':>6}")
for r in rows:
    pop = f"{r['affected_population']}{'*' if r['population_imputed'] else ''}"
    print(f"{r['lgd_code']:<9} {r['name'][:22]:<22} {r['district'][:14]:<14} {r['category']:<6} "
          f"{r['complaint_count']:>3} {float(r['demand_score']):>6.1f} {float(r['infrastructure_gap']):>6.1f} "
          f"{pop:>7} {float(r['urgency_score']):>5.1f} {float(r['funding_gap_score']):>5.1f} "
          f"{float(r['priority_score']):>6.1f}")
print("\n* = population imputed (block/district median)")
