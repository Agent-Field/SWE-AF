// Command swe-planner is the full-pipeline SWE-AF node (Python __main__.py /
// app.py). It builds the agent from the environment and registers the full
// swe-planner surface — 5 orchestrators + 25 role reasoners — then serves until
// SIGINT/SIGTERM. agent.Run installs its own signal handling, so main passes a
// plain context.Background() and does not double-handle signals.
package main

import (
	"context"
	"log"

	"github.com/Agent-Field/SWE-AF/go/internal/node"
)

func main() {
	// Defaults mirror app.py:51-59: NODE_ID "swe-planner", PORT 8003.
	n, err := node.BuildAgent("swe-planner", "8003", "Autonomous SWE planning pipeline")
	if err != nil {
		log.Fatalf("swe-planner: build agent: %v", err)
	}

	n.RegisterPlanner()

	if err := n.App.Run(context.Background()); err != nil {
		log.Fatalf("swe-planner: run: %v", err)
	}
}
