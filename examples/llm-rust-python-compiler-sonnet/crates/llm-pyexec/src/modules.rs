//! Module allowlist checker for the llm-pyexec library.
//!
//! Provides two public functions:
//! - [`check_module_allowed`] — verifies a module name against an allowlist `HashSet`.
//! - [`build_allowed_set`] — converts [`ExecutionSettings::allowed_modules`] into a
//!   `HashSet<String>` for O(1) per-import lookup.
//!
//! ## Special case: `os` / `os.path`
//!
//! Python's `os.path` is a submodule of `os`; importing `os.path` causes Python to
//! first load the `os` parent module.  To permit `import os.path` (which is in the
//! default allowlist) without also permitting a bare `import os`, the check grants
//! `"os"` whenever `"os.path"` is present in the allowlist.

use std::collections::HashSet;

use crate::types::{ExecutionError, ExecutionSettings};

/// Checks whether `module_name` is permitted by the given allowlist.
///
/// Returns `Ok(())` if the module is allowed, or
/// `Err(ExecutionError::ModuleNotAllowed { module_name })` if it is not.
///
/// # Special case
///
/// If `module_name` is `"os"` and `"os.path"` is present in `allowed_set`, the
/// function returns `Ok(())`.  This is required because Python automatically loads
/// the `os` parent when `import os.path` is executed.
pub fn check_module_allowed(
    module_name: &str,
    allowed_set: &HashSet<String>,
) -> Result<(), ExecutionError> {
    if allowed_set.contains(module_name) {
        return Ok(());
    }

    // Special case: allow bare "os" import when "os.path" is in the allowlist,
    // because Python's import machinery loads "os" as a side-effect of "os.path".
    if module_name == "os" && allowed_set.contains("os.path") {
        return Ok(());
    }

    Err(ExecutionError::ModuleNotAllowed {
        module_name: module_name.to_string(),
    })
}

/// Builds a `HashSet<String>` from [`ExecutionSettings::allowed_modules`] for
/// O(1) per-import lookup during Python execution.
pub fn build_allowed_set(settings: &ExecutionSettings) -> HashSet<String> {
    settings.allowed_modules.iter().cloned().collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{ExecutionSettings, DEFAULT_ALLOWED_MODULES};

    // ── check_module_allowed: basic allow/deny ─────────────────────────────────

    #[test]
    fn test_allowed_module_returns_ok() {
        let mut set = HashSet::new();
        set.insert("json".to_string());
        assert_eq!(check_module_allowed("json", &set), Ok(()));
    }

    #[test]
    fn test_denied_module_returns_err_with_correct_name() {
        let set = build_allowed_set(&ExecutionSettings::default());
        let result = check_module_allowed("socket", &set);
        assert_eq!(
            result,
            Err(ExecutionError::ModuleNotAllowed {
                module_name: "socket".to_string()
            })
        );
    }

    // ── os / os.path special case ──────────────────────────────────────────────

    #[test]
    fn test_os_allowed_when_os_path_in_set() {
        let mut set = HashSet::new();
        set.insert("os.path".to_string());
        assert_eq!(
            check_module_allowed("os", &set),
            Ok(()),
            "'os' should be permitted when 'os.path' is in the allowlist"
        );
    }

    #[test]
    fn test_os_denied_when_os_path_not_in_set() {
        let mut set = HashSet::new();
        set.insert("math".to_string());
        assert_eq!(
            check_module_allowed("os", &set),
            Err(ExecutionError::ModuleNotAllowed {
                module_name: "os".to_string()
            }),
            "'os' should be denied when 'os.path' is not in the allowlist"
        );
    }

    // ── empty allowlist ────────────────────────────────────────────────────────

    #[test]
    fn test_empty_allowlist_denies_everything() {
        let empty: HashSet<String> = HashSet::new();
        assert_eq!(
            check_module_allowed("json", &empty),
            Err(ExecutionError::ModuleNotAllowed {
                module_name: "json".to_string()
            })
        );
        assert_eq!(
            check_module_allowed("os", &empty),
            Err(ExecutionError::ModuleNotAllowed {
                module_name: "os".to_string()
            })
        );
    }

    // ── build_allowed_set ──────────────────────────────────────────────────────

    #[test]
    fn test_build_allowed_set_from_default_settings_has_11_entries() {
        let settings = ExecutionSettings::default();
        let set = build_allowed_set(&settings);
        assert_eq!(
            set.len(),
            DEFAULT_ALLOWED_MODULES.len(),
            "build_allowed_set should contain all {} DEFAULT_ALLOWED_MODULES entries",
            DEFAULT_ALLOWED_MODULES.len()
        );
    }

    #[test]
    fn test_build_allowed_set_from_default_settings_contains_all_defaults() {
        let settings = ExecutionSettings::default();
        let set = build_allowed_set(&settings);
        for module in DEFAULT_ALLOWED_MODULES {
            assert!(
                set.contains(*module),
                "Expected '{}' in the set built from default settings",
                module
            );
        }
    }

    #[test]
    fn test_build_allowed_set_from_custom_list_has_only_those_entries() {
        let settings = ExecutionSettings {
            allowed_modules: vec!["math".to_string(), "json".to_string()],
            ..ExecutionSettings::default()
        };
        let set = build_allowed_set(&settings);
        assert_eq!(set.len(), 2);
        assert!(set.contains("math"));
        assert!(set.contains("json"));
        assert!(!set.contains("re"));
    }
}
