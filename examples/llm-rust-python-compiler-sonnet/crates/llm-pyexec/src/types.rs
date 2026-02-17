//! Foundational public types for the llm-pyexec library.
//!
//! This module defines the core data structures used throughout the library:
//! - [`ExecutionSettings`] — configuration for a single Python execution
//! - [`ExecutionResult`] — the result of a Python execution
//! - [`ExecutionError`] — structured error variants
//! - [`DEFAULT_ALLOWED_MODULES`] — the default set of permitted stdlib modules

use serde::{Deserialize, Serialize};

/// The default set of Python standard library modules permitted for import.
///
/// Contains 11 modules commonly needed for data-processing and general scripting
/// while excluding network, filesystem, and subprocess modules that pose security
/// or sandboxing concerns.
pub const DEFAULT_ALLOWED_MODULES: &[&str] = &[
    "math",
    "re",
    "json",
    "datetime",
    "collections",
    "itertools",
    "functools",
    "string",
    "random",
    "os.path",
    "sys",
];

/// Configuration that governs how a single Python snippet is executed.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExecutionSettings {
    /// Maximum wall-clock time in nanoseconds before the execution is aborted.
    /// Default: 5,000,000,000 ns (5 seconds).
    pub timeout_ns: u64,

    /// Maximum number of bytes that may be written to stdout + stderr combined.
    /// Default: 1,048,576 bytes (1 MiB).
    pub max_output_bytes: usize,

    /// List of Python module names that scripts are permitted to import.
    /// Any `import` statement for a module not in this list raises
    /// [`ExecutionError::ModuleNotAllowed`].
    pub allowed_modules: Vec<String>,
}

impl Default for ExecutionSettings {
    fn default() -> Self {
        Self {
            timeout_ns: 5_000_000_000,
            max_output_bytes: 1_048_576,
            allowed_modules: DEFAULT_ALLOWED_MODULES
                .iter()
                .map(|s| s.to_string())
                .collect(),
        }
    }
}

/// The outcome of executing a Python snippet.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExecutionResult {
    /// Everything written to `sys.stdout` during execution (UTF-8).
    pub stdout: String,

    /// Everything written to `sys.stderr` during execution (UTF-8).
    pub stderr: String,

    /// The `repr()` of the last expression evaluated, or `None` if the snippet
    /// ended with a statement (or produced no value).
    pub return_value: Option<String>,

    /// `None` on success; `Some(e)` if execution was terminated by an error.
    pub error: Option<ExecutionError>,

    /// Elapsed wall-clock time of the execution in nanoseconds.
    pub duration_ns: u64,
}

/// Structured error variants produced when Python execution fails.
///
/// Serialized with an internally-tagged `"type"` discriminator field so that
/// JSON consumers can switch on `error.type` without a wrapper object.
///
/// # Examples (JSON)
/// ```json
/// {"type":"SyntaxError","message":"invalid syntax","line":1,"col":5}
/// {"type":"RuntimeError","message":"division by zero","traceback":"..."}
/// {"type":"Timeout","limit_ns":5000000000}
/// {"type":"OutputLimitExceeded","limit_bytes":1048576}
/// {"type":"ModuleNotAllowed","module_name":"socket"}
/// ```
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum ExecutionError {
    /// The Python source could not be parsed.
    SyntaxError {
        /// Human-readable description of the parse error.
        message: String,
        /// 1-based line number of the error, or 0 if unknown.
        line: u32,
        /// 1-based column number of the error, or 0 if unknown.
        col: u32,
    },

    /// A Python exception was raised during execution.
    RuntimeError {
        /// The exception message (e.g. `"division by zero"`).
        message: String,
        /// Python-formatted traceback string, or empty if unavailable.
        traceback: String,
    },

    /// Execution exceeded the configured [`ExecutionSettings::timeout_ns`].
    Timeout {
        /// The timeout limit that was exceeded, in nanoseconds.
        limit_ns: u64,
    },

    /// Combined stdout + stderr output exceeded [`ExecutionSettings::max_output_bytes`].
    OutputLimitExceeded {
        /// The output limit that was exceeded, in bytes.
        limit_bytes: usize,
    },

    /// The script attempted to import a module not present in
    /// [`ExecutionSettings::allowed_modules`].
    ModuleNotAllowed {
        /// The exact module name that was denied.
        module_name: String,
    },
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── ExecutionSettings::default() field assertions ─────────────────────────

    #[test]
    fn test_execution_settings_default_timeout_ns() {
        let settings = ExecutionSettings::default();
        assert_eq!(settings.timeout_ns, 5_000_000_000);
    }

    #[test]
    fn test_execution_settings_default_max_output_bytes() {
        let settings = ExecutionSettings::default();
        assert_eq!(settings.max_output_bytes, 1_048_576);
    }

    #[test]
    fn test_execution_settings_default_allowed_modules_count() {
        let settings = ExecutionSettings::default();
        assert_eq!(settings.allowed_modules.len(), 11);
    }

    #[test]
    fn test_execution_settings_default_allowed_modules_contents() {
        let settings = ExecutionSettings::default();
        for module in DEFAULT_ALLOWED_MODULES {
            assert!(
                settings.allowed_modules.contains(&module.to_string()),
                "Expected '{}' in default allowed_modules",
                module
            );
        }
    }

    // ── DEFAULT_ALLOWED_MODULES length assertion ──────────────────────────────

    #[test]
    fn test_default_allowed_modules_length_is_11() {
        assert_eq!(DEFAULT_ALLOWED_MODULES.len(), 11);
    }

    // ── ExecutionError serde round-trips ──────────────────────────────────────

    #[test]
    fn test_execution_error_syntax_error_round_trip() {
        let error = ExecutionError::SyntaxError {
            message: "invalid syntax".to_string(),
            line: 1,
            col: 5,
        };
        let json = serde_json::to_string(&error).expect("serialize SyntaxError");
        assert!(
            json.contains(r#""type":"SyntaxError""#),
            "JSON should contain type discriminator: {json}"
        );
        assert!(json.contains(r#""message":"invalid syntax""#));
        assert!(json.contains(r#""line":1"#));
        assert!(json.contains(r#""col":5"#));
        let deserialized: ExecutionError = serde_json::from_str(&json).expect("deserialize SyntaxError");
        assert_eq!(deserialized, error);
    }

    #[test]
    fn test_execution_error_runtime_error_round_trip() {
        let error = ExecutionError::RuntimeError {
            message: "division by zero".to_string(),
            traceback: "Traceback (most recent call last):\n  ...".to_string(),
        };
        let json = serde_json::to_string(&error).expect("serialize RuntimeError");
        assert!(
            json.contains(r#""type":"RuntimeError""#),
            "JSON should contain type discriminator: {json}"
        );
        assert!(json.contains(r#""message":"division by zero""#));
        let deserialized: ExecutionError = serde_json::from_str(&json).expect("deserialize RuntimeError");
        assert_eq!(deserialized, error);
    }

    #[test]
    fn test_execution_error_timeout_round_trip() {
        let error = ExecutionError::Timeout {
            limit_ns: 5_000_000_000,
        };
        let json = serde_json::to_string(&error).expect("serialize Timeout");
        assert!(
            json.contains(r#""type":"Timeout""#),
            "JSON should contain type discriminator: {json}"
        );
        assert!(json.contains(r#""limit_ns":5000000000"#));
        let deserialized: ExecutionError = serde_json::from_str(&json).expect("deserialize Timeout");
        assert_eq!(deserialized, error);
    }

    #[test]
    fn test_execution_error_output_limit_exceeded_round_trip() {
        let error = ExecutionError::OutputLimitExceeded { limit_bytes: 1_048_576 };
        let json = serde_json::to_string(&error).expect("serialize OutputLimitExceeded");
        assert!(
            json.contains(r#""type":"OutputLimitExceeded""#),
            "JSON should contain type discriminator: {json}"
        );
        assert!(json.contains(r#""limit_bytes":1048576"#));
        let deserialized: ExecutionError =
            serde_json::from_str(&json).expect("deserialize OutputLimitExceeded");
        assert_eq!(deserialized, error);
    }

    #[test]
    fn test_execution_error_module_not_allowed_round_trip() {
        let error = ExecutionError::ModuleNotAllowed {
            module_name: "socket".to_string(),
        };
        let json = serde_json::to_string(&error).expect("serialize ModuleNotAllowed");
        assert!(
            json.contains(r#""type":"ModuleNotAllowed""#),
            "JSON should contain type discriminator: {json}"
        );
        assert!(json.contains(r#""module_name":"socket""#));
        let deserialized: ExecutionError =
            serde_json::from_str(&json).expect("deserialize ModuleNotAllowed");
        assert_eq!(deserialized, error);
    }
}
