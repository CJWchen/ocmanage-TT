# Refactor OpenClaw Manager Backend Structure

## Goal

在已经完成后端 package 拆分的基础上，把 OpenClaw“新建实例”真正改成双模式创建：
- 默认创建 Docker 实例
- 允许显式选择创建本机实例
- 但当宿主机已经存在任意本机实例时，禁止再创建新的本机实例

同时保持现有启动方式和 API 基本兼容，不引入新的前端框架或构建链。

## What I already know

* 后端已经完成 package 拆分，当前入口是 `launcher_server.py -> manager_tt_backend/server.py`。
* 当前 `create_instance()` 仍直接走宿主机 `openclaw-instance create`。
* `openclaw-instance` 脚本会写 `~/.openclaw-*` 状态目录、安装/刷新 `systemd --user` service，并启动宿主机实例。
* 项目已经具备“识别 Docker runtime / 展示 Docker 状态 / 修复 Docker 漂移”的逻辑，但还没有“创建 Docker 实例”的实现入口。
* 现有真实 Docker 实例（如 `designer`）使用：
  * `~/.openclaw-<profile>-docker/docker-compose.yml`
  * `~/.local/bin/openclaw-<profile>-docker-service`
  * `~/.openclaw-<profile>/.openclaw-runtime.json`
  * `systemd --user` service + Docker override
* 前端当前创建区只有 `profile + port + create`，没有“创建类型”选择。

## Assumptions

* “本机实例只能有一个”按宿主机实例总数判断，不区分 profile；只要已有任意 `runtimeMode != docker` 的实例，就拒绝再创建新的本机实例。
* 这个限制只作用于“新建本机实例”，不追溯删除或自动迁移已有实例。
* Docker 创建优先复用现有 `designer`/`fapiao` 的运行结构：compose + control script + runtimeMeta + systemd override。

## Open Questions

* Docker 新建时是否要同时生成容器内桥接工具文件；当前代码没有直接消费它，但现有实例保留了该文件。

## Requirements

* `/api/openclaw/action { action:create }` 必须支持创建类型选择，未显式指定时默认按 Docker 创建。
* 新建 Docker 实例时，后端要真正落地：
  * profile state dir
  * docker compose 目录和 `docker-compose.yml`
  * control script
  * runtimeMeta
  * 需要的 token / bridge 配置
  * systemd service + Docker override
* 新建本机实例时仍可走现有 host-managed 流程，但必须在后端校验：
  * 如果已存在任意本机实例，则拒绝创建新的本机实例
* 前端创建区必须允许明确选择：
  * `Docker 实例`
  * `本机实例`
* 前端默认选中 `Docker 实例`。
* 当检测到已有本机实例时，前端应禁用 `本机实例` 选项，并给出明确提示。
* 现有 API 路径、实例列表刷新、创建后自动选中实例等体验保持可用。
* 不处理 UI 整体视觉重做，不引入新的前端框架。

## Acceptance Criteria

* [ ] 创建表单默认选择 `Docker 实例`
* [ ] 创建表单可切换 `Docker` / `本机`
* [ ] 若已存在本机实例，前端禁用 `本机实例` 选项
* [ ] 后端即使绕过前端也会拒绝第二个本机实例
* [ ] Docker 创建后，实例能以 `runtimeMode=docker` 出现在列表和详情中
* [ ] `launcher_server.py` 入口和现有 API 仍可正常启动与访问

## Definition of Done

* Docker / host 两种创建路径都能被代码明确区分
* 默认创建路径已切到 Docker
* 至少完成导入/语法验证、接口 smoke test、创建约束测试
* 如果仍有未自动化验证的环节，在交付说明里明确指出

## Out of Scope

* UI 整体视觉或布局重做
* 引入前端打包器、TypeScript、React 等新技术栈
* 清理历史上已存在的多本机实例
* 把所有旧实例自动迁移到 Docker

## Technical Notes

* 关键文件：
  * `launcher_server.py`
  * `manager_tt_backend/actions.py`
  * `manager_tt_backend/instances.py`
  * `manager_tt_backend/host_managed.py`
  * `openclaw_manager.html`
* 当前缺少完整 lint/type-check 工具配置，但已有 `tests/test_instances.py`。
* `openclaw-instance` 是当前 host-managed 实例创建的真实后端。
* Docker runtime 识别依赖每个 profile 状态目录中的 `.openclaw-runtime.json`。
* 参考样本：
  * `~/.openclaw-designer/.openclaw-runtime.json`
  * `~/.openclaw-designer-docker/docker-compose.yml`
  * `~/.local/bin/openclaw-designer-docker-service`
