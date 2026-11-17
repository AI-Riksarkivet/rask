//! The Rust side of `scripts/bus_interop.py`, which shows the Python and Rust buses working one stream together.
//!
//! `bus_interop publish <run id>…` publishes each run's COMPLETE event on `lineage.events.v1`; `bus_interop consume
//! <count>` takes that many deliveries of `LINEAGE/lineage-ingest` and prints each envelope's id. The bus is the one
//! `RASK_BUS_*` names, as for a service.

use lakehouse_bus::{Bus, BusSettings, Handler, lineage_envelope, publish_event};
use lakehouse_core::delivery::{Delivery, Lane, Verdict};
use serde_json::{Value, json};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;

struct Printer(mpsc::UnboundedSender<String>);

impl Handler for Printer {
    async fn handle(&self, delivery: &Delivery) -> Result<Verdict, String> {
        let id = delivery
            .envelope()
            .and_then(|envelope| envelope.get("id").and_then(Value::as_str).map(str::to_owned));
        let _ = self.0.send(id.unwrap_or_default());
        Ok(Verdict::Taken)
    }

    async fn park(&self, _: &Delivery, reason: &str) -> Result<(), String> {
        Err(format!("nothing is parked here: {reason}"))
    }
}

#[tokio::main]
async fn main() -> Result<(), String> {
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    let settings = BusSettings::from_env()?.ok_or("RASK_BUS_URL names no bus")?;
    let bus = Arc::new(Bus::start(settings));
    for _ in 0..500 {
        if bus.answering().is_ok() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    match arguments.split_first() {
        Some((mode, runs)) if mode == "publish" => {
            for run in runs {
                let event = json!({"eventType": "COMPLETE", "eventTime": "2026-11-17T10:00:00Z", "run": {"runId": run}, "producer": "rask/rust"});
                let envelope = lineage_envelope(event.as_object().ok_or("no event")?);
                publish_event(&bus, &envelope, "lineage.events.v1", Duration::from_secs(5), None)
                    .await
                    .map_err(|error| error.to_string())?;
                println!("{}", envelope.id);
            }
        }
        Some((mode, count)) if mode == "consume" => {
            let count: usize = count.first().and_then(|count| count.parse().ok()).ok_or("consume takes a count")?;
            let (sender, mut ids) = mpsc::unbounded_channel();
            let lane = Lane::new("LINEAGE", "lineage-ingest", 3, Duration::from_secs(1), Duration::from_secs(30), 4)?;
            let (stop, stopped) = tokio::sync::oneshot::channel::<()>();
            let consumer = Arc::clone(&bus);
            let task = tokio::spawn(async move {
                consumer
                    .consume(&lane, Arc::new(Printer(sender)), async {
                        let _ = stopped.await;
                    })
                    .await
            });
            for _ in 0..count {
                let id = tokio::time::timeout(Duration::from_secs(30), ids.recv())
                    .await
                    .map_err(|_| "no delivery came")?;
                println!("{}", id.unwrap_or_default());
            }
            let _ = stop.send(());
            task.await.map_err(|error| error.to_string())?.map_err(|error| error.to_string())?;
        }
        _ => return Err("usage: bus_interop publish <run id>… | consume <count>".to_owned()),
    }
    Ok(())
}
