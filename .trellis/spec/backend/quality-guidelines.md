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
- JSON flag fields must not be coerced with bare `bool(value)` because string
  payloads like `"false"` become truthy and silently flip behavior.

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

## Scenario: Tencent Coding Plan Model Module Injection

### 1. Scope / Trigger

- Trigger: `POST /api/openclaw/config/tencent-coding-plan`
- Why this needs code-spec depth:
  - The request crosses frontend draft state, backend validation, on-disk
    `openclaw.json`, optional provider probe, and optional service restart.
  - The module is intentionally **partial-write** behavior: it owns only the
    Tencent/OpenAI-related config fragment and must preserve unrelated
    instance-specific settings.

### 2. Signatures

- API entry:
  - `POST /api/openclaw/config/tencent-coding-plan`
- Request body:
  - `profile: string`
  - `apiKey: string`
  - `primaryModel: one of TENCENT_PRIMARY_MODELS`
  - `dryRun?: boolean | "true" | "false" | 0 | 1`
  - `restartAfterSave?: boolean | "true" | "false" | 0 | 1`
  - `probeAfterApply?: boolean | "true" | "false" | 0 | 1`
- Backend split:
  - `manager_tt_backend.server.ExecHandler._handle_openclaw_tencent_model_module`
  - `manager_tt_backend.actions.apply_tencent_model_module`
  - `manager_tt_backend.model_modules.apply_tencent_model_package`
  - `manager_tt_backend.model_modules.probe_tencent_model_package`

### 3. Contracts

- Request body must be a JSON object; arrays / scalars are invalid.
- The backend-owned write scope is limited to:
  - `models.mode`
  - `models.providers.tencent-coding-plan`
  - `agents.defaults.model.primary`
  - `agents.defaults.models.tencent-coding-plan/*`
  - `plugins.entries.openai.enabled`
  - `plugins.allow` append-only when the list already exists
- Unrelated config must be preserved exactly, including fields like:
  - `gateway.port`
  - `agents.defaults.workspace`
  - other providers and plugins
  - instance-local metadata / hooks / channels
- `dryRun=true` means:
  - no file write
  - no backup creation
  - no restart
  - response `status="preview"`
- `probeAfterApply=true` means:
  - dry-run may still call the remote provider probe
  - probe failure does **not** erase a successful config apply
- Restart behavior:
  - restart runs only after write + validate succeed
  - restart failure returns `status="failed"` and reports that config was saved
- Success status contract:
  - `preview` -> dry-run success
  - `applied` -> write/validate succeeded and probe is absent or ok
  - `applied_with_probe_failure` -> write/validate succeeded, probe failed

### 4. Validation & Error Matrix

- Request body is not an object -> `400` with `json body 必须是对象`
- `apiKey` missing / blank -> reject
- `primaryModel` outside the fixed allowlist -> reject
- Boolean-like flags parsed with bare `bool(value)` -> forbidden because
  `"false"` would incorrectly become truthy
- Config file path does not exist -> reject before any write
- `dryRun=true` -> do not write or restart
- Persisted config validate command returns non-zero -> restore original bytes,
  return `status="failed"`, set `rollbackPerformed=true`
- Probe HTTP/network failure after a valid apply -> return
  `status="applied_with_probe_failure"`
- Restart failure after a valid apply -> return `status="failed"` without
  rolling back the validated config file

### 5. Good / Base / Bad Cases

- Good:
  - User previews Tencent config on `designer`; response shows a masked preview
    fragment and the file on disk stays unchanged.
  - User applies Tencent config; backend preserves the existing workspace,
    gateway port, and non-Tencent providers.
- Base:
  - Existing `plugins.allow` already contains `feishu` and `qwen`; backend only
    appends `openai`.
  - Existing `agents.defaults.models` contains stale Tencent entries; backend
    replaces only the Tencent-prefixed entries and leaves non-Tencent models.
- Bad:
  - Replacing the entire `models.providers` map and deleting other providers
  - Resetting `agents.defaults.workspace` or gateway settings while injecting
    the model package
  - Treating probe failure as if the write itself failed
  - Using string `"false"` in JSON and accidentally triggering write/restart
  - Keeping a stale frontend draft after reloading the instance detail from the
    server

### 6. Tests Required

- Unit tests for module merge:
  - preserves unrelated config blocks
  - removes stale Tencent model aliases while preserving non-Tencent aliases
  - appends `openai` to `plugins.allow` only when the allowlist exists
- Unit tests for apply flow:
  - dry-run does not write or back up
  - validate failure rolls back original file bytes
  - probe failure maps to `applied_with_probe_failure`
  - string boolean fields like `"false"` remain false
- Handler tests:
  - reject non-object JSON payloads before reaching action logic
- Frontend regression expectation:
  - server-derived Tencent draft state must be rebuilt after config/detail
    reload so preview/apply reflects persisted config

### 7. Wrong vs Correct

#### Wrong

- The backend uses `bool(payload["dryRun"])`, so `"false"` becomes true/false
  unpredictably depending on caller type.
- The apply path rewrites `openclaw.json`, then leaves the broken file on disk
  after validation fails.
- The merge helper overwrites all provider/plugin state instead of only the
  Tencent-owned fragment.
- The frontend keeps an old Tencent draft after a refetch and shows stale API
  key / model data.

#### Correct

- Boolean flags are normalized explicitly from booleans, `0/1`, and string
  forms before control-flow decisions.
- The apply path writes a backup, validates the persisted file, and restores the
  original bytes on validation failure.
- The merge helper owns only the Tencent/OpenAI fragment and preserves other
  instance-specific config.
- Frontend refetch paths rebuild server-derived drafts from the latest instance
  detail payload.

---

## Scenario: Feishu Channel Injection + QR Session Management

### 1. Scope / Trigger

- Trigger A: `POST /api/openclaw/config/feishu-channel`
- Trigger B:
  - `POST /api/openclaw/feishu/qr/start`
  - `GET /api/openclaw/feishu/qr/status`
  - `POST /api/openclaw/feishu/qr/input`
  - `POST /api/openclaw/feishu/qr/stop`
- Why this needs code-spec depth:
  - The feature mixes partial config mutation with an interactive PTY-backed
    CLI session.
  - Feishu credentials already exist in multiple config shapes and must not be
    silently migrated.
  - A live QR session must temporarily lock config-mutating actions for that
    same profile.

### 2. Signatures

- Feishu config injection request:
  - `profile: string`
  - `appId: string`
  - `appSecret: string`
  - `accountId?: string`
  - `dryRun?: boolean | "true" | "false" | 0 | 1`
  - `restartAfterSave?: boolean | "true" | "false" | 0 | 1`
- QR start request:
  - `profile: string`
  - `verbose?: boolean | "true" | "false" | 0 | 1`
- QR input request:
  - `profile: string`
  - `input: string`
- Backend split:
  - `manager_tt_backend.actions.apply_feishu_channel_module`
  - `manager_tt_backend.feishu_modules.apply_feishu_channel_package`
  - `manager_tt_backend.feishu_qr_sessions.start_feishu_qr_session`
  - `manager_tt_backend.feishu_qr_sessions.get_feishu_qr_session_status`
  - `manager_tt_backend.feishu_qr_sessions.send_feishu_qr_input`
  - `manager_tt_backend.feishu_qr_sessions.stop_feishu_qr_session`

### 3. Contracts

- Feishu config injection owns only the Feishu channel credential fragment and
  must preserve unrelated model/gateway/workspace/plugin state.
- Credential-shape preservation:
  - If the profile already uses top-level `channels.feishu.appId/appSecret`,
    keep writing that shape.
  - If the profile already uses `channels.feishu.accounts.<id>`, keep writing
    the account shape and update `defaultAccount`.
  - Do not auto-migrate one shape into the other during injection.
- Validated write contract:
  - raw config save and config-module injection must both use the same
    write -> validate -> rollback-on-failure flow.
- QR session contract:
  - one active Feishu QR session per profile maximum
  - QR session command must include the target profile when profile is not
    `default`
  - QR status is process-local manager state, not inferred from disk
- Locking contract:
  - while a profile has an active Feishu QR session, backend must reject
    profile-scoped config mutations for that same profile, including:
    `save_config`, `ensure`, `doctor repair`, `delete`, and config modules
- Session isolation:
  - a QR session for `designer` must not block or mutate `fapiao`, `default`,
    or any other profile

### 4. Validation & Error Matrix

- `appId` missing / blank -> reject
- `appSecret` missing / blank -> reject
- illegal `accountId` characters -> reject
- QR `input` missing / empty -> reject
- QR start on a profile with no config file -> reject before launching PTY
- second QR start while the same profile already has an active session ->
  reject
- config validate returns non-zero after Feishu injection or raw save ->
  restore original file bytes, return `status="failed"`, do not restart
- any protected mutation attempted during active QR session -> reject with a
  profile-scoped lock error
- boolean-like flags parsed via bare `bool(value)` -> forbidden because
  `"false"` becomes truthy

### 5. Good / Base / Bad Cases

- Good:
  - `designer` starts a Feishu QR session; `designer` config-save buttons lock,
    but `fapiao` remains operable.
  - A top-level Feishu config receives injected credentials and keeps existing
    `dmPolicy` / `groupPolicy` untouched.
  - An account-mode Feishu config updates the active account credentials without
    deleting sibling accounts.
- Base:
  - QR session exits; next refetch updates instance detail and unlocks write
    operations.
  - Raw config save fails validation; backend returns failure and leaves the
    original config on disk.
- Bad:
  - Launching a QR login against the default profile because `--profile` was
    omitted for a named instance.
  - Converting an account-mode Feishu config into top-level credentials during
    injection.
  - Letting `delete` or `ensure` run against a profile that still has an active
    QR session.
  - Treating sanitized terminal output as authoritative config state instead of
    reloading from disk after session exit.

### 6. Tests Required

- Unit tests for Feishu module merge:
  - top-level credential shape is preserved
  - account-based credential shape is preserved
  - sibling Feishu accounts remain intact
- Apply/save tests:
  - dry-run does not write
  - validation failure rolls back bytes and skips restart
  - string boolean flags like `"false"` remain false
- Action/API tests:
  - QR start flag coercion uses explicit boolean normalization
  - QR lock blocks delete / ensure / save and config module writes
- Utility tests:
  - QR command includes `--profile` only for non-default profiles
  - terminal sanitizer removes ANSI control sequences

### 7. Wrong vs Correct

#### Wrong

- A named instance starts QR login without `--profile`, so the default profile
  is mutated instead.
- Raw config save and module injection each implement different validation and
  rollback behavior.
- Backend locks only the Feishu module endpoint, but leaves delete/ensure paths
  writable during an active QR session.
- Injection rewrites Feishu config shape instead of preserving the instance's
  current credential layout.

#### Correct

- QR session launch derives the command from the target profile and isolates the
  PTY session by profile key.
- All config writes use the same validated persistence helper and roll back on
  validation failure.
- Profile-scoped mutation paths consult the Feishu QR lock before mutating
  config/runtime state.
- Feishu injection updates only the active credential shape already used by that
  instance.

---

## Forbidden Patterns

- Do not add new instance-create behavior directly inside the HTTP handler.
  Route it through `actions.py` and the dedicated create-mode modules.
- Do not infer host/Docker policy from raw strings without canonicalization.
- Do not mutate systemd, compose, or runtime-meta artifacts in separate modules
  without a single create path owning rollback behavior.
- Do not parse JSON flag fields with bare `bool(value)` when the API accepts
  string/number forms.
- Do not implement raw config saves with a different write/validate/rollback
  flow than config-module writes.
- Do not allow profile-scoped config mutations to bypass an active Feishu QR
  session lock.

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
- Config-module changes should also cover:
  - dry-run smoke against the live HTTP endpoint
  - rollback and probe-status behavior in unit tests
- Interactive PTY-backed flows should also cover:
  - profile isolation for the spawned command
  - lock behavior for conflicting mutations
  - terminal output sanitization in pure unit tests
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
- Are JSON boolean flags normalized explicitly instead of `bool(value)`?
- Does config-module write scope stay limited to the documented Tencent-owned
  fragment?
- Do raw config saves and config-module writes share the same validation +
  rollback contract?
- Does an active Feishu QR session block all same-profile mutations that could
  race with the interactive CLI flow?
