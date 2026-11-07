package node

import (
	"strings"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/furrow"
)

// Every reasoner a person can see as a step carries a short display label,
// and none of them reads like machinery.
func TestEveryReasonerHasADisplayLabel(t *testing.T) {
	t.Setenv("SWE_PRO_ENGINE", "")
	t.Setenv(furrow.EnvEnabled, "")
	t.Setenv(furrow.EnvPublicAddr, "")
	n, err := BuildAgent("swe-planner", "8005", "Autonomous SWE planning pipeline")
	if err != nil {
		t.Fatal(err)
	}
	n.RegisterPlanner()
	for _, name := range n.RegisteredNames() {
		l, ok := DisplayLabels[name]
		if !ok {
			t.Errorf("%s has no display label", name)
			continue
		}
		if strings.HasPrefix(strings.ToLower(l.Label), "run ") || strings.ContainsAny(l.Label, "_-") || len(l.Label) > 20 {
			t.Errorf("%s label %q reads badly", name, l.Label)
		}
		found := false
		for _, tag := range n.meta[name].Tags {
			if tag == LabelTagPrefix+l.Label {
				found = true
			}
		}
		if !found {
			t.Errorf("%s is registered without its label tag (tags %v)", name, n.meta[name].Tags)
		}
	}
}
