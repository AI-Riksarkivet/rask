//! Lineage's signals on an in-memory reader, so a test reads what was counted at each door.

use lineage::signals::Signals;
use opentelemetry::metrics::MeterProvider as _;
use opentelemetry_sdk::metrics::data::{AggregatedMetrics, MetricData};
use opentelemetry_sdk::metrics::{InMemoryMetricExporter, PeriodicReader, SdkMeterProvider};
use std::sync::Arc;

pub struct Counted {
    provider: SdkMeterProvider,
    exporter: InMemoryMetricExporter,
    pub signals: Arc<Signals>,
}

impl Counted {
    pub fn new() -> Counted {
        let exporter = InMemoryMetricExporter::default();
        let provider = SdkMeterProvider::builder()
            .with_reader(PeriodicReader::builder(exporter.clone()).build())
            .build();
        let signals = Arc::new(Signals::new(|scope| provider.meter(scope)));
        Counted { provider, exporter, signals }
    }

    /// How many events `lineage.events.processed` counted at `door` with `outcome`.
    pub fn counted(&self, door: &str, outcome: &str) -> u64 {
        self.provider.force_flush().unwrap();
        let exported = self.exporter.get_finished_metrics().unwrap();
        let Some(latest) = exported.last() else { return 0 };
        let mut total = 0;
        for metric in latest.scope_metrics().flat_map(|scope| scope.metrics()) {
            if let (true, AggregatedMetrics::U64(MetricData::Sum(sum))) = (metric.name() == "lineage.events.processed", metric.data()) {
                for point in sum.data_points() {
                    let label = |key: &str| point.attributes().find(|kv| kv.key.as_str() == key).map(|kv| kv.value.to_string());
                    if label("lance.lineage.door").as_deref() == Some(door) && label("lance.lineage.outcome").as_deref() == Some(outcome) {
                        total += point.value();
                    }
                }
            }
        }
        total
    }
}
