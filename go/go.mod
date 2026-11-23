module github.com/Agent-Field/SWE-AF/go

// Match the AgentField Go SDK's go directive (sdk/go/go.mod: go 1.21) so the
// two modules resolve identically under the dev workspace and in CI/Docker.
go 1.21

require (
	github.com/Agent-Field/agentfield/sdk/go v0.1.146-rc.25
	github.com/invopop/jsonschema v0.13.0
	golang.org/x/sync v0.11.0
)

require (
	github.com/bahlo/generic-list-go v0.2.0 // indirect
	github.com/buger/jsonparser v1.1.1 // indirect
	github.com/gorilla/websocket v1.5.3 // indirect
	github.com/mailru/easyjson v0.7.7 // indirect
	github.com/santhosh-tekuri/jsonschema/v5 v5.3.1 // indirect
	github.com/wk8/go-ordered-map/v2 v2.1.8 // indirect
	gopkg.in/yaml.v3 v3.0.1 // indirect
)

// The SDK now publishes sdk/go/vX.Y.Z submodule tags, so the require above is
// a real version. go/Dockerfile and .github/workflows/ci.yml pin the same
// release via AGENTFIELD_SDK_REF (sdk/go/v0.1.146-rc.25) — bump them together.
// Dev can still layer a local checkout on top with the go.work workspace;
// nothing here depends on a sibling checkout, which is what makes
// `af install …//go` work.
