#!/usr/bin/env bash
# Build the Python image and verify it registers and executes a real reasoner
# call against agentfield/control-plane:v0.1.57, the control plane production
# runs, with zero model credentials.
#
# With SMOKE_BASELINE_SHA set, it also builds that commit's image (with only
# the Command Code pin moved to 1.88.5, since 1.82.2 no longer starts) and
# asserts both register the same reasoners and give the same result. That
# comparison is a one-off proof for a deploy, not a standing gate: a PR that
# adds a reasoner would fail it on purpose.
set -euo pipefail

BASELINE_SHA="${SMOKE_BASELINE_SHA:-}"
CONTROL_PLANE_IMAGE="agentfield/control-plane:v0.1.57"
NEW_IMAGE="swe-af-python:new"
BASELINE_IMAGE="swe-af-python:baseline"
NODE_PORT="8003"

RUN_ID="$(python3 -c 'import secrets; print(secrets.token_hex(6))')"
# Generate a per-run throwaway key so API-key authentication is exercised on
# registration, heartbeats, status callbacks, notes, and execute calls without
# storing any secret in the repository.
API_KEY="ci-throwaway-$(python3 -c 'import secrets; print(secrets.token_hex(16))')"

NETWORK_NAME="swe-af-smoke-${RUN_ID}"
CP_CONTAINER="swe-af-cp-${RUN_ID}"
NEW_CONTAINER="swe-af-new-${RUN_ID}"
BASELINE_CONTAINER="swe-af-baseline-${RUN_ID}"

WORK_DIR="$(mktemp -d)"
# _worktrees/ is listed in .dockerignore, and we also build the candidate image
# before populating this directory so baseline files never enter the candidate
# build context.
BASELINE_DIR="_worktrees/baseline-${RUN_ID}"

container_exists() {
  local name="$1"
  docker container inspect "${name}" >/dev/null 2>&1
}

print_logs_if_present() {
  local label="$1"
  local name="$2"
  echo "=== Logs: ${label} (${name}) ==="
  if container_exists "${name}"; then
    docker logs "${name}" 2>&1 || true
  else
    echo "(container ${name} was never created)"
  fi
}

cleanup() {
  local exit_code=$?
  # Keep all containers Stopped-not-Removed during the run so a failure at any
  # stage — including the final baseline-vs-new comparison — dumps every log.
  if [[ "${exit_code}" -ne 0 ]]; then
    echo "::error::Smoke test failed (exit ${exit_code}); dumping container logs."
    print_logs_if_present "new Python image" "${NEW_CONTAINER}"
    print_logs_if_present "baseline Python image" "${BASELINE_CONTAINER}"
    print_logs_if_present "control-plane v0.1.57" "${CP_CONTAINER}"
  fi

  docker rm -f "${NEW_CONTAINER}" "${BASELINE_CONTAINER}" "${CP_CONTAINER}" >/dev/null 2>&1 || true
  docker network rm "${NETWORK_NAME}" >/dev/null 2>&1 || true
  rm -rf "${BASELINE_DIR}" "${WORK_DIR}"
}
trap cleanup EXIT

wait_for_http_ok() {
  local label="$1"
  local url="$2"
  local timeout_secs="$3"
  local deadline=$((SECONDS + timeout_secs))

  while (( SECONDS < deadline )); do
    if curl -fsS --max-time 5 "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done

  echo "Timed out after ${timeout_secs}s waiting for ${label} at ${url}" >&2
  return 1
}

host_addr_for() {
  local container="$1"
  local port="$2"
  local mapping
  mapping="$(docker port "${container}" "${port}/tcp" | head -n 1)"
  if [[ -z "${mapping}" ]]; then
    echo "No host port mapping found for ${container}:${port}" >&2
    return 1
  fi
  echo "${mapping}"
}

wait_for_node_registration() {
  local cp_addr="$1"
  local node_id="$2"
  local out_reasoners_file="$3"
  local timeout_secs="$4"
  local deadline=$((SECONDS + timeout_secs))
  local nodes_file="${WORK_DIR}/nodes-${node_id}.json"

  # v0.1.57's GET /api/v1/nodes filters to health_status=active unless
  # show_all=true is set, whereas newly registered Python nodes report
  # health_status=healthy until the periodic monitor promotes them.
  while (( SECONDS < deadline )); do
    if curl -fsS --max-time 5 \
      -H "X-API-Key: ${API_KEY}" \
      "http://${cp_addr}/api/v1/nodes?show_all=true" \
      -o "${nodes_file}" 2>/dev/null; then
      if python3 - "${nodes_file}" "${node_id}" "${out_reasoners_file}" <<'PY'
import json
import sys

nodes_path, target_id, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(nodes_path, "r", encoding="utf-8") as fh:
    payload = json.load(fh)

for node in payload.get("nodes") or []:
    if node.get("id") != target_id:
        continue
    reasoners = node.get("reasoners") or []
    if not reasoners:
        continue
    ids = sorted(r["id"] for r in reasoners if isinstance(r, dict) and r.get("id"))
    if not ids:
        continue
    with open(out_path, "w", encoding="utf-8") as out:
        json.dump(ids, out, indent=2)
    sys.exit(0)

sys.exit(1)
PY
      then
        return 0
      fi
    fi
    sleep 1
  done

  echo "Timed out after ${timeout_secs}s waiting for ${node_id} to appear with reasoners on the control plane" >&2
  return 1
}

exercise_image() {
  local label="$1"
  local image_tag="$2"
  local container_name="$3"
  # Give each image its own NODE_ID so waiting for baseline registration cannot
  # match a leftover database record from the candidate run that preceded it.
  local node_id="swe-planner-${label}"
  local reasoners_file="${WORK_DIR}/${label}-reasoners.json"
  local response_file="${WORK_DIR}/${label}-execute-response.json"
  local agent_addr

  echo "=== Starting ${label} image (${image_tag}) as node ${node_id} ==="
  # Pass no model credentials so the smoke check proves startup, registration,
  # config defaults, and control-plane round-trips work credential-free.
  docker run -d \
    --name "${container_name}" \
    --network "${NETWORK_NAME}" \
    --network-alias "${container_name}" \
    -p "127.0.0.1::${NODE_PORT}" \
    -e AGENTFIELD_SERVER="http://control-plane:8080" \
    -e AGENTFIELD_API_KEY="${API_KEY}" \
    -e NODE_ID="${node_id}" \
    -e PORT="${NODE_PORT}" \
    -e AGENT_CALLBACK_URL="http://${container_name}:${NODE_PORT}" \
    "${image_tag}" >/dev/null

  agent_addr="$(host_addr_for "${container_name}" "${NODE_PORT}")"
  wait_for_http_ok "${label} /health" "http://${agent_addr}/health" 60
  wait_for_node_registration "${CP_ADDR}" "${node_id}" "${reasoners_file}" 60

  echo "=== Calling ${node_id}.execute through control-plane v0.1.57 ==="
  # Calling execute with an empty plan_result exercises ExecutionConfig's
  # default runtime cascade, DAGState serialization, app.note(), and the async
  # HTTP 202 + /executions/{id}/status callback without touching a model or git.
  curl -fsS --max-time 30 \
    -X POST "http://${CP_ADDR}/api/v1/execute/${node_id}.execute" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: ${API_KEY}" \
    -d '{"input":{"plan_result":{"issues":[],"levels":[]},"repo_path":"/tmp"}}' \
    -o "${response_file}"

  python3 - "${label}" "${response_file}" <<'PY'
import json
import sys

label, response_path = sys.argv[1], sys.argv[2]
with open(response_path, "r", encoding="utf-8") as fh:
    body = json.load(fh)

expected_result = {
    "repo_path": "/tmp",
    "artifacts_dir": "",
    "prd_path": "",
    "architecture_path": "",
    "issues_dir": "",
    "original_plan_summary": "",
    "prd_summary": "",
    "architecture_summary": "",
    "all_issues": [],
    "levels": [],
    "completed_issues": [],
    "failed_issues": [],
    "skipped_issues": [],
    "in_flight_issues": [],
    "current_level": 0,
    "replan_count": 0,
    "replan_history": [],
    "max_replans": 2,
    "git_integration_branch": "",
    "git_original_branch": "",
    "git_initial_commit": "",
    "git_mode": "",
    "pending_merge_branches": [],
    "merged_branches": [],
    "unmerged_branches": [],
    "worktrees_dir": "",
    "build_id": "",
    "merge_results": [],
    "integration_test_results": [],
    "accumulated_debt": [],
    "adaptation_history": [],
    "workspace_manifest": None,
}

status = body.get("status")
if status != "succeeded":
    raise SystemExit(f"{label}: expected status 'succeeded', got {status!r}: {body}")

actual_result = body.get("result")
if actual_result != expected_result:
    raise SystemExit(
        f"{label}: execute result mismatch:\n"
        f"expected: {json.dumps(expected_result, sort_keys=True)}\n"
        f"actual:   {json.dumps(actual_result, sort_keys=True)}"
    )

print(f"{label}: execute succeeded with expected empty-DAG state.")
PY

  echo "=== Stopping ${label} container ==="
  docker stop "${container_name}" >/dev/null
}

echo "=== 1. Building new Python image from current checkout ==="
docker build -t "${NEW_IMAGE}" .

if [[ -n "${BASELINE_SHA}" ]]; then
  echo "=== 2. Building baseline Python image from ${BASELINE_SHA:0:8} ==="
  git fetch --depth=1 origin "${BASELINE_SHA}"
  mkdir -p "${BASELINE_DIR}"
  git archive "${BASELINE_SHA}" | tar -x -C "${BASELINE_DIR}"

  # Command Code 1.82.2 no longer starts against current transitive npm packages,
  # so bumping this single pin is the minimal change needed for the Oct 28
  # Dockerfile to build today while keeping all baseline Python/SDK specs intact.
  sed -i.bak 's/command-code@1\.82\.2/command-code@1.88.5/g' "${BASELINE_DIR}/Dockerfile"
  rm -f "${BASELINE_DIR}/Dockerfile.bak"
  grep -Fq 'command-code@1.88.5' "${BASELINE_DIR}/Dockerfile"
  if grep -Fq 'command-code@1.82.2' "${BASELINE_DIR}/Dockerfile"; then
    echo "Failed to replace command-code@1.82.2 in baseline Dockerfile" >&2
    exit 1
  fi

  docker build -t "${BASELINE_IMAGE}" "${BASELINE_DIR}"
fi

echo "=== 3. Starting Docker network and ${CONTROL_PLANE_IMAGE} ==="
docker network create "${NETWORK_NAME}" >/dev/null

# v0.1.57 ships embedded SQLite + BoltDB compiled with sqlite_fts5 and owns
# /data as nonroot (UID 65532); pointing AGENTFIELD_HOME at /data keeps SQLite,
# BoltDB, and DID keys on that writable path without needing Postgres.
docker run -d \
  --name "${CP_CONTAINER}" \
  --network "${NETWORK_NAME}" \
  --network-alias control-plane \
  -p "127.0.0.1::8080" \
  -w /data \
  -e AGENTFIELD_HOME=/data \
  -e AGENTFIELD_STORAGE_MODE=local \
  -e AGENTFIELD_PORT=8080 \
  -e AGENTFIELD_HTTP_ADDR=0.0.0.0:8080 \
  -e AGENTFIELD_API_KEY="${API_KEY}" \
  "${CONTROL_PLANE_IMAGE}" \
  --port=8080 \
  --storage-mode=local \
  --backend-only \
  --open=false >/dev/null

CP_ADDR="$(host_addr_for "${CP_CONTAINER}" "8080")"
wait_for_http_ok "control-plane /api/v1/health" "http://${CP_ADDR}/api/v1/health" 60

exercise_image "new" "${NEW_IMAGE}" "${NEW_CONTAINER}"
if [[ -z "${BASELINE_SHA}" ]]; then
  echo "No SMOKE_BASELINE_SHA set; the new image registered and executed against ${CONTROL_PLANE_IMAGE}."
  exit 0
fi

exercise_image "baseline" "${BASELINE_IMAGE}" "${BASELINE_CONTAINER}"

echo "=== 5. Comparing registered reasoners and execute results ==="
python3 - \
  "${WORK_DIR}/new-reasoners.json" \
  "${WORK_DIR}/baseline-reasoners.json" \
  "${WORK_DIR}/new-execute-response.json" \
  "${WORK_DIR}/baseline-execute-response.json" <<'PY'
import json
import sys

new_r_path, base_r_path, new_e_path, base_e_path = sys.argv[1:5]

with open(new_r_path, "r", encoding="utf-8") as fh:
    new_reasoners = json.load(fh)
with open(base_r_path, "r", encoding="utf-8") as fh:
    base_reasoners = json.load(fh)
with open(new_e_path, "r", encoding="utf-8") as fh:
    new_exec = json.load(fh)
with open(base_e_path, "r", encoding="utf-8") as fh:
    base_exec = json.load(fh)

new_set = set(new_reasoners)
base_set = set(base_reasoners)
added = sorted(new_set - base_set)
removed = sorted(base_set - new_set)

if added or removed:
    print(f"Reasoner diff — added in new: {added}; removed from baseline: {removed}")
    raise SystemExit(
        "Registered reasoner set changed between baseline (da78a77d) and HEAD "
        "without an accounted reasoner registration change."
    )

print(f"Both images registered the identical set of {len(new_set)} reasoners.")

if new_exec.get("status") != base_exec.get("status"):
    raise SystemExit(
        f"Execute status differed: new={new_exec.get('status')!r} vs "
        f"baseline={base_exec.get('status')!r}"
    )

if new_exec.get("result") != base_exec.get("result"):
    raise SystemExit(
        "Execute result differed between new and baseline:\n"
        f"new:      {json.dumps(new_exec.get('result'), sort_keys=True)}\n"
        f"baseline: {json.dumps(base_exec.get('result'), sort_keys=True)}"
    )

print("Cheap reasoner call produced identical results on new and baseline.")
PY
