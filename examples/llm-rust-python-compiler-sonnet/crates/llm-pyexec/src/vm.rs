//! RustPython VM lifecycle for the llm-pyexec library.
//!
//! This module owns all RustPython API calls. It:
//! - Creates a fresh interpreter per execution with stdlib, import hook, and output capture.
//! - Compiles and executes Python source, returning a [`VmRunResult`].
//! - Extracts structured errors (SyntaxError, RuntimeError, ModuleNotAllowed).
//! - Extracts the `__result__` return value from the scope after execution.
//!
//! ## Import Hook (Option C: `builtins.__import__` override)
//!
//! Architecture §17 lists three options for intercepting imports:
//!   Option A: `vm.import_func` mutable slot
//!   Option B: `sys.meta_path` prepend
//!   Option C: `builtins.__import__` override
//!
//! RustPython 0.3 resolves imports by calling `builtins.__import__` (see
//! `src/vm/mod.rs` `import()` method). The init closure in `Interpreter::with_init`
//! runs BEFORE `vm.initialize()`, so `builtins.__import__` is not yet set. We
//! therefore install the import hook at the beginning of `run_code` (inside
//! `interp.enter()`), which runs after full initialization. This is Option C.
//!
//! ## Output Capture
//!
//! We replace `sys.stdout` and `sys.stderr` with minimal Python-level objects
//! whose `write(s)` method delegates to [`OutputBuffer::write_stdout`] /
//! [`OutputBuffer::write_stderr`]. The replacement also happens at the start of
//! each `run_code` call (inside `enter()`).
//!
//! ## Zero unsafe blocks (AC-23)
//!
//! This file contains no `unsafe` code. All RustPython integration uses the safe
//! public Rust API.

use std::collections::HashSet;
use std::sync::{Arc, Mutex};

use rustpython_vm::{
    builtins::PyBaseExceptionRef,
    compiler::Mode,
    function::FuncArgs,
    scope::Scope,
    AsObject, Interpreter, PyObjectRef, PyResult, VirtualMachine,
};

use crate::modules::check_module_allowed;
use crate::output::OutputBuffer;
use crate::types::ExecutionError;

// ── Public (crate-visible) types ─────────────────────────────────────────────

/// Internal result of running code in the VM.
/// This is an intermediate representation before constructing [`ExecutionResult`].
pub(crate) struct VmRunResult {
    pub stdout: String,
    pub stderr: String,
    pub return_value: Option<String>,
    pub error: Option<ExecutionError>,
}

/// A configured interpreter bundled with its module allowlist.
///
/// This wrapper holds both the RustPython `Interpreter` and the `allowed_set`
/// that governs which Python modules can be imported. It is created by
/// [`build_interpreter`] and passed (by reference) to [`run_code`].
pub(crate) struct PyInterp {
    inner: Interpreter,
    allowed_set: Arc<HashSet<String>>,
}

impl PyInterp {
    /// Replace the allowed-module set for this interpreter.
    ///
    /// Called by the pool slot thread before each `run_code()` call when the
    /// caller provides a custom allowlist that differs from the pool default.
    /// The new allowlist is reflected in the next `run_code()` call's import hook,
    /// because `install_import_hook()` re-reads `interp.allowed_set` each time.
    #[allow(dead_code)]
    pub(crate) fn set_allowed_set(&mut self, allowed_set: HashSet<String>) {
        self.allowed_set = Arc::new(allowed_set);
    }

    /// Execute a closure with access to the VirtualMachine.
    ///
    /// Used by pool.rs for sys.modules inspection and reset.
    /// The closure must not store any references to the VM outside its scope.
    #[allow(dead_code)]
    pub(crate) fn with_vm<F, R>(&self, f: F) -> R
    where
        F: FnOnce(&VirtualMachine) -> R,
    {
        self.inner.enter(f)
    }
}

// ── Public API ────────────────────────────────────────────────────────────────

/// Return candidate filesystem paths for a Python standard library installation.
///
/// RustPython can use pure-Python stdlib modules (json, collections, re, etc.)
/// from a host Python installation when they are added to `Settings::path_list`.
/// Native extension modules provided by `rustpython_stdlib::get_module_inits()`
/// take precedence over any .so files on the same path.
fn python_stdlib_paths() -> Vec<String> {
    // Common locations for Python 3.x stdlib on Linux.
    // We check multiple versions to be resilient across environments.
    let candidates = [
        "/usr/local/lib/python3.13",
        "/usr/local/lib/python3.12",
        "/usr/local/lib/python3.11",
        "/usr/local/lib/python3.10",
        "/usr/local/lib/python3.9",
        "/usr/lib/python3",
        "/usr/lib/python3.12",
        "/usr/lib/python3.11",
        "/usr/lib/python3.10",
    ];
    candidates
        .iter()
        .filter(|p| std::path::Path::new(p).is_dir())
        .map(|p| p.to_string())
        .collect()
}

/// Create a new RustPython interpreter with stdlib configured.
///
/// The import hook and output capture are installed at the beginning of each
/// `run_code` call (inside `enter()`), because `builtins.__import__` is only
/// available after `vm.initialize()` which runs AFTER the `with_init` closure.
///
/// # Parameters
/// - `allowed_set`: allowlisted module names, used to gate import calls
/// - `output`: shared buffer (not used here; passed through to run_code)
///
/// # Returns
/// A configured [`PyInterp`] ready for [`run_code`].
pub(crate) fn build_interpreter(
    allowed_set: HashSet<String>,
    _output: OutputBuffer,
) -> PyInterp {
    let mut settings = rustpython_vm::Settings::default();

    // Add the Python stdlib path so that pure-Python stdlib modules (json,
    // collections, re, datetime, etc.) are importable.
    //
    // The `freeze-stdlib` feature in rustpython-vm 0.3 only freezes the VM's
    // own core modules (python_builtins, core_modules), not the full Python
    // standard library. The full stdlib is available via the host Python
    // installation at /usr/local/lib/python3.12 (fallback: /usr/lib/python3).
    //
    // We add these paths so RustPython can find and run pure-Python modules.
    // The native C-extension modules (e.g. _json, math, re) are provided by
    // rustpython_stdlib::get_module_inits() and shadow any CPython .so files.
    for path in python_stdlib_paths() {
        settings.path_list.push(path);
    }

    let inner = Interpreter::with_init(settings, move |vm| {
        // ── Register stdlib modules ────────────────────────────────────────
        // This registers native (Rust-implemented) stdlib modules:
        // _json, math, _csv, unicodedata, zlib, etc.
        // These shadow any C extension .so files that might be on sys.path.
        vm.add_native_modules(rustpython_stdlib::get_module_inits());

        // ── Register minimal frozen stdlib wrappers ────────────────────────
        // Freeze a minimal Python-level json module compatible with RustPython.
        // The native _json module is registered above; this Python wrapper
        // provides the user-facing json.dumps/json.loads/etc. interface.
        vm.add_frozen(rustpython_vm::py_freeze!(
            source = r#"
import _json

class JSONDecodeError(ValueError):
    def __init__(self, msg, doc, pos):
        errmsg = '%s: line %d column %d (char %d)' % (
            msg,
            doc.count('\n', 0, pos) + 1,
            pos - doc.rfind('\n', 0, pos),
            pos,
        )
        ValueError.__init__(self, errmsg)
        self.msg = msg
        self.doc = doc
        self.pos = pos
        self.lineno = doc.count('\n', 0, pos) + 1
        self.colno = pos - doc.rfind('\n', 0, pos)

class JSONEncoder:
    def __init__(self, skipkeys=False, ensure_ascii=True,
                 check_circular=True, allow_nan=True, sort_keys=False,
                 indent=None, separators=None, default=None):
        self.skipkeys = skipkeys
        self.ensure_ascii = ensure_ascii
        self.check_circular = check_circular
        self.allow_nan = allow_nan
        self.sort_keys = sort_keys
        self.indent = indent
        if separators is not None:
            self.item_separator, self.key_separator = separators
        elif indent is not None:
            self.item_separator = ','
            self.key_separator = ': '
        else:
            self.item_separator = ', '
            self.key_separator = ': '
        self.default = default if default is not None else self._default

    def _default(self, obj):
        raise TypeError(f'Object of type {type(obj).__name__} is not JSON serializable')

    def encode(self, o):
        return _json.encode_basestring_ascii(str(o)) if False else _simple_encode(o, self)

    def iterencode(self, o, _one_shot=False):
        return iter([self.encode(o)])

def _simple_encode(obj, encoder):
    if obj is None:
        return 'null'
    elif obj is True:
        return 'true'
    elif obj is False:
        return 'false'
    elif isinstance(obj, int):
        return str(obj)
    elif isinstance(obj, float):
        if obj != obj:
            return 'NaN'
        elif obj == float('inf'):
            return 'Infinity'
        elif obj == float('-inf'):
            return '-Infinity'
        return repr(obj)
    elif isinstance(obj, str):
        return _encode_str(obj)
    elif isinstance(obj, (list, tuple)):
        if not obj:
            return '[]'
        items = [_simple_encode(v, encoder) for v in obj]
        return '[' + ', '.join(items) + ']'
    elif isinstance(obj, dict):
        if not obj:
            return '{}'
        keys = sorted(obj.keys()) if encoder.sort_keys else obj.keys()
        items = [_encode_str(str(k)) + ': ' + _simple_encode(v, encoder) for k, v in ((k, obj[k]) for k in keys)]
        return '{' + ', '.join(items) + '}'
    else:
        return encoder.default(obj)

def _encode_str(s):
    result = ['"']
    for c in s:
        if c == '"':
            result.append('\\"')
        elif c == '\\':
            result.append('\\\\')
        elif c == '\n':
            result.append('\\n')
        elif c == '\r':
            result.append('\\r')
        elif c == '\t':
            result.append('\\t')
        elif ord(c) < 0x20:
            result.append('\\u{:04x}'.format(ord(c)))
        else:
            result.append(c)
    result.append('"')
    return ''.join(result)

def dumps(obj, *, skipkeys=False, ensure_ascii=True, check_circular=True,
          allow_nan=True, cls=None, indent=None, separators=None, default=None,
          sort_keys=False, **kw):
    encoder = (cls or JSONEncoder)(
        skipkeys=skipkeys, ensure_ascii=ensure_ascii,
        check_circular=check_circular, allow_nan=allow_nan,
        indent=indent, separators=separators, default=default,
        sort_keys=sort_keys, **kw
    )
    return encoder.encode(obj)

def dump(obj, fp, **kwargs):
    fp.write(dumps(obj, **kwargs))

def loads(s, *, cls=None, object_hook=None, parse_float=None,
          parse_int=None, parse_constant=None, object_pairs_hook=None, **kw):
    if isinstance(s, (bytes, bytearray)):
        s = s.decode('utf-8')
    decoder = JSONDecoder(object_hook=object_hook, object_pairs_hook=object_pairs_hook,
                          parse_float=parse_float, parse_int=parse_int, strict=True)
    return decoder.decode(s)

def load(fp, **kwargs):
    return loads(fp.read(), **kwargs)

class JSONDecoder:
    def __init__(self, *, object_hook=None, parse_float=None, parse_int=None,
                 parse_constant=None, strict=True, object_pairs_hook=None):
        self.object_hook = object_hook
        self.object_pairs_hook = object_pairs_hook
        self.parse_float = parse_float or float
        self.parse_int = parse_int or int
        self.strict = strict
        self.scan_once = _json.make_scanner(self)

    def decode(self, s, _w=None):
        obj, end = self.raw_decode(s, 0)
        end = len(s.lstrip()) if not s else end
        return obj

    def raw_decode(self, s, idx=0):
        try:
            obj, end = self.scan_once(s, idx)
        except StopIteration as err:
            raise JSONDecodeError("Expecting value", s, err.value) from None
        return obj, end
"#,
            module_name = "json"
        ));
    });

    PyInterp {
        inner,
        allowed_set: Arc::new(allowed_set),
    }
}

/// Execute Python source code in the VM.
///
/// Installs the import allowlist hook and output capture at the start of each
/// call (inside `enter()`), then compiles and runs the code.
///
/// # Parameters
/// - `interp`: a configured interpreter (from [`build_interpreter`])
/// - `code_str`: the Python source to compile and execute
/// - `output`: shared buffer for capturing stdout/stderr and reading them back
///
/// # Returns
/// [`VmRunResult`] with captured output and any error.
pub(crate) fn run_code(interp: &PyInterp, code_str: &str, output: OutputBuffer) -> VmRunResult {
    let allowed_set = Arc::clone(&interp.allowed_set);

    interp.inner.enter(|vm| {
        // ── Step 0: Install import hook and output capture ────────────────
        // These are idempotent: each call to run_code reinstalls them so each
        // execution starts with a clean hook state.
        install_import_hook(vm, &allowed_set);
        install_output_capture(vm, output.clone());

        // ── Step 1: Compile ───────────────────────────────────────────────
        // Catches SyntaxError before any execution.
        let code = match vm.compile(code_str, Mode::Exec, "<string>".to_owned()) {
            Ok(c) => c,
            Err(e) => {
                let (stdout, stderr) = output.into_strings();
                return VmRunResult {
                    stdout,
                    stderr,
                    return_value: None,
                    error: Some(extract_syntax_error(e)),
                };
            }
        };

        // ── Step 2: Execute in an isolated scope ──────────────────────────
        // Set __name__ = "__main__" so the import hook can distinguish user
        // code (which must pass the allowlist) from stdlib module internals.
        let scope = vm.new_scope_with_builtins();
        let _ = scope.globals.set_item(
            "__name__",
            vm.ctx.new_str("__main__").into(),
            vm,
        );
        let exec_result = vm.run_code_obj(code, scope.clone());

        let (stdout, stderr) = output.into_strings();

        match exec_result {
            Ok(_) => {
                // ── Step 3: Extract return value ──────────────────────────
                // If executor.rs wrapped the last expression as `__result__ = <expr>`,
                // we can retrieve it from scope locals.
                let return_value = extract_return_value(vm, &scope);
                VmRunResult {
                    stdout,
                    stderr,
                    return_value,
                    error: None,
                }
            }
            Err(exc) => {
                // Check if it's our sentinel ModuleNotAllowed exception first.
                if let Some(module_err) = extract_module_not_allowed(vm, &exc) {
                    return VmRunResult {
                        stdout,
                        stderr,
                        return_value: None,
                        error: Some(module_err),
                    };
                }
                // Otherwise it's a RuntimeError.
                VmRunResult {
                    stdout,
                    stderr,
                    return_value: None,
                    error: Some(extract_runtime_error(vm, exc)),
                }
            }
        }
    })
}

// ── Private helpers ───────────────────────────────────────────────────────────

/// Returns `true` if the import is originating from user code (not from stdlib).
///
/// Strategy: check `__name__` in the calling module's globals.
/// - User code (compiled from a string) runs with `__name__ == "__main__"`.
/// - Any real module (stdlib, frozen, etc.) has a non-"__main__" `__name__`.
///
/// Falls back to checking `__file__` when `__name__` is unavailable:
/// user code has `__file__ == "<string>"` or no `__file__`, while stdlib modules
/// have real filesystem paths.  Frozen modules may have `__file__ == None`, so
/// we treat them as stdlib (non-user-code) when their `__name__ != "__main__"`.
fn is_user_code_import(args: &FuncArgs, vm: &VirtualMachine) -> bool {
    let globals = match args.args.get(1) {
        Some(g) => g,
        None => return true, // No globals — assume user code.
    };

    if vm.is_none(globals) {
        return true; // None globals — assume user code.
    }

    // Primary check: __name__ in globals.
    // User code always runs as "__main__"; real modules have their own name.
    if let Ok(name_val) = vm.call_method(globals, "get", (vm.ctx.new_str("__name__"),)) {
        if !vm.is_none(&name_val) {
            if let Ok(name_str) = name_val.str(vm) {
                let name = name_str.as_str();
                // If __name__ is explicitly set to a non-__main__ value, it's a
                // module (stdlib, frozen, site-package) — not user code.
                if !name.is_empty() && name != "__main__" {
                    return false;
                }
                // "__main__" → user code; fall through to __file__ check only if
                // __name__ is exactly "__main__".
                if name == "__main__" {
                    return true;
                }
            }
        }
    }

    // Fallback: check __file__.
    // User code is compiled from a string → __file__ is "<string>" (or absent).
    // Frozen modules have __file__ == None → treat as stdlib (return false).
    let file_val = vm
        .call_method(globals, "get", (vm.ctx.new_str("__file__"),))
        .ok();

    match file_val {
        None => true, // get() failed — assume user code.
        Some(v) if vm.is_none(&v) => false, // None __file__ → frozen/stdlib module.
        Some(v) => {
            // Check if __file__ is "<string>" (user code) or a real path (stdlib).
            v.str(vm)
                .map(|s| {
                    let file = s.as_str();
                    // User code markers: compiled from source string
                    file == "<string>" || file == "<stdin>" || file == "<module>" || file.is_empty()
                })
                .unwrap_or(true) // If str() fails, assume user code.
        }
    }
}

/// Check if `module_name` is allowed, considering submodule imports.
///
/// If "json" is in the allowlist, then "json.decoder", "json.encoder", etc.
/// are also allowed because they are internal submodules loaded automatically
/// when importing the parent package.
///
/// Also delegates to `check_module_allowed` for the "os" / "os.path" special case.
fn is_module_allowed(module_name: &str, allowed_set: &HashSet<String>) -> bool {
    // Direct match (handles "json", "math", "os.path", etc.)
    if check_module_allowed(module_name, allowed_set).is_ok() {
        return true;
    }

    // Check if this is a submodule of an allowed parent package.
    // e.g. "json.decoder" is allowed if "json" is allowed.
    if let Some(dot_pos) = module_name.find('.') {
        let parent = &module_name[..dot_pos];
        if check_module_allowed(parent, allowed_set).is_ok() {
            return true;
        }
    }

    false
}

/// Install `builtins.__import__` override that enforces the module allowlist.
///
/// **Approach**: Option C from architecture §17.
/// We replace `builtins.__import__` with a Rust native function that:
/// 1. Extracts the module name (first positional argument).
/// 2. Checks it against `allowed_set` via `check_module_allowed`.
/// 3. If denied, raises `ImportError("ModuleNotAllowed:<name>")`.
/// 4. If allowed, delegates to the original `__import__` function.
///
/// This function is called inside `enter()` (after full initialization),
/// so `builtins.__import__` is guaranteed to exist.
fn install_import_hook(vm: &VirtualMachine, allowed_set: &Arc<HashSet<String>>) {
    // On pool slot reuse, `builtins.__import__` may already be our hook from a
    // previous call. We must always delegate to the REAL original Python __import__,
    // not to a previously installed hook (which would use a stale allowed_set).
    //
    // Strategy: save the real original under `builtins.__pyexec_original_import__`
    // on first install. On subsequent calls, use that saved original instead.
    const SAVED_IMPORT_ATTR: &str = "__pyexec_original_import__";

    let original_import = if let Ok(saved) = vm.builtins.get_attr(SAVED_IMPORT_ATTR, vm) {
        // Already saved from a prior install — use it directly.
        saved
    } else {
        // First install on this interpreter: save the real original.
        let real_original = match vm.builtins.get_attr("__import__", vm) {
            Ok(f) => f,
            Err(_) => return, // Shouldn't happen but handle gracefully.
        };
        let _ = vm.builtins.set_attr(SAVED_IMPORT_ATTR, real_original.clone(), vm);
        real_original
    };

    // Wrap in Arc so the closure captures them safely.
    // PyObjectRef is not Send+Sync but the closure runs within the VM thread only.
    #[allow(clippy::arc_with_non_send_sync)]
    let original_import = Arc::new(original_import);
    let allowed_set = Arc::clone(allowed_set);

    let hook = vm.new_function(
        "__import__",
        move |args: FuncArgs, vm: &VirtualMachine| -> PyResult<PyObjectRef> {
            // Python's __import__ signature:
            //   __import__(name, globals=None, locals=None, fromlist=(), level=0)
            // - name: module name (can be relative like "decoder" when level > 0)
            // - globals: calling module's globals dict (contains __package__)
            // - level: 0 for absolute, > 0 for relative imports
            let module_name: String = args
                .args
                .first()
                .and_then(|o| o.str(vm).ok())
                .map(|s| s.as_str().to_owned())
                .unwrap_or_default();

            // Extract the import level (arg[4] = level, default 0).
            let level: i64 = args
                .args
                .get(4)
                .and_then(|o| {
                    use rustpython_vm::TryFromObject;
                    i64::try_from_object(vm, o.clone()).ok()
                })
                .unwrap_or(0);

            // For relative imports (level > 0), extract the parent package from globals.
            // The full module path would be "<parent>.<name>".
            let full_module_name = if level > 0 {
                // Try to get __package__ or __name__ from globals (arg[1]).
                let package = args
                    .args
                    .get(1)
                    .and_then(|globals| {
                        vm.call_method(globals, "get", (vm.ctx.new_str("__package__"),))
                            .ok()
                            .filter(|v| !vm.is_none(v))
                            .and_then(|v| v.str(vm).ok())
                            .map(|s| s.as_str().to_owned())
                    });

                if let Some(pkg) = package {
                    // Go up `level` levels from the package name.
                    let base = if level > 1 {
                        let parts: Vec<&str> = pkg.split('.').collect();
                        let keep = parts.len().saturating_sub((level - 1) as usize);
                        parts[..keep].join(".")
                    } else {
                        pkg.clone()
                    };

                    if module_name.is_empty() {
                        base
                    } else {
                        format!("{base}.{module_name}")
                    }
                } else {
                    module_name.clone()
                }
            } else {
                module_name.clone()
            };

            // Check if this import is coming from user code (<string>) or from
            // an internal stdlib module. We only enforce the allowlist for imports
            // originating from user code (where __file__ is "<string>" or None).
            //
            // This allows stdlib modules to import their own dependencies freely
            // while still blocking user code from importing denied modules.
            let importing_from_user_code = is_user_code_import(&args, vm);

            if importing_from_user_code {
                // Check allowlist. We check both the full (resolved) module name AND its
                // top-level package. For example, if "json" is allowed, then "json.decoder"
                // and "decoder" (relative import within json) are also allowed.
                let allowed = is_module_allowed(&full_module_name, &allowed_set);
                if !allowed {
                    // Raise ImportError with sentinel prefix so extract_module_not_allowed
                    // can detect it. Use the user-visible name for the error message.
                    let deny_name = if full_module_name != module_name {
                        full_module_name.clone()
                    } else {
                        module_name.clone()
                    };
                    return Err(vm.new_import_error(
                        format!("ModuleNotAllowed:{deny_name}"),
                        vm.ctx.new_str(deny_name),
                    ));
                }
            }

            // Allowed — delegate to original __import__.
            original_import.call(args, vm)
        },
    );

    let _ = vm.builtins.set_attr("__import__", hook, vm);
}

/// Replace `sys.stdout` and `sys.stderr` with write-capturing objects.
///
/// Creates two minimal Python-level objects (one for stdout, one for stderr).
/// Each has:
/// - `write(s)`: delegates to `OutputBuffer::write_stdout` / `write_stderr`
/// - `flush()`: no-op
///
/// RustPython's `print()` calls `sys.stdout.write(s)` then `sys.stdout.write('\n')`,
/// so this captures all print output.
fn install_output_capture(vm: &VirtualMachine, output: OutputBuffer) {
    let stdout_buf = output.clone();
    let stderr_buf = output;

    let stdout_obj = build_writer_object(vm, stdout_buf, true);
    let stderr_obj = build_writer_object(vm, stderr_buf, false);

    let _ = vm.sys_module.set_attr("stdout", stdout_obj, vm);
    let _ = vm.sys_module.set_attr("stderr", stderr_obj, vm);
}

/// Build a minimal Python object with `write(s)` and `flush()` methods.
///
/// The object is a Python module (namespace) with callable attributes.
/// When Python calls `obj.write(s)`, it calls the Rust closure which writes to
/// the `OutputBuffer`.
fn build_writer_object(vm: &VirtualMachine, output: OutputBuffer, is_stdout: bool) -> PyObjectRef {
    // Wrap the OutputBuffer in Arc<Mutex<>> so the closure can own it safely.
    let output = Arc::new(Mutex::new(output));
    let output_clone = Arc::clone(&output);

    let write_fn = vm.new_function(
        "write",
        move |args: FuncArgs, vm: &VirtualMachine| -> PyResult<PyObjectRef> {
            let data: String = args
                .args
                .first()
                .and_then(|o| o.str(vm).ok())
                .map(|s| s.as_str().to_owned())
                .unwrap_or_default();

            let buf = output.lock().expect("OutputBuffer mutex poisoned");
            let write_result = if is_stdout {
                buf.write_stdout(data.as_bytes())
            } else {
                buf.write_stderr(data.as_bytes())
            };

            match write_result {
                Ok(()) => Ok(vm.ctx.new_int(data.len()).into()),
                Err(ExecutionError::OutputLimitExceeded { limit_bytes }) => {
                    // Raise an exception; Python code will see a RuntimeError.
                    Err(vm.new_exception_msg(
                        vm.ctx.exceptions.runtime_error.to_owned(),
                        format!("Output limit exceeded: {limit_bytes} bytes"),
                    ))
                }
                Err(_) => Err(vm.new_runtime_error("Write error".to_owned())),
            }
        },
    );

    let flush_fn = vm.new_function(
        "flush",
        move |_args: FuncArgs, vm: &VirtualMachine| -> PyResult<PyObjectRef> {
            // Keep output_clone alive (ensures the buffer Arc stays valid).
            let _buf = output_clone.lock().expect("OutputBuffer mutex poisoned");
            Ok(vm.ctx.none())
        },
    );

    // Use a Python module as a simple namespace — it supports get_attr/set_attr
    // and is writable. This is the simplest approach that works with RustPython.
    let ns = vm.new_module("<writer>", vm.ctx.new_dict(), None);
    let _ = ns.set_attr("write", write_fn, vm);
    let _ = ns.set_attr("flush", flush_fn, vm);
    // Some Python code checks .closed; make it False.
    let _ = ns.set_attr("closed", vm.ctx.new_bool(false), vm);
    // Some code checks .encoding attribute.
    let _ = ns.set_attr("encoding", vm.ctx.new_str("utf-8"), vm);
    ns.into()
}

/// Convert a RustPython compile error into [`ExecutionError::SyntaxError`].
fn extract_syntax_error(err: rustpython_vm::compiler::CompileError) -> ExecutionError {
    let (row, col) = err.python_location();
    ExecutionError::SyntaxError {
        message: err.to_string(),
        line: row as u32,
        col: col as u32,
    }
}

/// Extract a [`ExecutionError::ModuleNotAllowed`] if the exception originated
/// from our import hook sentinel. Returns `None` if it's a different exception.
fn extract_module_not_allowed(
    vm: &VirtualMachine,
    exc: &PyBaseExceptionRef,
) -> Option<ExecutionError> {
    // The import hook raises ImportError("ModuleNotAllowed:<name>").
    // We detect this by converting the exception to string and checking the prefix.
    let msg = exc.as_object().str(vm).ok()?;
    let s = msg.as_str();
    s.strip_prefix("ModuleNotAllowed:").map(|name| ExecutionError::ModuleNotAllowed {
        module_name: name.to_string(),
    })
}

/// Convert a RustPython runtime exception into [`ExecutionError::RuntimeError`].
///
/// Uses `vm.write_exception` to capture the full traceback. `String` implements
/// `rustpython_vm::py_io::Write` via `write_fmt`, so no custom wrapper needed.
fn extract_runtime_error(vm: &VirtualMachine, exc: PyBaseExceptionRef) -> ExecutionError {
    // Get exception message via str().
    let message = exc
        .as_object()
        .str(vm)
        .map(|s| s.as_str().to_owned())
        .unwrap_or_else(|_| "Unknown runtime error".to_owned());

    // Get formatted traceback. String implements py_io::Write via write_fmt.
    let mut traceback = String::new();
    let _ = vm.write_exception(&mut traceback, &exc);

    ExecutionError::RuntimeError { message, traceback }
}

/// Try to extract the last expression value from the execution scope.
///
/// Uses the `__result__` variable name convention: executor.rs wraps the last
/// expression as `__result__ = <expr>` before compilation. This function looks
/// for `__result__` in `scope.locals` and returns its `repr()` if found.
fn extract_return_value(vm: &VirtualMachine, scope: &Scope) -> Option<String> {
    // scope.locals is an ArgMapping which Deref's to PyObject via AsRef.
    // We call .get("__result__") on it (Python dict protocol).
    let locals_obj: PyObjectRef = scope.locals.as_ref().to_owned();

    let result_obj = vm
        .call_method(&locals_obj, "get", (vm.ctx.new_str("__result__"),))
        .ok()?;

    if vm.is_none(&result_obj) {
        return None;
    }

    result_obj
        .repr(vm)
        .ok()
        .map(|s| s.as_str().to_owned())
}

// ── Tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::DEFAULT_ALLOWED_MODULES;

    fn make_allowed_set() -> HashSet<String> {
        DEFAULT_ALLOWED_MODULES
            .iter()
            .map(|s| s.to_string())
            .collect()
    }

    fn run(code: &str) -> VmRunResult {
        let output = OutputBuffer::new(1_048_576);
        let interp = build_interpreter(make_allowed_set(), output.clone());
        run_code(&interp, code, output)
    }

    // (1) print statement verifies stdout capture
    #[test]
    #[ignore = "slow: VM init per test"]
    fn test_stdout_capture() {
        let result = run("print('hello')");
        assert!(result.error.is_none(), "unexpected error: {:?}", result.error);
        assert_eq!(result.stdout, "hello\n");
    }

    // (2) syntax error input returns SyntaxError variant with line > 0
    #[test]
    #[ignore = "slow: VM init per test"]
    fn test_syntax_error() {
        let result = run("def f(:");
        match result.error {
            Some(ExecutionError::SyntaxError { line, .. }) => {
                assert!(line > 0, "Expected line > 0, got {}", line);
            }
            other => panic!("Expected SyntaxError, got: {:?}", other),
        }
    }

    // (3) ZeroDivisionError returns RuntimeError with 'division' in message (case-insensitive)
    #[test]
    #[ignore = "slow: VM init per test"]
    fn test_zero_division_error() {
        let result = run("x = 1/0");
        match result.error {
            Some(ExecutionError::RuntimeError { ref message, .. }) => {
                assert!(
                    message.to_lowercase().contains("division"),
                    "Expected 'division' in message, got: {message}"
                );
            }
            other => panic!("Expected RuntimeError, got: {:?}", other),
        }
    }

    // (4) denied module returns ModuleNotAllowed
    #[test]
    #[ignore = "slow: VM init per test"]
    fn test_denied_module() {
        let result = run("import socket");
        match result.error {
            Some(ExecutionError::ModuleNotAllowed { module_name }) => {
                assert_eq!(module_name, "socket");
            }
            other => panic!("Expected ModuleNotAllowed(socket), got: {:?}", other),
        }
    }

    // (5) allowed module (json) with default set returns no error
    #[test]
    #[ignore = "slow: VM init per test"]
    fn test_allowed_module_json() {
        let result = run("import json; x = json.dumps({'a': 1})");
        assert!(
            result.error.is_none(),
            "Expected no error for allowed json module, got: {:?}",
            result.error
        );
    }

    // (6) code setting __result__ returns Some via extract_return_value
    #[test]
    #[ignore = "slow: VM init per test"]
    fn test_extract_return_value() {
        // Executor wraps last expression as __result__ = <expr>
        // Simulate that wrapping directly here:
        let result = run("__result__ = 42");
        assert!(
            result.error.is_none(),
            "Unexpected error: {:?}",
            result.error
        );
        assert_eq!(
            result.return_value,
            Some("42".to_string()),
            "Expected return_value == Some('42'), got {:?}",
            result.return_value
        );
    }
}
