# SWE-AF Go port — build/test targets. Run from the go/ directory.
# Mirrors the acceptance gate used by every port task and CI (design §10):
#   go build ./... && go vet ./... && go test ./...

.PHONY: build vet test check lint run-planner run-fast docker-build docker-up docker-down

# Compile every package in the module.
build:
	go build ./...

# Static analysis (go vet) across the module.
vet:
	go vet ./...

# Unit tests across the module.
test:
	go test ./...

# The full local gate CI runs: vet + tests.
check: vet test

# Optional lint pass; no-op with a hint when golangci-lint is not installed.
lint:
	@command -v golangci-lint >/dev/null 2>&1 && golangci-lint run || \
		echo "golangci-lint not installed; skipping (install: https://golangci-lint.run)"

# Run the full-pipeline node (swe-planner-go, default port 8005).
run-planner:
	go run ./cmd/swe-planner

# Run the fast-mode node (swe-fast-go, default port 8006).
run-fast:
	go run ./cmd/swe-fast

# --- Docker (multi-stage image + Go-node compose stack) -------------------
# These operate on the repo root (one level up) because the Dockerfile build
# context is the SWE-AF root (so the go/ module is in context) and the compose
# file (docker-compose.go.yml) lives there too. The image tag and the pinned
# AgentField SDK ref are overridable:
#   make docker-build IMAGE=myrepo/swe-af-go:dev AGENTFIELD_SDK_REF=<sha>
IMAGE ?= swe-af-go:latest
AGENTFIELD_SDK_REF ?= a99510564f31ea11e8cc24bcbc983acff578a700

# Build the multi-stage Go image from the repo root.
docker-build:
	docker build -f Dockerfile \
		--build-arg AGENTFIELD_SDK_REF=$(AGENTFIELD_SDK_REF) \
		-t $(IMAGE) ..

# Bring up the Go nodes (swe-agent-go:8005 + swe-fast-go:8006) as an ADD-ON to
# the Python stack. Start the Python stack first (`docker compose up` — it owns
# the control plane + shared network); this add-on joins that network as an
# external reference.
docker-up:
	docker compose -f ../docker-compose.go.yml up --build

# Tear the Go-node stack down.
docker-down:
	docker compose -f ../docker-compose.go.yml down
