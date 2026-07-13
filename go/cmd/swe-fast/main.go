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
	// Defaults: NODE_ID "swe-fast-go", PORT 8006 — a distinct identity from the
	// Python swe-fast node (fast/app.py:24-31) so the Go port runs as an opt-in
	// sibling alongside Python against one control plane. NODE_ID / PORT env
	// vars still override.
	n, err := node.BuildAgent(
		"swe-fast-go",
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
