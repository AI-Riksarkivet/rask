//! Whether what a service depends on is answering, asked in the background so a probe never waits on a dependency.

use serde::Serialize;
use std::collections::BTreeMap;
use std::pin::Pin;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;
use tokio::task::JoinHandle;
use tokio::time::Instant;

/// How often each dependency is asked.
pub const HEALTH_INTERVAL: Duration = Duration::from_secs(5);
/// How long a dependency has to answer.
pub const HEALTH_BUDGET: Duration = Duration::from_secs(5);

/// One read of a dependency: `Err` says why it did not answer.
pub type Read = Box<dyn Fn() -> Pin<Box<dyn Future<Output = Result<(), String>> + Send>> + Send + Sync>;

/// Something the service calls. A service is unhealthy without a `required` one and degraded without another.
pub struct Dependency {
    pub name: &'static str,
    pub required: bool,
    pub read: Read,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ReadinessStatus {
    Starting,
    Ready,
    Degraded,
    Unhealthy,
    ShuttingDown,
}

/// What `/readyz` answers.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Readiness {
    pub status: ReadinessStatus,
    pub components: BTreeMap<String, String>,
}

impl Readiness {
    pub fn of(status: ReadinessStatus) -> Self {
        Readiness {
            status,
            components: BTreeMap::new(),
        }
    }

    /// Whether traffic should be sent here.
    pub fn in_rotation(&self) -> bool {
        matches!(self.status, ReadinessStatus::Ready | ReadinessStatus::Degraded)
    }
}

#[derive(Debug, Clone, Copy, Default)]
struct Answer {
    answering: Option<bool>,
    answered_at: Option<Instant>,
}

pub struct HealthWatch {
    dependencies: Vec<Dependency>,
    answers: Mutex<Vec<Answer>>,
    interval: Duration,
    budget: Duration,
}

impl HealthWatch {
    pub fn new(dependencies: Vec<Dependency>) -> Self {
        let answers = Mutex::new(vec![Answer::default(); dependencies.len()]);
        HealthWatch {
            dependencies,
            answers,
            interval: HEALTH_INTERVAL,
            budget: HEALTH_BUDGET,
        }
    }

    fn answers(&self) -> std::sync::MutexGuard<'_, Vec<Answer>> {
        self.answers.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Ask the dependency at `index` once and keep its answer. A change of answer is logged, a repeat is not.
    pub async fn check(&self, index: usize) {
        let dependency = &self.dependencies[index];
        let answered = match tokio::time::timeout(self.budget, (dependency.read)()).await {
            Ok(answered) => answered,
            Err(_) => Err(format!("no answer within {} s", self.budget.as_secs())),
        };
        let mut answers = self.answers();
        let answer = &mut answers[index];
        match answered {
            Ok(()) => {
                if answer.answering != Some(true) {
                    tracing::info!(dependency = dependency.name, "dependency_answering");
                }
                *answer = Answer {
                    answering: Some(true),
                    answered_at: Some(Instant::now()),
                };
            }
            Err(error) => {
                if answer.answering != Some(false) {
                    tracing::warn!(dependency = dependency.name, %error, "dependency_unavailable");
                }
                answer.answering = Some(false);
            }
        }
    }

    /// Whether the dependency answered its last read, and that read is recent enough to speak for now.
    fn answering(&self, answer: Answer) -> bool {
        let fresh = |at: Instant| at.elapsed() <= 2 * (self.interval + self.budget);
        answer.answering == Some(true) && answer.answered_at.is_some_and(fresh)
    }

    pub fn readiness(&self) -> Readiness {
        let answers = self.answers();
        let answering: Vec<bool> = answers.iter().map(|answer| self.answering(*answer)).collect();
        let verdict = |up: &bool| if *up { "healthy" } else { "unavailable" }.to_owned();
        let components = self
            .dependencies
            .iter()
            .zip(&answering)
            .map(|(dependency, up)| (dependency.name.to_owned(), verdict(up)))
            .collect();
        let down = || {
            self.dependencies
                .iter()
                .zip(&answering)
                .filter(|(_, up)| !**up)
                .map(|(dependency, _)| dependency)
        };
        let status = if down().any(|dependency| dependency.required) {
            ReadinessStatus::Unhealthy
        } else if down().next().is_some() {
            ReadinessStatus::Degraded
        } else {
            ReadinessStatus::Ready
        };
        Readiness { status, components }
    }

    /// Ask every dependency on the interval, each on a task of its own, until the tasks are aborted.
    pub fn watch(self: &Arc<Self>) -> Vec<JoinHandle<()>> {
        let forever = |index: usize| {
            let watch = Arc::clone(self);
            tokio::spawn(async move {
                loop {
                    watch.check(index).await;
                    tokio::time::sleep(watch.interval).await;
                }
            })
        };
        (0..self.dependencies.len()).map(forever).collect()
    }
}
