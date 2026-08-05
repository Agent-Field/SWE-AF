// Command swe-fast is the fast-mode SWE-AF node (Python fast/__main__.py /
// fast/app.py). It builds the agent from the environment and registers the
// swe-fast surface — 4 fast reasoners + the same 25 role reasoners the full
// node exposes — then serves until SIGINT/SIGTERM. agent.Run installs its own
// signal handling, so main passes a plain context.Background().
package main

import (
	"context"
	"log"

	"github.com/Agent-Field/SWE-AF/go/internal/node"
)

func main() {
	// Defaults: NODE_ID "swe-fast", PORT 8006 — the product's name, matching
	// cmd/swe-planner, so callers' triggers survive the move from Python to Go.
	// To run this alongside the Python node against one control plane, give it
	// a distinct NODE_ID; the compose files do exactly that. NODE_ID / PORT env
	// vars override both defaults.
	n, err := node.BuildAgent(
		"swe-fast",
		"8006",
		"Speed-optimized SWE agent — single-pass planning, sequential execution",
	)
	if err != nil {
		log.Fatalf("swe-fast: build agent: %v", err)
	}

	n.RegisterFast()

	if err := n.App.Run(context.Background()); err != nil {
		log.Fatalf("swe-fast: run: %v", err)
	}
}
