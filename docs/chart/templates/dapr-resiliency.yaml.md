# chart/templates/dapr-resiliency.yaml

Rationale for `chart/templates/dapr-resiliency.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: dapr.io/v1alpha1`

Dapr Resiliency (opt-in, ships TOGETHER with the deadLetterTopic subscriptions + the crash-recovery
broker backOff — see values.yaml dapr.resiliency): the SIDECAR owns pub/sub delivery retries, so a
delivery that exhausts this schedule is published to the subscription's dead-letter topic (visible,
ERROR-logged parking) instead of silently ceasing to redeliver after the broker's maxDeliver.

This object governs pub/sub delivery alone. Service invocation has its own object below
(`<release>-invocation-resiliency`), because Dapr applies invocation resiliency on the CALLING sidecar
and that object is scoped to the one caller there is. Public traffic reaches each service through the
cluster route (route.yaml), which involves no sidecar, so no policy can or needs to see it.

THE POLICY IS `constant` BECAUSE IT IS THE ONLY ONE THAT CAN HOLD A WINDOW. dapr/kit's NewBackOff
reads `Duration` under PolicyConstant alone (`b = backoff.NewConstantBackOff(c.Duration)`); under
PolicyExponential it is ignored outright and the schedule comes from InitialInterval / Multiplier /
RandomizationFactor / MaxInterval. The Resiliency CRD declares only policy, duration, matching,
maxInterval and maxRetries (read off the live CRD, 2026-09-14), so under `exponential` the first
step is pinned at cenkalti/backoff's 500ms default and NO chart value can move it — 0.5 + 0.75 +
1.125 + 1.6875 ≈ 4s, whatever duration is written beside it. `constant` is also the only
DETERMINISTIC shape, for the same reason: `randomizationFactor` is equally unsettable and
exponential carries a 0.5 default that swings the total ±50%.

The arithmetic, so the next edit can check itself: 4 retries × 120s = 480s = 8 min, no jitter, over
5 total attempts. The margin under the components' 720s crash-recovery ackWait is not slack — while
the sidecar retries, the broker still holds the original un-acked, so a schedule outlasting ackWait
has JetStream redeliver the same trigger underneath a retry still in flight and the stage runs
twice. THE HANDLER'S OWN TIME COUNTS AGAINST THAT MARGIN, which is why the window is spent on FEW
LONG steps rather than many short ones: 480s of backoff plus 5 attempts leaves 48s apiece, where the
same 480s split into 30s steps would need 16 attempts and overrun ackWait on any handler slower than
15s. Long steps cost little for the failure this exists to survive — a dependency pod is back inside
~90s, so the FIRST retry already lands after recovery.

Measured 2026-09-14 by driving a poison delivery to `bronze-to-silver` and timing the park; pinned
by tests/unit/test_the_pubsub_retry_window_is_the_one_the_chart_states.py, which computes the
window from the rendered policy under dapr/kit's semantics and checks BOTH edges.

## `policy: constant`

ONLY the fields the Resiliency CRD actually declares: policy, duration, matching,
maxInterval, maxRetries. `initialInterval`, `multiplier` and `randomizationFactor` are
rejected — `strict decoding error: unknown field` — which `helm upgrade` swallows as a
warning while a GitOps reconciler parks the whole Application OutOfSync on it. `maxInterval`
is omitted deliberately: it is read only under `exponential`, so carrying it here would be a
value that looks like a bound and bounds nothing.

## `{{- $subscribers = append $subscribers (dict "appId" "notifications") }}`

The inbox subscription, registered in the SAME act that mints its component. Both halves of the
omission are silent and they point opposite ways: unregistered, an app-returned RETRY falls back to
the broker's crash-recovery schedule with no dead-lettering at all; dead-lettering turned on WITHOUT
this line, the FIRST transient failure parks the message — the publication head's defect, measured
and fixed this session. `IngressSettings.dlq_topic` therefore ships empty, and enabling it is an act
that presupposes this line.

## `{{- if and .Values.maintenance.enabled .Values.maintenance.arrivalEnabled }}`

MAINTENANCE's write-event subscription. Added here under the SAME condition dapr-component.yaml uses
to create it, so the two lists cannot disagree about whether the lane exists. It also puts the app-id
into `scopes:` below, which is the half a component-only entry would silently miss.

## `{{- if .Values.services.notifications }}`

The NOTIFICATION plane's control-topic subscriber (v3 targeting), which `$subscribers` misses
because `lance.subPubsub` renders the LINEAGE-topic
component name, and this subscription rides the control one. `notifications` is already in
`$subscribers`, so `scopes:` below covers the app — it is only the second COMPONENT target that
has to be named, which is the shape that makes this class of omission invisible.

Rendered under the same condition as the Component itself (dapr-component.yaml), from the same
expression, so a rename of `pubsub.controlName` cannot leave the policy pointing at a component
that is gone. Gated by tests/unit/test_invariants.py's durable-component sweep, which is what
caught this one before it shipped.

## `{{- if or $notifications.frontDoor .Values.singleTenant.enabled }}` — `<release>-invocation-resiliency`

The service-invocation policy, rendered only when notifications renders, because notifications is the
only service-invocation caller in the estate: its reconciler reads lineage's durable feed through
`/v1.0/invoke/lineage/method` (`IngressSettings.feed_base_url`). Dapr applies an invocation policy on
the CALLING sidecar, so `scopes: [notifications]` is what makes it apply at all; a policy scoped to any
other app-id governs nothing on this hop. Pinned by
`tests/unit/test_invariants.py::test_the_notifications_pod_asks_for_a_sidecar_and_is_allowed_to_receive_one`.

`invokeRetry` is `constant` 2 s, three retries, matching `408,429,500-599`: never a 4xx, because a 403's
answer will not change, and a missing `event_reader` grant is a 403 on every tick that no retry can fix.
`invokeTimeout` is 300 s. The only target is app `lineage`. No policy targets the catalog: no service
invokes it through Dapr, so its write doors are replayed only by their own callers (see
`service_kit.lakehouse.idempotency`).

A pass that exhausts the retries leaves the reconciler's cursor where it was (it advances only on a pass
that completes), so the next cron tick re-walks it.
