//! The lineage service's binary. `lineage serve` serves it from the environment the chart gives its Deployment, with the
//! server's flags (`lineage::service::Args`); `lineage operations` prints the operations it serves, for the port's ledger.

use lineage::service::{Args, START_REFUSED, run};

fn main() {
    let mut args = std::env::args().skip(1);
    match args.next().as_deref() {
        Some("operations") => {
            let served: Vec<_> = lineage::SERVED
                .iter()
                .map(|(method, path)| serde_json::json!({"method": method, "path": path}))
                .collect();
            println!("{}", serde_json::Value::Array(served));
        }
        Some("serve") => {
            let code = match (Args::parse(args), tokio::runtime::Runtime::new()) {
                (Ok(args), Ok(runtime)) => runtime.block_on(run(args)),
                (Err(why), _) => {
                    eprintln!("lineage did not start: {why}");
                    START_REFUSED
                }
                (_, Err(why)) => {
                    eprintln!("lineage did not start: no runtime: {why}");
                    START_REFUSED
                }
            };
            std::process::exit(code);
        }
        _ => {
            eprintln!("usage: lineage serve [--host=<address>] [--port=<port>] [--timeout-graceful-shutdown=<seconds>] | lineage operations");
            std::process::exit(2);
        }
    }
}
