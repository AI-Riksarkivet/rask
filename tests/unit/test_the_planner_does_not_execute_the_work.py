"""The pod that PLANS maintenance must not be the pod that DOES it ([[LH-183]]).

THE OOM WAS A SIZING DEFECT WEARING AN ALLOCATOR'S CLOTHES. `api/routes.py` has two lanes: with a
`work_topic` the tick plans and enqueues one message per dataset; without one it falls through to
`run_sweep(settings)` and compacts the whole estate inside its own request. Measured on the live
estate 2026-09-22: `MAINTENANCE_WORK_TOPIC` was EMPTY, the planner emitted **2,227 `compaction_*` log
lines in 30 minutes**, and its limit was **512Mi** — while the chart's own `dedicatedWorkers` block
sizes that work at requests 1Gi / limits 4Gi and says why: "compaction reads whole fragments: on
bronze, whose rows are ~1.8MB page images, `scanBatchSize: 64` is ~115MB in flight before Lance's own
overhead."

It never leaked. It was doing a job it was never sized for, and the only question was how long that
took: OOMKilled at 86m52s, then 442m35s under `MALLOC_ARENA_MAX`, then 460m50s under
`ARROW_DEFAULT_MEMORY_POOL=system`. Two allocator fixes bought time around the work; neither could
stop a 512Mi pod doing 4Gi work.

TWO SWITCHES, AND THE CHART EXPLAINS WHY ONE CANNOT DO IT: `workTopic` makes the tick enqueue, and
`dedicatedWorkers` is what sets `MAINTENANCE_EXECUTE_WORK=false` so "THE PLANNER PUBLISHES AND DOES
NOT CONSUME. It still needs the topic — it enqueues onto it — which is exactly why `workTopic` cannot
express this and a second switch exists." Enabling only the queue leaves the planner consuming its own
messages, in the same 512Mi pod: the identical defect with an extra hop.

SO THE INVARIANT IS THE PAIR, not either switch. This gate fails if maintenance ships able to execute
its own heavy work.
"""

from __future__ import annotations

from chart_render import DEFAULT_ARGS, containers, env_of, render


#: BOTH CONTAINERS ARE NAMED `maintenance` — the planner and the worker are told apart by the
#: WORKLOAD, not the container name. Filtering on the name alone folds them into one set, which is how
#: the first version of this gate reported the planner's `EXECUTE_WORK=false` as missing.
def _split(*args: str) -> tuple[list[tuple[str, dict]], list[tuple[str, dict]]]:
    rows = [(where, c) for where, name, c in containers(render(*(args or DEFAULT_ARGS))) if name == "maintenance"]
    workers = [(w, c) for w, c in rows if "worker" in w.lower()]
    planners = [(w, c) for w, c in rows if "worker" not in w.lower()]
    return planners, workers


def _maintenance(*args: str) -> list[tuple[str, dict[str, str]]]:
    return [(where, env_of(c)) for where, c in _split(*args)[0]]


def test_the_walk_finds_the_planner() -> None:
    """Without this every assertion below could hold over an empty set."""
    assert _maintenance(), "no maintenance container rendered — this gate would pass vacuously"


def test_the_planner_is_given_a_WORK_QUEUE() -> None:
    """An empty topic is the serial lane: the tick maintains the whole estate inside its own request."""
    missing = [where for where, env in _maintenance() if not env.get("MAINTENANCE_WORK_TOPIC")]

    assert not missing, f"these planners run the sweep INLINE — no work queue, so `run_sweep` executes in the planner's own pod: {missing}"


def test_the_planner_does_NOT_EXECUTE_what_it_plans() -> None:
    """The half a queue alone cannot give: without this the planner consumes its own messages."""
    executing = [where for where, env in _maintenance() if env.get("MAINTENANCE_EXECUTE_WORK") != "false"]

    assert not executing, f"these planners still EXECUTE the units they enqueue, in a pod sized for planning — the queue only adds a hop: {executing}"


def test_the_WORKER_is_sized_for_the_work_and_the_planner_is_not() -> None:
    """The split is only worth having if the two pods are sized differently.

    A worker inheriting the planner's 512Mi would move the OOM rather than remove it, which is the
    failure this whole row is about wearing a new pod name.
    """
    planners, workers = _split()
    limit = lambda c: (c.get("resources") or {}).get("limits", {}).get("memory", "?")  # noqa: E731

    assert workers, f"no dedicated maintenance worker rendered; only {[w for w, _ in planners]}"
    planner_limits = {limit(c) for _, c in planners}
    for where, c in workers:
        assert limit(c) not in planner_limits, (
            f"{where} inherited the planner's sizing ({limit(c)}), which moves the OOM onto a new pod name rather than removing it"
        )
