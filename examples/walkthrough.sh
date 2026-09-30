#!/usr/bin/env bash
# End-to-end curl walkthrough for the Temporary Event Radio Spectrum API.
# Usage: ./examples/walkthrough.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
J() { python3 -m json.tool; }

post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "${2:-}"; }

echo "== health =="
curl -s "$BASE/health"; echo

echo "== 1. invalid submission (422, whole request rejected) =="
post /api/applications '{
  "name":"bad",
  "circle":{"cx":0,"cy":0,"radius":-5},
  "frequency":{"low":200,"high":100},
  "time_window":{"start":"2026-10-01T10:00:00Z","end":"2026-10-01T08:00:00Z"}
}' | J

echo "== 2. submit A =="
A=$(post /api/applications '{
  "name":"Festival A",
  "circle":{"cx":0,"cy":0,"radius":1000},
  "frequency":{"low":100,"high":110},
  "time_window":{"start":"2026-10-01T08:00:00Z","end":"2026-10-01T12:00:00Z"}
}')
echo "$A" | J
A_ID=$(echo "$A" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

echo "== 3. preview A =="
post "/api/applications/$A_ID/preview" | J

echo "== 4. approve A at revision 0 =="
post "/api/applications/$A_ID/approve" '{"expected_revision":0}' | J

echo "== 5. submit B (1 MHz gap < 2 MHz guard band) =="
B=$(post /api/applications '{
  "name":"Marathon B",
  "circle":{"cx":100,"cy":0,"radius":1000},
  "frequency":{"low":111,"high":112},
  "time_window":{"start":"2026-10-01T09:00:00Z","end":"2026-10-01T11:00:00Z"}
}')
B_ID=$(echo "$B" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$B_ID/preview" | J

echo "== 6. stale approval (expected 0, current 1) =="
post "/api/applications/$B_ID/approve" '{"expected_revision":0}' | J || true

echo "== 7. current revision but interference =="
post "/api/applications/$B_ID/approve" '{"expected_revision":1}' | J || true

echo "== 8. revoke A =="
post "/api/applications/$A_ID/revoke" '{"reason":"postponed"}' | J

echo "== 9. approve B at revision 2 =="
post "/api/applications/$B_ID/approve" '{"expected_revision":2}' | J

echo "== 10. batch-approve three non-interfering devices (revs 4,5,6) =="
D1=$(post /api/applications '{
  "name":"Stage device D1",
  "circle":{"cx":0,"cy":0,"radius":1000},
  "frequency":{"low":200,"high":201},
  "time_window":{"start":"2026-10-02T08:00:00Z","end":"2026-10-02T12:00:00Z"}
}')
D2=$(post /api/applications '{
  "name":"Stage device D2",
  "circle":{"cx":0,"cy":0,"radius":1000},
  "frequency":{"low":203,"high":204},
  "time_window":{"start":"2026-10-02T08:00:00Z","end":"2026-10-02T12:00:00Z"}
}')
D3=$(post /api/applications '{
  "name":"Stage device D3",
  "circle":{"cx":0,"cy":0,"radius":1000},
  "frequency":{"low":206,"high":207},
  "time_window":{"start":"2026-10-02T08:00:00Z","end":"2026-10-02T12:00:00Z"}
}')
D1_ID=$(echo "$D1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
D2_ID=$(echo "$D2" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
D3_ID=$(echo "$D3" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post /api/applications/batch-approve "{
  \"expected_revision\":3,
  \"application_ids\":[$D1_ID,$D2_ID,$D3_ID]
}" | J

echo "== 11. snapshot at revision 5 contains only the first two group members =="
curl -s "$BASE/api/revision/5" | J

echo "== 12. batch-approve an interfering pair -> whole group rejected =="
E1=$(post /api/applications '{
  "name":"Device E1",
  "circle":{"cx":0,"cy":0,"radius":1000},
  "frequency":{"low":300,"high":310},
  "time_window":{"start":"2026-10-03T08:00:00Z","end":"2026-10-03T12:00:00Z"}
}')
E2=$(post /api/applications '{
  "name":"Device E2",
  "circle":{"cx":0,"cy":0,"radius":1000},
  "frequency":{"low":305,"high":315},
  "time_window":{"start":"2026-10-03T08:00:00Z","end":"2026-10-03T12:00:00Z"}
}')
E1_ID=$(echo "$E1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
E2_ID=$(echo "$E2" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post /api/applications/batch-approve "{
  \"expected_revision\":6,
  \"application_ids\":[$E1_ID,$E2_ID]
}" | J || true

echo "== 13. history snapshots =="
curl -s "$BASE/api/revision/1" | J
curl -s "$BASE/api/revision/2" | J
curl -s "$BASE/api/revision/current" | J
