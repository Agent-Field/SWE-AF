use clap::Parser;
use llm_pyexec::{execute, ExecutionSettings, DEFAULT_ALLOWED_MODULES};
use std::io::{self, Read};

/// Execute Python code and emit JSON result.
#[derive(Parser, Debug)]
#[command(name = "llm-pyexec-cli", about = "Execute Python code and emit JSON result")]
struct Args {
    /// Read Python source from file instead of stdin
    #[arg(long)]
    file: Option<std::path::PathBuf>,

    /// Timeout in nanoseconds (default: 5000000000 = 5s)
    #[arg(long, default_value_t = 5_000_000_000u64)]
    timeout: u64,

    /// Comma-separated list of allowed modules (default: standard set)
    #[arg(long)]
    modules: Option<String>,
}

fn main() {
    let args = Args::parse();

    // Read Python source.
    let code = if let Some(path) = args.file {
        std::fs::read_to_string(&path).unwrap_or_else(|e| {
            eprintln!("Error reading file: {e}");
            std::process::exit(1);
        })
    } else {
        let mut buf = String::new();
        io::stdin().read_to_string(&mut buf).unwrap_or_else(|e| {
            eprintln!("Error reading stdin: {e}");
            std::process::exit(1);
        });
        buf
    };

    // Build settings.
    let allowed_modules: Vec<String> = if let Some(m) = args.modules {
        m.split(',').map(|s| s.trim().to_string()).collect()
    } else {
        DEFAULT_ALLOWED_MODULES.iter().map(|s| s.to_string()).collect()
    };

    let settings = ExecutionSettings {
        timeout_ns: args.timeout,
        max_output_bytes: 1_048_576,
        allowed_modules,
    };

    // Execute.
    let result = execute(&code, settings);

    // Serialize to JSON. Always exits 0.
    let json = serde_json::to_string(&result).expect("ExecutionResult is always serializable");
    println!("{json}");
    // Exit 0 always — errors are encoded in the JSON, not the exit code.
}
