//! Where a service is in its life: starting, serving, or draining before it stops.

use std::sync::atomic::{AtomicBool, Ordering};

#[derive(Debug, Default)]
pub struct Lifecycle {
    started: AtomicBool,
    draining: AtomicBool,
}

impl Lifecycle {
    /// Everything the service needs is up: it may take traffic.
    pub fn mark_started(&self) {
        self.started.store(true, Ordering::SeqCst);
        self.draining.store(false, Ordering::SeqCst);
    }

    /// The service was told to stop: it finishes what it holds and takes nothing new.
    pub fn mark_draining(&self) {
        self.draining.store(true, Ordering::SeqCst);
        self.started.store(false, Ordering::SeqCst);
    }

    pub fn is_started(&self) -> bool {
        self.started.load(Ordering::SeqCst)
    }

    pub fn is_draining(&self) -> bool {
        self.draining.load(Ordering::SeqCst)
    }
}
