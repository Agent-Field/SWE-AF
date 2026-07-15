module github.com/Agent-Field/SWE-AF/go

// Match the AgentField Go SDK's go directive (sdk/go/go.mod: go 1.21) so the
// two modules resolve identically under the dev workspace and in CI/Docker.
go 1.21

require (
	github.com/Agent-Field/agentfield/sdk/go v0.0.0-20260714191100-2cc5fe2adcf4
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
