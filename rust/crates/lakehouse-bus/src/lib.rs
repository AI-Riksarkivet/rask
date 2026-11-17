//! The lakehouse's one seam to the bus (NATS JetStream): the CloudEvents envelope each event rides in, publishing it,
//! taking the deliveries of a durable consumer and settling each by `lakehouse_core::delivery`, and reading a stream by
//! sequence. The envelope, the page and the wire protocol are values; `bus` is the shell that speaks to the bus.

pub mod bus;
pub mod envelope;
pub mod instant;
pub mod jetstream;
pub mod page;
pub mod pystr;

pub use bus::{Bus, BusSettings, Handler, HandlingPanicked, PublishError, Unavailable, publish_event};
pub use envelope::{CloudEvent, TraceContext, lineage_envelope};
pub use page::StreamPage;
