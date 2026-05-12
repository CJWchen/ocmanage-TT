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

---

## Common Mistakes

<!-- State management mistakes your team has made -->

(To be filled by the team)
