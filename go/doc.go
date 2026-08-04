// Package swe is the module root marker for the SWE-AF Go port.
//
// The port turns a natural-language goal into a verified codebase and a draft
// PR by orchestrating specialized agent roles across a dependency-sorted Issue
// DAG, mirroring the Python swe_af package 1:1 (same reasoner names, same
// control-plane routed call graph, same HTTP API shapes).
//
// Layout (design §1.3):
//
//	cmd/swe-planner   full-pipeline node entry point (node id "swe-planner")
//	cmd/swe-fast      fast-mode node entry point   (node id "swe-fast")
//	internal/afx      small AgentField SDK ergonomics (input binding, notes)
//	internal/...      schemas, config, prompts, roles, execution engine, etc.
//
// It depends on the AgentField Go SDK
// (github.com/Agent-Field/agentfield/sdk/go), which is consumed via a
// workspace in dev and a replace directive in CI/Docker (design §1.2).
package swe
