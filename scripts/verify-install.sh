#!/usr/bin/env bash
set -Eeuo pipefail

# Verify a 9Router + OpenCode SWE-AF install end to end.
# Checks (in order):
#   1. 9Router gateway reachable and the configured model exists
#   2. swe-fast / swe-agent containers running
#   3. HARNESS_MODEL resolved inside each container matches the expected model
#   4. opencode.json provider matches HARNESS_MODEL's provider prefix
#   5. control-plane + node health endpoints respond

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'EOF'
Usage: verify-install.sh [EXPECTED_MODEL]

  EXPECTED_MODEL   optional; defaults to 9router/claude-sonnet-4-6.
                   When set, the script also asserts each container's
                   HARNESS_MODEL equals it.

Exit codes: 0 = all checks pass; non-zero = first failing check.
EOF
  exit 0
fi

for dep in curl jq docker; do
  command -v "$dep" >/dev/null || { echo "Missing dependency: $dep" >&2; exit 127; }
done

expected="${1:-9router/claude-sonnet-4-6}"
provider="${expected%%/*}"
base_url="http://10.20.10.133:20128/v1"
data_dir="${SWE_AF_DATA:-/opt/swe-af-data}"
control_plane="http://10.20.10.103:5080"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

# --- 1. 9Router gateway + model ----------------------------------------------
api_key="${OMNI_API_KEY:-${ROUTER_API_KEY:-}}"
if [[ -z "${api_key}" ]]; then
  fail "OMNI_API_KEY (or ROUTER_API_KEY) is not set in the environment"
fi
code=$(curl -fsS --connect-timeout 3 --max-time 10 \
  -H "Authorization: Bearer ${api_key}" \
  "${base_url}/models" -o /tmp/verify-models.json 2>/dev/null && echo ok || echo err)
[[ "${code}" == ok ]] || fail "9Router not reachable at ${base_url}/models"
if ! jq -e --arg m "${expected##*/}" '.data[].id | select(test($m))' /tmp/verify-models.json >/dev/null 2>&1; then
  fail "model '${expected##*/}' not found in 9Router /v1/models (got $(jq '.data | length' /tmp/verify-models.json) models)"
fi
pass "9Router reachable; model '${expected##*/}' present"

# --- 2. Containers -----------------------------------------------------------
for node in swe-af-swe-fast-1 swe-af-swe-agent-1; do
  docker inspect -f '{{.State.Running}}' "$node" 2>/dev/null | grep -q true \
    || fail "container $node is not running"
done
pass "containers swe-fast / swe-agent running"

# --- 3. HARNESS_MODEL inside containers --------------------------------------
for node in swe-af-swe-fast-1 swe-af-swe-agent-1; do
  actual=$(docker exec "$node" printenv HARNESS_MODEL 2>/dev/null | tr -d '\r')
  [[ -n "${actual}" ]] || fail "HARNESS_MODEL unset inside $node"
  [[ "${actual}" == "${expected}" ]] \
    || fail "HARNESS_MODEL inside $node is '${actual}', expected '${expected}'"
done
pass "HARNESS_MODEL=${expected} in both containers"

# --- 4. opencode.json provider -----------------------------------------------
for node in swe-af-swe-fast-1 swe-af-swe-agent-1; do
  cfg=/root/.config/opencode/opencode.json
  docker exec "$node" sh -c "grep -q '\"${provider}\"' ${cfg}" 2>/dev/null \
    || fail "provider '${provider}' missing from ${cfg} in $node"
done
pass "provider '${provider}' defined in opencode.json in both containers"

# --- 5. Health endpoints ------------------------------------------------------
curl -fsS --connect-timeout 3 --max-time 10 "${control_plane}/api/v1/health" >/dev/null \
  || fail "control-plane health at ${control_plane}/api/v1/health"
for port in 5803 5804; do
  curl -fsS --connect-timeout 3 --max-time 10 "http://127.0.0.1:${port}/health" >/dev/null \
    || fail "node health at 127.0.0.1:${port}/health"
done
pass "control-plane + swe-agent + swe-fast health endpoints responding"

echo
echo "All checks passed. Trigger a build with:"
echo "  ./scripts/run-fast.sh --repo-path ${data_dir}/test-repos/swe-af-smoke-test \\"
echo "    --goal 'Add a /health endpoint' --profile configs/opencode-balanced.json"
