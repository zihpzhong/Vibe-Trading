#!/usr/bin/env bash
# AGT live-runner 运维：清理重复 session、重启 runner / Ops: prune sessions, restart runner
set -euo pipefail

API="${API:-http://127.0.0.1:8899}"
BROKER="${BROKER:-bitget}"
PREFIX="live-runner:${BROKER}"

echo "=== stop runner ==="
curl -sf -X POST "${API}/live/runner/stop" \
  -H "Content-Type: application/json" \
  -d "{\"broker\":\"${BROKER}\"}" | python3 -m json.tool || true

echo "=== list live-runner sessions ==="
SESSIONS=$(curl -sf "${API}/sessions?prefix=live-runner")
echo "$SESSIONS" | python3 -m json.tool

KEEP=$(echo "$SESSIONS" | python3 -c "
import json, sys
rows = json.load(sys.stdin)
same = [r for r in rows if r.get('title') == '${PREFIX}']
if not same:
    print('')
    sys.exit(0)
best = max(same, key=lambda r: r.get('updated_at') or r.get('created_at') or '')
print(best['session_id'])
")

if [[ -z "${KEEP}" ]]; then
  echo "No existing session to keep; start will create one."
else
  echo "=== keep session ${KEEP}; delete others ==="
  echo "$SESSIONS" | python3 -c "
import json, sys, urllib.request
keep = '${KEEP}'
prefix = '${PREFIX}'
api = '${API}'
rows = json.load(sys.stdin)
for r in rows:
    sid = r['session_id']
    if r.get('title') == prefix and sid != keep:
        req = urllib.request.Request(f'{api}/sessions/{sid}', method='DELETE')
        try:
            urllib.request.urlopen(req, timeout=30)
            print('deleted', sid)
        except Exception as exc:
            print('failed', sid, exc)
"
fi

echo "=== start runner ==="
curl -sf -X POST "${API}/live/runner/start" \
  -H "Content-Type: application/json" \
  -d "{\"broker\":\"${BROKER}\"}" | python3 -m json.tool

echo "=== live status ==="
curl -sf "${API}/live/status?broker=${BROKER}" | python3 -m json.tool
