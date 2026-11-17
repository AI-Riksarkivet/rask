//! The background work a process runs beside its doors, each piece named; one that ends while the process serves ends the
//! process (ADR 0203). A lane that stopped leaves a pod that is Ready and does nothing, which only its restart repairs, so
//! the end is said, the telemetry given `FLUSH` to leave, and the process exits `EX_SOFTWARE` for Kubernetes to start again.

use crate::telemetry::Telemetry;
use std::collections::HashMap;
use std::time::Duration;
use tokio::sync::watch;
use tokio::task::{AbortHandle, Id, JoinHandle, JoinSet};

/// The exit code of a process that ended itself because something in it stopped working.
pub const EX_SOFTWARE: i32 = 70;
/// How long the end of a process waits at most for its logs, metrics and traces to leave.
pub const FLUSH: Duration = Duration::from_secs(3);

/// Resolves once the process is told to stop; what a piece kept with [`Supervisor::keep_until_stopped`] finishes on.
pub type Stopping = watch::Receiver<bool>;

#[derive(Default)]
pub struct Supervisor {
    running: JoinSet<()>,
    names: HashMap<Id, &'static str>,
    /// The pieces stopping cancels outright; the others are told and waited for.
    cancelled: Vec<AbortHandle>,
    stop: Option<watch::Sender<bool>>,
}

impl Supervisor {
    /// Run `work` as the process's background work `name`, cancelled when the process stops.
    pub fn keep(&mut self, name: &'static str, work: impl Future<Output = ()> + Send + 'static) {
        let handle = self.running.spawn(work);
        self.names.insert(handle.id(), name);
        self.cancelled.push(handle);
    }

    /// Keep a task already spawned (a session's renewal, a dependency's watch) as the background work `name`, cancelled
    /// when the process stops.
    pub fn keep_spawned(&mut self, name: &'static str, task: JoinHandle<()>) {
        struct CancelledWith(JoinHandle<()>);
        impl Drop for CancelledWith {
            fn drop(&mut self) {
                self.0.abort();
            }
        }
        self.keep(name, async move {
            let mut held = CancelledWith(task);
            let _ = (&mut held.0).await;
        });
    }

    /// Run `work` as the process's background work `name`, told when the process stops and waited for: what it holds
    /// then (a delivery in hand) is finished, never cut.
    pub fn keep_until_stopped<F: Future<Output = ()> + Send + 'static>(&mut self, name: &'static str, work: impl FnOnce(Stopping) -> F) {
        let told = self.stop.get_or_insert_with(|| watch::channel(false).0).subscribe();
        let handle = self.running.spawn(work(told));
        self.names.insert(handle.id(), name);
    }

    /// The first piece to end while the process serves, by name, and whether it panicked; pending while all run.
    pub async fn ended(&mut self) -> (&'static str, bool) {
        let (id, panicked) = match self.running.join_next_with_id().await {
            Some(Ok((id, ()))) => (id, false),
            Some(Err(failed)) => (failed.id(), failed.is_panic()),
            None => std::future::pending().await,
        };
        (self.names.get(&id).copied().unwrap_or("unnamed"), panicked)
    }

    /// Tell every piece the process stops, cancel those that hold nothing, and wait for each to end.
    pub async fn stop(mut self) {
        if let Some(stop) = &self.stop {
            let _ = stop.send(true);
        }
        for handle in &self.cancelled {
            handle.abort();
        }
        while self.running.join_next().await.is_some() {}
    }
}

/// Wait until `stopping` says the process stops.
pub async fn stopped(mut stopping: Stopping) {
    let _ = stopping.wait_for(|stop| *stop).await;
}

/// End this process because the background work `name` ended while it served: said, the telemetry given [`FLUSH`] at
/// most, then [`EX_SOFTWARE`]. Nothing is released first: a piece still running could hold the exit for as long as it
/// runs, and the restart is the repair.
pub async fn end_process(telemetry: &Telemetry, name: &str, panicked: bool) -> ! {
    tracing::error!(loop_name = name, panicked, "background_work_ended");
    let _ = tokio::time::timeout(FLUSH, telemetry.flush()).await;
    std::process::exit(EX_SOFTWARE)
}
