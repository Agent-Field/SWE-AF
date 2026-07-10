package schemas

import "reflect"

// EmptyForNilSlices walks v in place, replacing every nil slice it encounters
// with a non-nil empty slice of the same element type. Maps (nil or non-nil)
// and nil pointers are left UNTOUCHED.
//
// Why this exact shape:
//
//   - Python's pydantic rejects a present-but-null value for a list field
//     (list[...] with a default_factory=list): every DAGState list field, every
//     role-result list field, etc. must serialise as [] not null, or a pydantic
//     model that re-parses the JSON (the replanner context, a checkpoint loaded
//     by the Python executor, the SDK's schema validation of a harness output)
//     raises a ValidationError.
//   - dict|None fields legitimately accept null in pydantic — DAGState's
//     workspace_manifest, WorkspaceRepo.git_init_result, BuildResult.verification.
//     A blanket nil-map→{} would corrupt those (Python wants null), so maps are
//     never touched.
//   - Optional[...] fields (mapped to Go pointers: IssueResult.split_request,
//     CoderResult.tests_passed, ask_user_form) are `X | None = None` in pydantic
//     and serialise as null when unset; nil pointers are therefore left as null.
//
// v MUST be a non-nil pointer for the replacement to take effect — reflection
// can only Set through an addressable value. Callers pass &value.
func EmptyForNilSlices(v any) {
	if v == nil {
		return
	}
	walkNilSlices(reflect.ValueOf(v))
}

// walkNilSlices recurses structs, slices and pointers, replacing nil slices with
// empty ones. It deliberately does not recurse into maps (nor replace nil maps),
// leaving pydantic's dict|None fields as null. Unexported / unaddressable fields
// are skipped (CanSet guard) rather than panicking.
func walkNilSlices(rv reflect.Value) {
	switch rv.Kind() {
	case reflect.Pointer:
		if !rv.IsNil() {
			walkNilSlices(rv.Elem())
		}
	case reflect.Interface:
		// The concrete value inside an interface is not addressable, so a nil
		// slice reached only through an interface cannot be replaced; the recursion
		// is still safe (the CanSet guards below no-op). Non-interface paths cover
		// every concrete schema field.
		if !rv.IsNil() {
			walkNilSlices(rv.Elem())
		}
	case reflect.Struct:
		for i := 0; i < rv.NumField(); i++ {
			f := rv.Field(i)
			if f.CanSet() {
				walkNilSlices(f)
			}
		}
	case reflect.Slice:
		if rv.IsNil() {
			if rv.CanSet() {
				rv.Set(reflect.MakeSlice(rv.Type(), 0, 0))
			}
			return
		}
		for i := 0; i < rv.Len(); i++ {
			walkNilSlices(rv.Index(i))
		}
	}
	// reflect.Map: intentionally left untouched (see doc comment).
}
