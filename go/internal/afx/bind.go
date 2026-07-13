// Package afx holds small ergonomics over the AgentField Go SDK that every
// reasoner handler in the port reuses.
package afx

import (
	"encoding/json"
	"fmt"
)

// Bind decodes a reasoner's untyped input map into a typed value T.
//
// Handlers registered with the SDK receive input as map[string]any. Bind
// round-trips that map through JSON (marshal then unmarshal into T), which
// mirrors how the Python port materializes a Pydantic model from the request
// body: field-name matching is by the json struct tags (the exact snake_case
// pydantic field names), and any custom UnmarshalJSON on T runs — so a T whose
// UnmarshalJSON seeds non-zero Pydantic defaults gets those defaults for keys
// absent from input (design §2.2, §8).
//
// Plain encoding/json is deliberate: no json.Decoder/UseNumber. Numbers in the
// input map are already Go float64/int (they came from the SDK's own JSON
// decode or from a Go caller), and re-marshaling then unmarshaling into the
// typed fields of T yields the correct concrete types without number-precision
// gymnastics (design §8).
func Bind[T any](input map[string]any) (T, error) {
	var out T
	b, err := json.Marshal(input)
	if err != nil {
		return out, fmt.Errorf("afx.Bind: marshal input: %w", err)
	}
	if err := json.Unmarshal(b, &out); err != nil {
		return out, fmt.Errorf("afx.Bind: unmarshal into %T: %w", out, err)
	}
	return out, nil
}
