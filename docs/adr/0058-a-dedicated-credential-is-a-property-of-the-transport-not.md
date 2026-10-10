# 0058. A dedicated credential is a property of the TRANSPORT, not only of the service (2026-09-07)

F2-3's ordering rule had two cases and needed a third, which the estate paid for live.

The two known cases: the CLIENT half alone (a service reads its own token) is inert, because nothing
yet demands it; the SERVER half alone (a door demands it) is an outage, because the service still
sends the shared one. So for a subject being added they land together.

**`notifications` is the case where landing them together is ALSO an outage.** Its client half was
correct — the resolver read `service-token-notifications` and `feed_token` put it on the wire — and
the door still answered `401 the presented credential may not claim 'notifications'`. The reason is
one layer under the credential: the reconciler reaches lineage through **Dapr service invocation**
(`127.0.0.1:3500/v1.0/invoke/lineage/method/events`), and daprd stamps its OWN `dapr-api-token` on
every request it delivers. Whatever the caller sets, the credential the callee's door sees is the
estate's shared one.

**So the transport decides whether a dedicated credential is possible at all.** `service-ingest`
holds one at the SAME door, and the only difference is that ingest calls lineage directly over HTTP.
A subject reached through service invocation cannot present its own credential, and naming it
privileged converts that into a refusal of every call it makes.

**Why it was not caught by rendering.** All three halves rendered — client, server and seed — and
each is individually correct. The pair looked complete in the chart and in the tests, and the wire is
where it was not. That is the same lesson as "a control's NAME is not evidence that it exists", one
step further along: a control's CONFIGURATION is not evidence either. Only the request on the wire is.

**And the symptom was the failure the change was meant to prevent.** A refused feed walk returns no
rows rather than an error, so the outage presented as a quietly incomplete inbox — which is precisely
why notifications was worth a dedicated credential in the first place.

F2-3's remaining work is therefore a design question rather than a credential one: move that call off
service invocation, or accept that sidecar-invoked hops authenticate as the estate.
