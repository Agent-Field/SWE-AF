#!/usr/bin/env bash
set -euo pipefail

# Fail fast when HARNESS_MODEL references a provider that isn't defined in
# opencode.json. Without this check, opencode's subprocess waits out the full
# runner timeout (default 300s) per call before failing, which makes the whole
# SWE-AF pipeline appear to "hang at git_init" (2026-08-04 incident:
# HARNESS_MODEL=omni/codex-terra while omni was not a configured provider).
if [[ -n "${HARNESS_MODEL:-}" && "${HARNESS_MODEL}" == */* ]]; then
    provider="${HARNESS_MODEL%%/*}"
    config="/root/.config/opencode/opencode.json"
    if [[ -f "${config}" ]] && ! grep -q "\"${provider}\"" "${config}"; then
        echo "ERROR: HARNESS_MODEL='${HARNESS_MODEL}' references provider '${provider}'" >&2
        echo "       which is not defined in ${config}." >&2
        echo "       Set HARNESS_MODEL to a provider/model defined there, e.g." >&2
        echo "       9router/claude-sonnet-4-6  (9Router backend)" >&2
        echo "       openrouter/<model>          (OpenRouter)" >&2
        exit 2
    fi
fi

exec "$@"
