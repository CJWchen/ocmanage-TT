# Docker Instance Create Shape

## Sources checked

* Local OpenClaw docs: `~/.npm-global/lib/node_modules/openclaw/docs/install/docker.md`
* Existing live runtime: `~/.openclaw-designer/.openclaw-runtime.json`
* Existing compose sample: `~/.openclaw-designer-docker/docker-compose.yml`
* Existing control script: `~/.local/bin/openclaw-designer-docker-service`

## Key observations

* This repo already knows how to **read** Docker-backed instances via `.openclaw-runtime.json`, inspect Docker containers, and repair Docker-specific drift.
* A real Docker-backed instance in this environment uses four coordinated artifacts:
  * host state dir: `~/.openclaw-<profile>`
  * compose dir: `~/.openclaw-<profile>-docker`
  * control script: `~/.local/bin/openclaw-<profile>-docker-service`
  * runtime meta: `~/.openclaw-<profile>/.openclaw-runtime.json`
* The compose file publishes `127.0.0.1:<port>:<port>`, sets:
  * `HOME=/home/node`
  * `OPENCLAW_STATE_DIR=/home/node/.openclaw`
  * `OPENCLAW_CONFIG_PATH=/home/node/.openclaw/openclaw.json`
  * `OPENCLAW_DISABLE_BONJOUR=1`
  * host bridge env vars pointing at `host.docker.internal:<bridge-port>`
* The compose command runs the gateway directly:
  * `node openclaw.mjs gateway --allow-unconfigured --bind lan --port <port>`
* Existing Docker instances keep the same `systemd --user` service naming convention as host-managed instances, but swap the override to a Docker control script.
* The control script is a thin `docker compose` wrapper supporting:
  * `run`
  * `stop`
  * `ps`
  * `logs`
  * `pull`

## Implications for implementation

* `create` must branch by runtime type:
  * `docker`: create all Docker artifacts directly and enable/start the service through the Docker override path
  * `host`: keep delegating to `openclaw-instance`, but only if there is no existing host-managed instance
* The repo should enforce the host-instance singleton rule in backend code, not only in UI.
* Frontend can infer whether host creation is allowed from `summary.instances[*].runtimeMode`, but backend must still reject invalid requests.
* Docker should be the default create mode when the payload omits the runtime type.

## Suggested low-risk implementation shape

1. Add a dedicated Docker creation module, separate from `host_managed.py`.
2. Reuse current path conventions:
   * state dir: `~/.openclaw-<profile>`
   * compose dir: `~/.openclaw-<profile>-docker`
   * control script: `~/.local/bin/openclaw-<profile>-docker-service`
3. Initialize config/state without going through `openclaw-instance create`, so the flow is not mistakenly treated as a host-managed instance.
4. Install the base service with `openclaw gateway install --force --port <port>` for the profile, then replace the override with the Docker control script.
5. Write `.openclaw-runtime.json` before the first service start so the manager can immediately classify the instance as Docker-backed.
