# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues on `AI-Riksarkivet/rask`. Use the `gh` CLI for all operations.

## rask rules (these override the generic conventions below)

- **One issue is one register row.** The title starts with its row id (`LH-243: …`); ids keep their
  prefixes (`LH` lakehouse, `XC` cross-cutting, `CP` controlplane, `CTL` control lane, `FE` frontend,
  `LOW`, `LIN` lineage), are never renumbered and never reused. Code, commits and docs cite the row id;
  `#n` is the GitHub handle for the same row.
- **No new issue without the owner.** A skill that finds a problem proposes it; it does not open a
  counted issue on its own. Unadmitted findings get the `parked` label and stay out of every count.
- **Labels carry the register's markers:** `blocks-prod`, `criterion-1` … `criterion-5`,
  `phase-1`, `phase-1-after-launch`, `phase-2`, `frontend`, `low`, `parked`, the row's severity tag as
  `severity:high` / `severity:medium` / `severity:low` (an OBSERVATION or an untagged item carries
  none), and the triage roles in `triage-labels.md`.
- **The order lives in the pinned map issue** (label `wayfinder:map`, title `Phase 1 map`): its body
  holds FOCUS NOW, the production short list, owner rulings in force and decisions still open.
- **An issue closes when its fix is deployed and read back live, not when it merges.** Close it with a
  comment that names the helm revision.

## Working a row alongside other people (owner, 2026-10-13)

The assignee field is the only record of who is working on what, so these hold for every row, not only
wayfinder tickets:

- **Claim before code.** The first write on a row is `gh issue edit <n> --add-assignee @me`, before a
  branch, a commit or a test. An assigned row belongs to its assignee; nobody else starts it, and a
  question about it goes on the issue. Unassign (`--remove-assignee @me`) when you stop, so it is free again.
- **Pick from the map.** The next row is the first one in the pinned map's order that is unassigned and
  has no open blocker: the frontier query under *Wayfinding operations*, run over the map's rows.
  Blocking edges are native issue dependencies (same section); a row whose blocker is open waits.
- **One row, one branch, one PR.** The branch is named after the row (`lh-476-management-routes`), is
  short-lived and rebased onto `main` daily, and merges through a PR the other people can see, so new
  work starts from merged code rather than from someone's local branch.
- **One open row per defect.** A row found to describe the same defect as another is closed with the
  `duplicate` label and a comment naming the survivor, and its evidence is copied onto the survivor. A
  row that a spec ticket fixes stays its own row, gets a `blocked_by` edge on that ticket, and the ticket
  gets a comment naming it, so it closes together and is not built twice.
- **Triage the rows you did not write.** A row opened by someone else, an audit or a finding enters the
  map only through `/triage` with the owner's decisions (see `triage-labels.md`); every triage comment
  starts with the triage disclaimer.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

Infer the repo from `git remote -v`; `gh` does this automatically when run inside a clone.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

When set to `yes`, PRs run through the same labels and states as issues, using the `gh pr` equivalents:

- **Read a PR**: `gh pr view <number> --comments` and `gh pr diff <number>` for the diff.
- **List external PRs for triage**: `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments` then keep only `authorAssociation` of `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE` (drop `OWNER`/`MEMBER`/`COLLABORATOR`).
- **Comment / label / close**: `gh pr comment`, `gh pr edit --add-label`/`--remove-label`, `gh pr close`.

GitHub shares one number space across issues and PRs, so a bare `#42` may be either: resolve with `gh pr view 42` and fall back to `gh issue view 42`.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single issue with **child** issues as tickets.

- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `gh issue create --label wayfinder:map`.
- **Child ticket**: an issue linked to the map as a GitHub sub-issue (`gh api` on the sub-issues endpoint). Where sub-issues aren't enabled, add the child to a task list in the map body and put `Part of #<map>` at the top of the child body. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: GitHub's **native issue dependencies**, the canonical, UI-visible representation. Add an edge with `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`, where `<blocker-db-id>` is the blocker's numeric **database id** (`gh api repos/<owner>/<repo>/issues/<n> --jq .id`, _not_ the `#number` or `node_id`). GitHub reports `issue_dependencies_summary.blocked_by` (open blockers only, the live gate). Where dependencies aren't available, fall back to a `Blocked by: #<n>, #<n>` line at the top of the child body. A ticket is unblocked when every blocker is closed.
- **Frontier query**: list the map's open children (`gh issue list --state open`, scoped to the map's sub-issues / task list), drop any with an open blocker (`issue_dependencies_summary.blocked_by > 0`, or an open issue in the `Blocked by` line) or an assignee; first in map order wins.
- **Claim**: `gh issue edit <n> --add-assignee @me`, the session's first write.
- **Resolve**: `gh issue comment <n> --body "<answer>"`, then `gh issue close <n>`, then append a context pointer (gist + link) to the map's Decisions-so-far.
