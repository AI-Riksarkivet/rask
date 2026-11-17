//! This process's secrets, read from the secret store as the pod's own identity: the sole source, never the environment.
//!
//! A service logs in to the store's Kubernetes auth method as the role bound to its own ServiceAccount, presenting the
//! projected token the kubelet rotates, read from its file at every login and kept nowhere. It holds the store token
//! the login returns in memory for its lease, and reads each KV v2 secret under it. A loop logs in again at half the
//! lease and asks the store about the held token at every pass (`auth/token/lookup-self`), because the store answers
//! 403 both to a token it dropped and to a secret the policy does not name, and only that question tells them apart.
//! No read logs in while the loop is passing, so a failing store is asked for a login once a pass and never once a read.
//!
//! What is decided is in `decide`; the session is `store`; the signers' published keys and their cache are `published`.

pub mod decide;
pub mod published;
pub mod store;

pub use decide::{SecretStoreSettings, fits_a_header};
pub use published::{PublishedKeys, signing_public_secret, store_fetch};
pub use store::{Budget, Pass, Refused, SecretStore, Session, Unanswered, read_required_secret};
