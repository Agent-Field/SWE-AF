// Command swe-planner is the full-pipeline SWE-AF node (Python __main__.py /
// app.py). It builds the agent from the environment and registers the full
// swe-planner surface — 5 orchestrators + 25 role reasoners — then serves until
// SIGINT/SIGTERM. agent.Run installs its own signal handling, so main passes a
// plain context; the cancellable wrapper below exists only to stop the
// pro-engine sidecar when Run returns.
package main

import (
	"context"
	"log"
	"time"

	"github.com/Agent-Field/SWE-AF/go/internal/node"
	"github.com/Agent-Field/SWE-AF/go/internal/pro"
)

func main() {
	// Defaults: NODE_ID "swe-planner", PORT 8005. This is the SWE node, so it
	// registers under the product's name rather than a port-specific variant —
	// callers' triggers do not change when a node moves from Python to Go. To
	// run this alongside the Python node against one control plane, give it a
	// distinct NODE_ID; the compose files do exactly that. NODE_ID / PORT env
	// vars override both defaults.
	n, err := node.BuildAgent("swe-planner", "8005", "Autonomous SWE planning pipeline")
	if err != nil {
		log.Fatalf("swe-planner: build agent: %v", err)
	}

	n.RegisterPlanner()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Pro-engine sidecar: inert unless SWE_PRO_ENGINE is truthy, which the
	// af-install manifest defaults to "1" and SWE_PRO_ENGINE=0 turns off. The
	// sidecar registers its own node on the same control plane; a missing
	// binary logs a warning and the planner comes up as usual.
	var sup *pro.Supervisor
	if pro.Enabled() {
		sup = pro.Start(ctx, pro.Options{Server: n.AgentFieldServer, Token: n.Token})
	}

	runErr := n.App.Run(ctx)
	cancel()
	sup.Wait(10 * time.Second) // nil-safe; bounded wind-down for the sidecar
	if runErr != nil {
		log.Fatalf("swe-planner: run: %v", runErr)
	}
}
