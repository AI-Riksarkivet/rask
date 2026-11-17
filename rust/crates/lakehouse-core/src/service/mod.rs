//! The shell every service is served in: its probes, its error bodies and the limits every request is held to.

pub mod health;
pub mod lifecycle;
pub mod problem;
pub mod shell;
pub mod trace;
