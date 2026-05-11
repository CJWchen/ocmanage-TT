# Quality Guidelines

> Code quality standards for backend development.

---

## Overview

This backend is a small Python control plane for local processes, systemd user
services, and Docker-backed OpenClaw instances. Quality work here is mostly
about keeping cross-layer contracts explicit:

- API payloads must stay stable enough for the static HTML frontend.
- Runtime classification must not silently drift between `docker` and
  `systemd`.
- Infra mutations must either finish coherently or roll back their own
  artifacts.

---

## Scenario: OpenClaw Instance Create Modes

### 1. Scope / Trigger

- Trigger: `POST /api/openclaw/action` with `{"action":"create", ...}`
- Why this needs code-spec depth:
  - The request crosses frontend form state, backend routing, local OpenClaw
    CLI, systemd user services, Docker Compose artifacts, and runtime metadata.
  - The default create mode is part of product behavior, not an implementation
    detail.

### 2. Signatures

- API entry:
  - `POST /api/openclaw/action`
- Create payload:
  - `action: "create"`
  - `profile: string`
  - `port?: number | string | null`
  - `runtimeMode?: "docker" | "host" | alias`
- Backend split:
  - `manager_tt_backend.actions.create_instance(payload)`
  - `manager_tt_backend.create_modes.resolve_create_mode(payload)`
  - `manager_tt_backend.host_managed.create_instance_via_host_manager(payload)`
  - `manager_tt_backend.docker_managed.create_instance_via_docker_manager(payload)`

### 3. Contracts

- Default behavior:
  - Missing `runtimeMode` means **Docker create**, not host create.
- Accepted create-mode aliases:
  - Docker: `docker`, `container`
  - Host: `host`, `local`, `systemd`, `default`, `host-managed`,
    `host_managed`
- API-visible runtime normalization:
  - Docker-backed instances are exposed as `runtimeMode="docker"`
  - Host-managed instances are exposed as `runtimeMode="systemd"`
- Host singleton rule:
  - If any existing instance is not Docker-backed, backend must reject creation
    of another host-managed instance.
- Delete with preserved state:
  - `removeStateDir=false` must still retire the profile from discovery by
    archiving `openclaw.json` and related runtime-discovery metadata out of
    their canonical filenames.
- Docker create artifact contract:
  - State dir: `~/.openclaw-<profile>`
  - Compose dir: `~/.openclaw-<profile>-docker`
  - Control script: `~/.local/bin/openclaw-<profile>-docker-service`
  - Runtime meta: `~/.openclaw-<profile>/.openclaw-runtime.json`
  - systemd unit name remains `openclaw-gateway-<profile>.service`
  - Docker override must replace the unit start/stop commands
- Docker session-path diagnostics:
  - Path-safety checks must honor the instance's actual bind mounts, including
    preserved same-path mounts declared in `docker-compose.yml`.

### 4. Validation & Error Matrix

- `profile == "default"` with `action=create` -> reject with `ValueError`
- Unknown create mode -> reject with `ValueError("未知创建模式: ...")`
- Host create while any host-managed instance exists -> reject with a message
  that names the blocking profiles
- Requested port `<= 0` or `> 65535` -> reject
- Requested explicit Docker port already bound on the host -> reject before any
  config/compose/systemd artifacts are written
- Existing profile config path already present -> reject
- Docker create step fails after writing artifacts -> roll back newly written
  config/state/compose/override/runtime-meta/token/script artifacts before
  surfacing the error
- Delete with `removeStateDir=false` -> preserve user data, but archive
  `openclaw.json` and `.openclaw-runtime.json` so manager discovery and
  `openclaw-gateway-supervised` no longer treat the profile as active
- `removeStateDir=true` on delete for a Docker-backed profile -> remove Docker
  side artifacts as well, not only the state dir

### 5. Good / Base / Bad Cases

- Good:
  - Frontend omits `runtimeMode`; backend creates a Docker-backed instance.
  - Frontend sends `runtimeMode="host"` and the workspace has zero host-managed
    instances; backend delegates to the host-managed path.
- Base:
  - Runtime metadata stores `container`; listing still normalizes it back to
    API-visible `docker`.
  - Config parsing fails for a Docker-backed profile; instance listing still
    keeps Docker classification instead of falling back to host.
- Bad:
  - Routing every create request through `openclaw-instance create`
  - Treating a Docker alias like `container` as host-managed in list/create
    policy code
  - Deleting a profile but leaving `~/.openclaw-<profile>/openclaw.json` in
    place, so discovery and port-policy scans still count it
  - Leaving compose/script/token/unit leftovers after a failed Docker create

### 6. Tests Required

- Unit tests for create-mode resolution:
  - default create mode is Docker
  - host aliases map to host
  - Docker aliases map to Docker
- Unit tests for host singleton enforcement:
  - reject when any non-Docker instance exists
  - allow when only Docker instances exist
- Unit tests for Docker pure logic:
  - runtime meta keeps preserved workspace paths
  - port suggestion respects the managed port series
  - explicit occupied port is rejected before Docker create writes artifacts
- Rollback tests:
  - simulate service start failure and assert Docker create removes all newly
    written artifacts
- Listing tests:
  - config-read failure must not erase Docker classification
  - session path diagnostics must not flag a host path when compose preserves
    that same path inside the container
- Delete tests:
  - Docker-specific artifacts must be included in cleanup when removing state
  - Preserved-state delete must archive discovery files and make the profile
    disappear from future instance scans

### 7. Wrong vs Correct

#### Wrong

- Frontend assumes Docker is default, but backend falls back to host-managed
  create when `runtimeMode` is missing.
- Docker create writes compose/runtime-meta/override files, then leaves them
  behind if `systemctl start` fails.
- Explicit-port Docker create writes config/compose files first and only then
  fails on the container bind error.
- Instance policy checks compare raw runtime strings instead of canonicalized
  values.
- Session diagnostics assume Docker only exposes `/home/node/.openclaw`, even
  when compose also bind-mounts the host path to itself.

#### Correct

- Backend owns the default: missing mode resolves to Docker in
  `resolve_create_mode()`.
- Docker create is isolated in `docker_managed.py` and rolls back its own
  artifacts on failure.
- Explicit-port Docker create checks the requested host port before writing any
  managed artifacts.
- Runtime labels are canonicalized before policy decisions, and normalized back
  to `docker`/`systemd` in API responses.
- Docker session diagnostics derive safe-path aliases from compose/runtime
  mounts before flagging host-path residue.

---

## Forbidden Patterns

- Do not add new instance-create behavior directly inside the HTTP handler.
  Route it through `actions.py` and the dedicated create-mode modules.
- Do not infer host/Docker policy from raw strings without canonicalization.
- Do not mutate systemd, compose, or runtime-meta artifacts in separate modules
  without a single create path owning rollback behavior.

---

## Required Patterns

- Keep host-managed and Docker-managed create flows in separate modules.
- Canonicalize create/runtime mode aliases before policy checks.
- Add regression tests for every new create-mode branch or cleanup path.

---

## Testing Requirements

- Any change to create/delete/runtime-mode policy requires unit coverage.
- Safe smoke checks should verify at least:
  - `GET /cgi-bin/status`
  - `GET /api/openclaw/summary`
  - one authenticated Docker bridge route when bridge logic changes
- Do not run destructive live create/delete flows on the developer machine just
  to prove routing.

---

## Code Review Checklist

- Is Docker still the backend default when create mode is omitted?
- Are host singleton checks enforced server-side, not only in the UI?
- Are explicit requested ports checked for host listeners before Docker create
  writes artifacts?
- If Docker create fails midway, are all newly created artifacts cleaned up?
- If delete preserves the state dir, does it still remove the profile from
  discovery and future port-conflict scans?
- Are runtime aliases normalized consistently in policy and listing paths?
- Do delete flows clean up Docker-specific artifacts when removing state?
