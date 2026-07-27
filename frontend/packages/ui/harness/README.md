# Notification-centre harness

A one-page vite app that mounts `NotificationCenter` in a real browser, so the panel can be opened,
driven and screenshotted. It exists because the panel is **portalled**: `svelte/server`'s `render()`
emits nothing for a portal, so the SSR tests in `tests/notifications.test.ts` can assert the bell and
the rows (`NotificationList` renders standalone) but never the open popover, its focus trap, its
dismiss click or its dark/narrow layout. This closes that gap without adding a DOM test runner —
`@rask/ui` has no jsdom and no `@testing-library/svelte`, and adding either would change the
workspace lockfile.

The rows are shaped from a real payload: `GET /lakehouse/api/runs` as alice on 2026-07-26.

```sh
cd frontend/packages/ui
bunx vite --config vite.config.ts harness --port 5411 --strictPort &   # the package config: the
                                                                      # svelte + tailwind plugins
bun harness/drive.mjs                    # asserts, and writes docs/audits/shots/notifications-*.png
```

`--config vite.config.ts` is load-bearing: started without it, vite finds no plugins and serves the
`.svelte` file untransformed (`Failed to parse source for import analysis`). If a source edit seems
not to apply, restart the server with `--force` — the transform cache held a stale copy of
`notification-center.svelte` once during this work, which read exactly like the change not landing.
