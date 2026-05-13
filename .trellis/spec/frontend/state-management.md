# State Management

> How state is managed in this project.

---

## Overview

<!--
Document your project's state management conventions here.

Questions to answer:
- What state management solution do you use?
- How is local vs global state decided?
- How do you handle server state?
- What are the patterns for derived state?
-->

(To be filled by the team)

---

## State Categories

<!-- Local state, global state, server state, URL state -->

(To be filled by the team)

---

## When to Use Global State

<!-- Criteria for promoting state to global -->

(To be filled by the team)

---

## Server State

For the current static HTML manager:

- `app.detailByProfile` is the source of truth for per-instance server data.
- Any server-backed form draft derived from instance config must be regenerated
  from the latest detail payload after an explicit reload or refetch.
- Do not keep stale per-profile drafts alive across refreshes when the UI is
  supposed to reflect the persisted config on disk.
- Long-running interactive backend state, such as a Feishu QR terminal session,
  must live in a separate per-profile server-state cache and be refreshed by
  polling instead of being inferred from config text.
- When a polled per-profile session state says writes are locked, all UI actions
  that mutate that same profile must disable immediately instead of letting the
  user click into a guaranteed backend rejection.
- When a polled interactive session exits, the UI must refetch the latest
  instance detail/config for that profile before regenerating any server-backed
  drafts, because the underlying CLI flow may have changed the config on disk.

---

## Common Mistakes

<!-- State management mistakes your team has made -->

- Keeping mutation buttons enabled after the backend has entered a per-profile
  interactive lock state, which creates noisy errors and stale assumptions in
  the UI.
- Updating only a narrow panel's local state after polling, while leaving the
  surrounding profile header/detail views stale.
