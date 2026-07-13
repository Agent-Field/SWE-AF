module github.com/Agent-Field/SWE-AF/go

// Match the AgentField Go SDK's go directive (sdk/go/go.mod: go 1.21) so the
// two modules resolve identically under the dev workspace and in CI/Docker.
go 1.21

require (
	github.com/Agent-Field/agentfield/sdk/go v0.0.0-00010101000000-000000000000
	github.com/invopop/jsonschema v0.13.0
	golang.org/x/sync v0.11.0
)

require (
	github.com/bahlo/generic-list-go v0.2.0 // indirect
	github.com/buger/jsonparser v1.1.1 // indirect
	github.com/mailru/easyjson v0.7.7 // indirect
	github.com/santhosh-tekuri/jsonschema/v5 v5.3.1 // indirect
	github.com/wk8/go-ordered-map/v2 v2.1.8 // indirect
	gopkg.in/yaml.v3 v3.0.1 // indirect
)

// No sdk/go/vX.Y.Z submodule tags exist, so a normal versioned require is
// impossible. Dev uses the go.work workspace; CI/Docker place the agentfield
// repo as a sibling checkout at a pinned SHA and rely on this replace
// (design §1.2). Path is two levels up from SWE-AF/go: ../.. -> the shared parent of both checkouts.
replace github.com/Agent-Field/agentfield/sdk/go => ../../agentfield/sdk/go
