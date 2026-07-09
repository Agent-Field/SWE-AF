// Package node is the wiring wave (T6.2): it constructs the shared *agent.Agent
// from the environment and registers every reasoner by its exact Python name so
// the Go node is byte-compatible with the Python swe-planner / swe-fast nodes.
//
// node.go owns agent construction (env -> agent.Config, mirroring app.py:51-59 /
// fast/app.py:24-31) and the cross-cutting seam wiring the orchestrators need:
// the poll-based approval client provider (orch.SetApprovalClientProvider) built
// from the SDK *client.Client, and the hax REST client resolved from the
// environment. register.go owns the per-reasoner registration.
package node

import (
	"context"
	"fmt"
	"os"
	"strings"

	"github.com/Agent-Field/agentfield/sdk/go/agent"
	"github.com/Agent-Field/agentfield/sdk/go/client"

	"github.com/Agent-Field/SWE-AF/go/internal/envelope"
	"github.com/Agent-Field/SWE-AF/go/internal/hitl"
	"github.com/Agent-Field/SWE-AF/go/internal/orch"
)

// Node bundles the constructed agent with the resolved environment config and
// the collaborators the registration wave threads into every reasoner's Deps.
type Node struct {
	// App is the SDK agent. It satisfies every role/orch/fast dependency
	// interface directly (Harness, AI, Note, Call), so the Deps built in
	// register.go point their fields at it.
	App *agent.Agent

	// NodeID is the resolved node id (NODE_ID env, or the per-binary default).
	NodeID string

	// AgentFieldServer is the control-plane base URL (AGENTFIELD_SERVER).
	AgentFieldServer string

	// Token is the control-plane bearer token (AGENTFIELD_API_KEY). Mirrors the
	// Go SDK's own client, which sends AGENTFIELD_API_KEY as Authorization:
	// Bearer (agent.go:593), so the approval client below authenticates the same
	// way the agent does.
	Token string

	// approvals is the poll-based approval client the HITL loops drive
	// (RequestApproval + WaitForApproval — the Go replacement for app.pause,
	// design §4.6). Built once from the env; the *client.Client satisfies
	// hitl.ApprovalClient. nil only when the CP base URL is unusable.
	approvals hitl.ApprovalClient

	// hax is the hax REST client, nil when HAX_API_KEY is unset (HITL disabled,
	// mirroring build_hax_client_from_env() returning None).
	hax *hitl.HaxClient

	// registered records every reasoner name passed through regHandler — the
	// single registration path — so the parity test can assert the exact
	// surface. RegisterReasoner is a pure insert keyed by name, so this slice
	// equals the agent's reasoner set (the test also guards against duplicates).
	registered []string
}

// RegisteredNames returns a copy of the reasoner names registered on this node,
// in registration order. Used by the parity test to assert the surface exactly
// matches the Python node.
func (n *Node) RegisteredNames() []string {
	return append([]string(nil), n.registered...)
}

// BuildAgent constructs the SWE-AF agent from the environment exactly as the
// Python entry points do (app.py:51-59 / fast/app.py:24-31):
//
//   - NODE_ID           default defaultNodeID ("swe-planner" / "swe-fast")
//   - AGENTFIELD_SERVER default "http://localhost:8080"
//   - AGENTFIELD_API_KEY -> Config.Token (bearer)
//   - PORT              default defaultPort ("8003" / "8004") -> ListenAddress
//   - AGENT_CALLBACK_URL -> Config.PublicURL — the base URL the node registers
//     with the control plane. The Python SDK reads the same env var
//     (agent_server.py:758) before defaulting to localhost; without it a
//     containerized node registers http://localhost:<port> and the CP cannot
//     route execute calls back to it (found by the T7.2 functional tests).
//     Unset -> the SDK's localhost default, matching Python.
//   - Version           "1.0.0"
//   - description       the per-node description string
//
// It also builds the SDK approval client and wires the orchestrator approval
// seam (orch.SetApprovalClientProvider) so the plan-approval gate can pause via
// the control plane. Register the reasoners with RegisterPlanner / RegisterFast.
func BuildAgent(defaultNodeID, defaultPort, description string) (*Node, error) {
	nodeID := envOr("NODE_ID", defaultNodeID)
	server := envOr("AGENTFIELD_SERVER", "http://localhost:8080")
	token := os.Getenv("AGENTFIELD_API_KEY")
	port := envOr("PORT", defaultPort)

	cfg := agent.Config{
		NodeID:        nodeID,
		Version:       "1.0.0",
		AgentFieldURL: server,
		Token:         token,
		ListenAddress: ":" + port,
		// PublicURL: the callback base URL the CP uses to reach this node.
		// AGENT_CALLBACK_URL mirrors the Python SDK's env var of the same name
		// (agent_server.py:758); when unset the SDK defaults to
		// http://localhost:<port>, which is correct only outside containers.
		PublicURL: os.Getenv("AGENT_CALLBACK_URL"),
		// The Go SDK sends no node-level description in its registration payload
		// (unlike the Python Agent(description=...)); AppDescription is the CLI
		// help string, the closest home for the Python description. It does not
		// enable CLI mode (no reasoner sets WithCLI), so Run still serves.
		CLIConfig: &agent.CLIConfig{AppDescription: description},
	}

	app, err := agent.New(cfg)
	if err != nil {
		return nil, fmt.Errorf("create agent %q: %w", nodeID, err)
	}

	n := &Node{
		App:              app,
		NodeID:           nodeID,
		AgentFieldServer: server,
		Token:            token,
		hax:              hitl.BuildHaxClientFromEnv(),
	}

	// Build the poll-based approval client (best-effort: a bad base URL leaves
	// approvals nil, and the approval gate then no-ops rather than failing the
	// build — the same graceful-degradation the Python gate uses when the pause
	// substrate is unavailable).
	if c, cerr := client.New(server, client.WithBearerToken(token)); cerr == nil {
		n.approvals = c
	}

	// Wire the orchestrator approval seam once: the plan-approval gate resolves
	// its pause client through this provider. The request-scoped ApprovalRequest
	// is ignored — a single process-wide SDK client serves every execution.
	orch.SetApprovalClientProvider(func(orch.ApprovalRequest) hitl.ApprovalClient {
		return n.approvals
	})

	return n, nil
}

// newCallFn returns the app.Call + envelope-unwrap closure injected into the
// coding loop, the DAG executor and the fast pipeline. It is structurally
// identical to coding.CallFn and fast.CallFn (both func(ctx, target, kwargs)
// (map[string]any, error)), so a single closure satisfies both. The unwrap
// label is the reasoner segment after the final dot — matching orch.NewCallFn.
func newCallFn(app *agent.Agent) func(context.Context, string, map[string]any) (map[string]any, error) {
	return func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
		raw, err := app.Call(ctx, target, kwargs)
		if err != nil {
			return nil, err
		}
		label := target
		if i := strings.LastIndex(target, "."); i >= 0 {
			label = target[i+1:]
		}
		return envelope.UnwrapCallResult(raw, label)
	}
}

// envOr returns the value of key, or def when the env var is unset or empty.
func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
