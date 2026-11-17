//! What lineage counts of its own work, under the names `lineage.core.metrics` and `lakehouse.outbox_metrics` record:
//! each event's outcome at the door it came through, an ingest's duration, a signature's verdict, a forward, and the
//! outbox the relay drains. The bus counts its own (`lakehouse_bus::Bus::instrumented`).

use lakehouse_core::telemetry::Telemetry;
use lakehouse_core::telemetry::instruments::{LineageMetrics, OutboxLane, OutboxMetrics, TimerMetrics};
use opentelemetry::metrics::Meter;

pub struct Signals {
    pub lineage: LineageMetrics,
    pub outbox: OutboxMetrics,
    /// The scope the relay's timer is counted on, which its own instruments are made from when it starts.
    pub timers: Meter,
}

impl Signals {
    /// The instruments on the meter `meter` gives each scope (`lance.lineage`, `lance.outbox`, `lance.timers`), every
    /// series a rule reads the increase of created at 0.
    pub fn new(meter: impl Fn(&'static str) -> Meter) -> Self {
        let signals = Signals {
            lineage: LineageMetrics::new(&meter("lance.lineage")),
            outbox: OutboxMetrics::new(&meter("lance.outbox")),
            timers: meter("lance.timers"),
        };
        signals.lineage.create_outcome_series();
        signals.lineage.create_signature_refused_series();
        signals
    }

    pub fn of(telemetry: &Telemetry) -> Self {
        Signals::new(|scope| telemetry.meter(scope))
    }

    pub fn timer(&self) -> TimerMetrics {
        TimerMetrics::new(&self.timers)
    }
}

/// The outbox lineage's relay drains.
pub const LANE: OutboxLane = OutboxLane::Lineage;
