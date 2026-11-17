//! The lineage service's binary. `lineage operations` prints the operations it serves, for the port's ledger.

fn main() {
    if std::env::args().nth(1).as_deref() == Some("operations") {
        let served: Vec<_> = lineage::SERVED
            .iter()
            .map(|(method, path)| serde_json::json!({"method": method, "path": path}))
            .collect();
        println!("{}", serde_json::Value::Array(served));
        return;
    }
    eprintln!("usage: lineage operations");
    std::process::exit(2);
}
