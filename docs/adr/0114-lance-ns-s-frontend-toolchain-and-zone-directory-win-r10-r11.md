# 0114. lance-ns's frontend toolchain and zone directory win (R10, R11, 2026-07-27)

Source: `docs/architecture/lance-ns-merge.md:446,466` (at `44b354f3`) (owner rulings R10 and R11, 2026-07-27).

## Context

rask's frontend linted and formatted with ESLint + Prettier; lance-ns used oxlint + oxfmt + rsvelte-fmt with a
zone-contract test asserting byte-identical scripts across every package. Two toolchains in one workspace
would mean every shared package obeys two formatters, and the merge's copy would need path translation if the
two trees named the zone directory differently.

## Decision

- **R10 — all lance-ns configs come to rask:** the chart and the frontend toolchain. oxlint + oxfmt +
  rsvelte-fmt win; ESLint and Prettier retire. The pure-format commit reformats rask's surviving zones and
  `packages/{api,ui}` under the lance-ns toolchain, and `@repo/zone-contract`'s script-parity gate applies to
  every package unchanged.
- **R11 — the zone directory is `microfrontends/` on both sides.** The trees are identical, so the copy is a
  directory move with no sed and no path translation, and zone-contract's gate files arrive byte-identical to
  upstream.

## Consequences

- `frontend/.oxlintrc.json` and `frontend/.oxfmtrc.json` are the configs; zones live under
  `frontend/microfrontends/`; the contract package is `@rask/zone-contract`, which also fails the build if
  ESLint or Prettier reappear (`CLAUDE.md`, Toolchain rules and Repository layout).
