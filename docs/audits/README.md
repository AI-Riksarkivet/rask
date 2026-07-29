# Audits — the long-form evidence

The trackers (`docs/GOAL-*.md`) carry verdicts and the commits that acted on them. These files carry the
working: every route enumerated, every citation, every command re-run, and the adversarial verification
pass appended at the bottom of each.

They are here because the evidence was nearly lost. All three were written to a scratchpad, and a scratchpad
does not survive the session — so a claim like "26 pre-merge routes map 1:1 to 26 lakehouse routes" would
have been a sentence in a tracker with nothing behind it. The point of an audit is that the next person can
check it, which means the table has to outlive the conversation that produced it.

## How to read them

Each was produced by one agent and then re-checked by a second whose **default verdict was REFUTED** unless
the code itself carried the claim. The `## Verification` section at the end of each file is that second
pass, and it is the more interesting half — it refutes as well as confirms, which is the only way to know
the first pass was worth anything.

| file | what it establishes | outcome |
| ---- | ------------------- | ------- |
| `2026-07-26-routes-and-ia.md` | Every route in all four zones; the orphan sweep; the 7→4 merge diff recomputed from git; the IA compared against the Lakekeeper console | 8 of 9 claims confirmed, 1 downgraded to deviates-with-reason |
| `2026-07-26-mfe-composition.md` | The four-zone composition against the micro-frontends skill: zone boundaries, base paths, cross-zone hard-nav, the declaration chain, independent deployability | 5 of 5 top claims confirmed; 6 supporting details refuted |
| `2026-07-26-svelte5.md` | Runes correctness across the estate: `$state`/`$derived`/`$effect`, legacy-vs-runes mixing, props, `bind:` against `$bindable` | 1 real runtime bug confirmed; 3 of the report's own claims refuted |

## What came out of them

Fixed: five dead cross-zone links (`bf00499`), the cross-zone verifier that could not start because its
readiness probe was one of those dead paths (`1cd9329`), a stale prop mirror that displayed a fusion balance
the search was not running (`dff061a`), and the navbar clipping regression the first fix introduced
(`bd8a1cb`).

Recorded as deviates-with-reason rather than fixed: `TableDetail.svelte:331`'s 60-assignment reset effect
(intent right, mechanism hand-maintained), and the fact that nothing automated composes all four zones
behind one origin.

## A note on the second pass

It is worth reading the refutations before trusting any audit of this kind. In the Svelte report the first
pass wanted five `$effect`s deleted; two of them were options-validity clamps and deleting them would have
introduced bugs. In the routes report a "dead route" was actually reachable and the verdict had to be
downgraded. Neither would have been caught by a more careful single pass — they were caught by an
adversarial one.
