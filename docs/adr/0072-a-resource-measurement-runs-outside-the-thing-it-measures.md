# 0072. A resource measurement runs OUTSIDE the thing it measures (2026-09-21)

**The incident.** Investigating [[LH-183]]'s maintenance OOM, a repro driving four full `run_sweep`
passes was executed via `kubectl exec` INSIDE the pod whose RSS was the measurement. An exec process
shares the container's cgroup, so it counted against both the 512Mi limit and every reading: the series
went 254Mi -> 385 -> 409 -> 438Mi, all of it the probe's own footprint, and stopped roughly 74Mi short
of OOMing the service under investigation. Killing the processes returned RSS to 269Mi, consistent with
the last genuine reading. The pod never restarted.

**Why it is worth a ruling rather than a note.** The failure mode is self-destroying evidence. An OOM
would have restarted the pod and reset the very counter being studied, and the contaminated readings
looked like the most dramatic finding of the investigation — a 180Mi jump exactly where a leak would
show one. Nothing about them was distinguishable from a real result except knowing what else was
running in that container.

**The rule.** A measurement of a resource — memory, file descriptors, connections, disk — runs outside
its target: a separate pod, a local process, or an in-pod probe whose own cost is known to be
negligible. Read-only probes are fine (opening a dataset, calling a planner, reading a counter); driving
the workload at scale is not. The earlier in-pod probes in that same investigation were safe precisely
because they were small, and the distinction is the probe's footprint relative to the limit, never
whether it "feels read-only".

**Two traps that compound it, both hit the same day.** Stopping the local `kubectl` does NOT kill the
remote process — it kept running after it appeared stopped, and had to be found in `/proc/*/cmdline` and
killed by pid. And a cleanup scan whose pattern contains the literal it hunts matches ITSELF: two
successive `case`-based scans reported the scanner as a surviving repro. That is the same shape as the
`pkill -f` self-match, arrived at from a different direction.
