package schemas

// FieldType is the widget kind for one AskUserFormField. Ported verbatim from
// hitl/ask_user.py::FieldType (a Literal[...]); kept as a typed string so the
// allowed values live next to the type.
type FieldType string

const (
	FieldTypeInput         FieldType = "input"          // single-line text
	FieldTypeNumber        FieldType = "number"         // numeric
	FieldTypeTextarea      FieldType = "textarea"       // multi-line text
	FieldTypeSelect        FieldType = "select"         // requires options
	FieldTypeRadio         FieldType = "radio"          // requires options
	FieldTypeCheckbox      FieldType = "checkbox"       // boolean
	FieldTypeCheckboxGroup FieldType = "checkbox_group" // requires options
	FieldTypeSwitch        FieldType = "switch"         // boolean
	FieldTypeSlider        FieldType = "slider"         // numeric, requires min/max
	FieldTypeDate          FieldType = "date"           // ISO date
)

// AskUserFormField is one field in a form the agent is constructing for the
// user. Ported from hitl/ask_user.py::AskUserFormField.
//
// description, placeholder, min, max and step are Optional in Pydantic
// (default None) so map to pointers; default_value is Any|None so maps to any;
// options is list[dict[str,str]]|None so maps to a nil-able slice.
type AskUserFormField struct {
	ID           string              `json:"id"`
	Type         FieldType           `json:"type"`
	Label        string              `json:"label"`
	Description  *string             `json:"description"`
	Required     bool                `json:"required"`
	Placeholder  *string             `json:"placeholder"`
	DefaultValue any                 `json:"default_value"`
	Options      []map[string]string `json:"options"`
	Min          *float64            `json:"min"`
	Max          *float64            `json:"max"`
	Step         *float64            `json:"step"`
}

// AskUserForm is a form the agent wants the user to fill out before continuing.
// Ported from hitl/ask_user.py::AskUserForm.
//
// SubmitLabel has a non-zero Pydantic default ("Submit") — seeded in
// defaults.go via UnmarshalJSON.
type AskUserForm struct {
	Title       string             `json:"title"`
	Description *string            `json:"description"`
	Fields      []AskUserFormField `json:"fields"`
	SubmitLabel string             `json:"submit_label"`
}

// AskUserResponse is what the wrapper hands back to the reasoner on
// re-invocation. Ported from hitl/ask_user.py::AskUserResponse.
//
// Status is a Literal in Pydantic ("submitted"|"timeout"|"cancelled"|"error")
// but per the port strategy pseudo-literal strings stay a plain string.
type AskUserResponse struct {
	Status   string         `json:"status"`
	Values   map[string]any `json:"values"`
	Feedback *string        `json:"feedback"`
	Error    *string        `json:"error"`
}
