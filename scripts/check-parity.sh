#!/usr/bin/env bash
# Archimedes — local↔prod parity check
# Verifies the local stack matches production expectations:
#   - LLM backend is live (not canned)
#   - Corpus is mounted and non-empty
#
# Usage: ./scripts/check-parity.sh [BASE_URL]
#   BASE_URL defaults to http://localhost:8000

set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"
FAIL=0

echo "Archimedes local↔prod parity check"
echo "Target: $BASE_URL/health"
echo ""

HEALTH=$(curl -sf "$BASE_URL/health" 2>/dev/null) || {
    echo "FAIL: Cannot reach $BASE_URL/health — is the stack running?"
    echo "  Start with: docker compose up -d --build"
    exit 1
}

# Extract fields (portable jq or python)
if command -v jq &>/dev/null; then
    CORPUS=$(echo "$HEALTH" | jq -r '.corpus_papers // 0')
    LLM=$(echo "$HEALTH" | jq -r '.llm_backend // "unavailable"')
    PROVIDER=$(echo "$HEALTH" | jq -r '.llm_provider // "unknown"')
else
    CORPUS=$(python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('corpus_papers',0))" <<< "$HEALTH")
    LLM=$(python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('llm_backend','unavailable'))" <<< "$HEALTH")
    PROVIDER=$(python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('llm_provider','unknown'))" <<< "$HEALTH")
fi

# Check: corpus non-empty
if [ "$CORPUS" -gt 0 ] 2>/dev/null; then
    echo "PASS: corpus_papers=$CORPUS"
else
    echo "FAIL: corpus_papers=$CORPUS (expected > 0)"
    echo "  Fix: ensure data/corpus/manifest.jsonl exists and is mounted"
    FAIL=1
fi

# Check: LLM backend is live (not canned/unavailable)
if [ "$LLM" != "canned-fallback" ] && [ "$LLM" != "unavailable" ] && [ "$LLM" != "canned-fusion-fallback" ]; then
    echo "PASS: llm_backend=$LLM (provider=$PROVIDER)"
else
    echo "FAIL: llm_backend=$LLM (expected live model)"
    echo "  Fix: set LLM_PROVIDER + LLM_API_KEY or LLM_AUTH_TOKEN+LLM_BASE_URL in .env"
    FAIL=1
fi

echo ""
if [ "$FAIL" -eq 0 ]; then
    echo "All checks PASSED — local stack matches production expectations."
else
    echo "Some checks FAILED — see above for fixes."
fi
exit $FAIL
