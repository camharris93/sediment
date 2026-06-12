#!/usr/bin/env bash
# Reset the sediment demo to a clean cold-start state.
#   - drops the warehouse + profiles + dbt target (so `sediment up` rebuilds live)
#   - removes the bring-your-own-data "sales" dataset created during Act 5
#   - restores any tracked files the demo edited (e.g. _sources.yml)
# Safe to run anytime. Run it between rehearsals and right before going live.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "› dropping the warehouse + dbt target …"
python run.py clean

echo "› removing the Act-5 demo dataset (if present) …"
rm -rf datasets/sales
rm -f dbt_project/models/staging/sales__*.sql dbt_project/models/staging/_sales__*.yml

echo "› restoring tracked files the demo may have edited …"
git checkout -- dbt_project/models/staging/_sources.yml 2>/dev/null || true

echo "✓ reset complete — 'sediment up' will now rebuild AnAge from scratch."
