# 从 AstrBot 到 PHOEBE：可借鉴的工程治理与演进路线

> 本文是一份面向 PHOEBE 的架构评估与实施路线图。它研究 AstrBot 已验证的工程治理思路，但不把 PHOEBE 改造成聊天平台，也不建议复制 AstrBot 的实现。目标是在保持硬件控制边界的前提下，提高安全性、可恢复性、可复现性和可诊断性。

| 项目 | 内容 |
| --- | --- |
| 文档状态 | 提案；除“当前基线”外，文中设计均未实现 |
| 最后核验 | 2026-07-10 |
| 核验基线 | PHOEBE `4703ff0` 加当前工作区；AstrBot `12f2f5a09` |
| 权威来源 | 当前代码是行为事实；[refactor.md](refactor.md) 是目标架构规范 |
| 适用范围 | 新平台 [phoebe/](phoebe/)；`awg5204/` 与 `TPA_experiment/` 仍是只读迁移参考 |

## 执行摘要

PHOEBE 已经有一套适合实验室硬件的核心骨架：严格契约、Gateway 指令入口、TaskManager、原子设备租约、Controller 安全语义、每个 run 一个 RunWriter、仿真后端，以及 PyQt/asyncio 线程隔离。当前最需要补强的不是另一套前端或插件市场，而是核心周围的治理能力。

建议按以下顺序演进：

1. **先封住运行时风险**：维护模式、持久化 `command_id` 幂等、run 生命周期日志、受控 shutdown 与崩溃恢复测试。
2. **再固化扩展契约**：`PluginManifest`、类型化准入结果、兼容性责任边界和可执行的架构门禁。
3. **再建设可复现资产**：明确 Profile 的领域模型，实现安全、可预检、不可执行的 Profile Bundle。
4. **补齐离线回归和运维视图**：真实协议回放、插件合规测试、本地 Run Catalog、诊断面板和脱敏支持包。
5. **Web/API 保持条件式项目**：只有出现多人、跨机器或无头运行需求，并具备身份、权限、审计和网络隔离时才启动。

这个顺序把硬件安全和事故可追溯性放在便捷功能之前，也避免在基础契约尚未稳定时过早增加远程攻击面。

## 1. 已验证的当前基线

下表只描述当前代码，不把 [refactor.md](refactor.md) 中尚未落地的规则写成既有能力。

| 领域 | 当前已有 | 已确认的缺口 |
| --- | --- | --- |
| 边界契约 | [contracts.py](phoebe/core/contracts.py) 提供严格、不可变的 `ContractModel` 和 `validate_boundary()` | 还没有面向准入失败、Bundle、持久化恢复的专用契约 |
| 指令路径 | [gateway.py](phoebe/core/gateway.py) → [task_manager.py](phoebe/core/task_manager.py)，payload 在 dispatch 中按插件 Config 校验 | `command_id` 没有去重或持久化；拒绝原因仍是自由文本 |
| 插件注册 | [plugin.py](phoebe/core/plugin.py) 的 `PluginSpec` 保存 plugin id、command、入口和 Config 类型 | 没有插件版本、API 兼容范围、产物类型、manifest hash 或可用性报告 |
| 依赖与资源 | [di.py](phoebe/core/di.py) 按 handler 类型标注和 role 解析设备；[device_manager.py](phoebe/core/device_manager.py) 原子获取 lease | 没有统一的准入阶段，也没有校准有效期、health freshness、维护策略的类型化判定 |
| run 生命周期 | `TaskManager._records` 和队列保存在内存；pause/cancel/cleanup 路径已存在 | 崩溃后状态丢失；执行结果在 cleanup 前进入 terminal；cleanup 降级不会改变执行结果 |
| 进程关闭 | [bootstrap.py](phoebe/app/bootstrap.py) 依次停止 suspenders、DeviceManager 和 workers | 没有总的 `TaskManager.shutdown()`，也没有“拒绝新任务 → cancel → 等待清理 → 安全态”的协议 |
| 数据与复现 | [writer.py](phoebe/core/writer.py) 为每个 run 写 `run.json`、baseline、JSONL、HDF5 和可选 Parquet；manifest 已含实验 Config 全文、AppConfig hash、git 状态和仪器 identity/options | manifest 不含完整 AppConfig、plugin version 或资产来源，`code_version` 当前未赋值 |
| 回放与仿真 | [mock.py](phoebe/transports/mock.py) 已有 `MockScpiTransport` 与 `TranscriptReplayTransport`；L4 sim 测试已存在 | 仓库目前没有真实 transcript fixture，也没有 L2 replay test |
| UI 与日志 | [events.py](phoebe/core/events.py) 有 `LogEvent`，[bridge.py](phoebe/ui/bridge.py) 和 [main_window.py](phoebe/ui/main_window.py) 已能显示事件 | 生产日志只写 run 的 `experiment.jsonl`；还没有 loguru → `LogEvent` 的受限 sink |
| 工程门禁 | 当前测试覆盖契约、bus/worker、lease/DI、L1 driver 和 L4 sim | 根项目没有 CI workflow，也没有 Ruff、Pyright、import-linter 配置；架构边界主要靠约定 |

两个措辞必须保持准确：

- RunWriter 是“**每个 run 一个唯一写者**”，不是整个进程只有一个 RunWriter。
- EventBus 的 retained 状态是每个 topic 的最新观测值，不是持久化运行记录，也不是可靠命令队列。

## 2. 借鉴边界与不可破坏的约束

### 2.1 许可证与来源边界

PHOEBE 根目录的 [LICENSE](LICENSE) 是 MPL-2.0，但根 [pyproject.toml](pyproject.toml) 尚未声明 license 元数据；AstrBot 后端的 [pyproject.toml](AstrBot/pyproject.toml) 与 [LICENSE](AstrBot/LICENSE) 标注 AGPL-3.0-or-later；AstrBot Dashboard 目录另有 [MIT LICENSE](AstrBot/dashboard/LICENSE)。许可证是按文件和来源判断的，不能用一个标签概括整个嵌套目录。

因此本文采用以下工程策略：

- 可以研究模块划分、状态机、备份协议、测试方法和 UI 信息架构。
- 默认不复制 AstrBot 的 Python、Vue、TypeScript、模板、样式或大段实现。
- 如未来确实需要复用代码或资产，先确认具体文件的版权与许可证，再做正式合规评估并保留来源记录。
- 本节是工程边界，不替代法律意见。

AstrBot 的备份实现值得参考，但要准确描述：exporter 会在 manifest 中写 SHA-256；importer 有版本预检和路径包含检查，却没有验证这些 checksum。PHOEBE 应把强制 checksum 校验作为**改进项**，而不是宣称 AstrBot 已完整实现。

### 2.2 PHOEBE 的架构不变量

后续工作必须继续满足：

1. UI 保持 PyQt5；是否引入 Web 是独立、条件式决策。
2. 运行控制流保持 `UI → Gateway → TaskManager → plugin → capability/controller`。
3. 静态依赖边界与运行控制流分开描述；plugin 只依赖 `RunContext`、领域契约和 capability Protocol，不 import Driver 或 UI。
4. 指令、观测和大数据仍走三条不同通道：Gateway、EventBus、RunWriter。
5. Profile 预检、Run Catalog 扫描、支持包导出均不得连接仪器或执行任意代码。
6. real run 崩溃后绝不自动 resume 或重放硬件命令。
7. `awg5204/` 与 `TPA_experiment/` 不得被 `phoebe/` import。

## 3. 风险与路线图

“优先级”表示风险顺序，“规模”只表示相对工程量，不是工期承诺。

| 阶段 | 工作包 | 解决的风险 | 依赖 | 规模 |
| --- | --- | --- | --- | --- |
| P0 | S1：维护模式、Command Ledger、持久化幂等 | 双击/重试启动重复硬件动作；shutdown 时仍接收任务 | 无 | M |
| P0 | S2：Run Journal、finalization 语义、受控 shutdown | 崩溃上下文丢失；cleanup 未完成却显示成功 | S1 | L |
| P0 | F1：PluginManifest、类型化 admission、架构门禁 | 插件和设备不兼容直到运行期才暴露；边界只靠约定 | 可与 S1 并行 | M |
| P1 | R1：Profile/Calibration 模型与安全 Bundle | 配置、LUT、校准资产无法可靠交接或复现 | F1 | L |
| P1 | T1：真实协议 replay 与插件 conformance | 固件/协议变化只能上真机发现 | F1，可并行 | M |
| P1 | O1：Run Catalog、诊断、受限日志桥和支持包 | 一次 run 难以查找、比较、解释和分享 | S2 | L |
| P2 | U1：Schema 驱动的通用设置表单 | 通用设置页重复代码 | F1 | S |
| 条件式 | W1：只读 API，再评估 Web 控制面 | 多人、跨机器、无头运维 | S2、O1、安全前置条件 | XL |

依赖关系如下：

```text
S1 Command Ledger ──> S2 Run Journal / Shutdown ──> O1 Run Catalog
        │
        └────────────> F1 Manifest / Admission ──> R1 Profile Bundle
                                      └──────────> T1 Replay / Conformance

U1 只依赖稳定契约；W1 必须等待运行时安全与本地运维能力成熟。
```

## 4. P0：先补齐安全运行时

### 4.1 S1：维护模式与持久化 Command Ledger

#### 当前问题

[task_manager.py](phoebe/core/task_manager.py) 在验证 payload 和解析依赖后立即创建新的 `task_id` 并尝试租约；相同 `command_id` 会创建两个 run。内存中的 run record 不能跨重启去重。

#### 目标设计

在创建 task、获取 lease 或触碰硬件前引入 `CommandLedger`：

- key：`command_id`。
- 不可变字段：command、规范化 payload hash、issued_by、首次接收时间。
- 状态：received、rejected、queued、admitted、started、terminal。
- 结果：原始 `CommandAck`、task/run id、类型化拒绝码。
- 同 id + 同 payload：返回第一次的结果，不创建新 task。
- 同 id + 不同 payload：返回 `COMMAND_ID_CONFLICT`。

推荐先定义存储接口，再用标准库 SQLite 实现默认 backend；唯一索引能直接表达幂等约束。如果选择 JSONL，必须额外证明单写者、flush/fsync、损坏尾行恢复和重建索引的行为。

Gateway/TaskManager 同时增加 maintenance gate。maintenance 中拒绝新的 start command，但仍允许 status、cancel 和安全诊断。

#### 交付物

- `phoebe/core/command_ledger.py`
- `AdmissionCode`、`AdmissionDecision` 契约
- Gateway/TaskManager 的 maintenance 开关
- duplicate/conflict/restart/partial-write 测试

#### 验收标准

- 并发提交两个相同 id、相同 payload 的命令，只产生一个 task 和一次 lease 获取。
- 相同 id、不同 payload 在任何硬件动作前被拒绝。
- 进程在 received、queued、started 后分别中断，重启仍能给出确定结果。
- rejected command 也可审计，但不会创建空 run 目录。

### 4.2 S2：持久化 run 生命周期，不混用“执行结果”和“清理完成”

#### 当前问题

现有 `RunState` 适合 UI 的 pause/cancel 状态机，但 run 会在 cleanup 前进入 completed/failed/aborted，随后用 `reason="final"` 再广播一次。这个字符串约定不是可靠的持久化恢复协议，cleanup 异常也只写日志。

#### 目标设计

保留 `RunState` 作为交互状态，不再另造一套与它冲突的状态枚举；新增追加式 `RunJournalEvent`，记录生命周期事实：

```text
admitted
  → run_dir_created
  → baseline_captured
  → staged
  → execution_started
  → execution_outcome(completed | failed | aborted)
  → cleanup_started
  → writer_closed
  → leases_released
  → finalized(ok | degraded)
```

其中：

- `execution_outcome` 描述插件主体结果。
- `finalized` 描述 writer、baseline、controller cleanup 和 lease release 是否全部完成。
- cleanup 失败时可以是“执行 completed，但 finalization degraded”；UI 必须显示二者。
- 下一版事件契约应使用显式 finalization 字段或 `RunFinalizedEvent`，逐步淘汰对 `reason="final"` 的业务依赖。

关键记录需 flush；是否每条 fsync 应根据风险分级，但 `started`、`cleanup_started` 和 `finalized` 必须具备断电可见性。

启动时扫描未 finalized 的 run：

- sim run：标为 interrupted，可从头创建新 run；不得覆盖旧 run。
- real run：标为 `operator_review_required`；不得自动调用 Controller。
- 允许的操作只有查看上下文、显式 health/safe-state 检查、确认放弃，以及从保存的实验 Config 新建草稿。

#### 验收标准

- 在 staged、execution_started、cleanup_started、writer_closed 四个 fault injection 点强制终止，重启后均得到确定的 journal 解释。
- real run 的恢复扫描执行零条设备命令。
- Run Catalog 能区分 execution outcome 与 finalization status。
- journal 尾部损坏不会掩盖此前已落盘的记录，并产生明确诊断。

### 4.3 S2：受控 shutdown

新增 `TaskManager.shutdown(deadline_s)`，由所有退出路径复用：

```text
Gateway 进入 maintenance，拒绝新 start
  → 取消 queued run
  → 请求 active run cancel
  → 有界等待 checkpoint、plugin finally、writer close、controller cleanup、lease release
  → 对未确认设备执行 stop / safe_state
  → 持久化 degraded finalization 与 operator-review 原因
  → 停止 suspenders / lease reaper
  → disconnect controllers
  → 停止 workers 和 asyncio loop
```

超时不是成功。每个未完成步骤都要落入 journal 和退出摘要；下一次启动在 identity/health 与人工复核完成前不得把相关 real device 标为 ready。

验收至少覆盖 UI close、SIGINT、启动中途失败、paused run、无响应 Controller、writer close 异常和 cleanup 超时。

## 5. P0：插件治理与实验准入

### 5.1 PluginManifest 只保存静态事实

AstrBot 的 `StarMetadata` 和 `PluginManager` 证明了版本、兼容范围、启停状态和 UI 元数据的价值。PHOEBE 可以吸收这个思路，但不应引入运行时 pip install、插件市场、watcher 热重载或自动更新。

建议扩展 [plugin.py](phoebe/core/plugin.py)，让每个受信任插件提供不可变的 `PluginManifest`：

- plugin id、语义版本、PHOEBE API 兼容范围、Config schema version。
- command 列表、输入 Profile 类型、输出 artifact 类型。
- 是否要求真实硬件、UI panel id 和显示名称。
- manifest 自身的稳定 hash。

必须避免两个来源同时描述设备需求：

- handler 的 `Depends` 类型标注和 role 仍是运行时需求的唯一事实来源。
- `PluginManifest` 保存产品元数据；如为了静态展示而缓存 required kinds，注册时必须由 handler 派生并做一致性检查。
- `CapabilityRegistry` 是每个 Controller 的能力实现表；`DeviceManager` 提供在场实例与 health；admission 只组合结果，不重复实现三者。

每个 run manifest 记录 plugin version、Config schema version 和 manifest hash。

### 5.2 类型化 admission chain

AstrBot 的有序 Pipeline 适合借鉴为准入阶段，但不应复制聊天 pipeline。推荐顺序：

```text
CommandEnvelope boundary validation
  → CommandLedger idempotency
  → maintenance / operator policy
  → plugin manifest + PHOEBE API compatibility
  → DI resolution + configured inventory
  → cached identity / health freshness
  → Profile / calibration binding and validity
  → safety / interlock snapshot
  → lease / queue policy
  → admit and create run
```

准入阶段必须快速、确定、可解释、默认 fail closed。它只消费已缓存的 inventory/health/safety snapshot；需要设备 I/O 的 health refresh 应是显式、有超时的独立诊断动作，不能在 admission 内发送任意原始命令。

每次结果使用稳定 reason code，例如：

- `MAINTENANCE_MODE`
- `PLUGIN_API_INCOMPATIBLE`
- `MISSING_ROLE`
- `HEALTH_STALE`
- `CALIBRATION_EXPIRED`
- `DEVICE_BUSY`
- `COMMAND_ID_CONFLICT`

自由文本只用于面向人的 detail；UI 逻辑、测试和未来 API 依赖 reason code。

### 5.3 把架构规则变成可执行门禁

[refactor.md](refactor.md) 已要求 import-linter 和分层 CI 测试，但当前根项目尚未配置。建议新增：

- Ruff：格式、未使用 import、常见错误。
- Pyright：契约、Protocol 和 Optional 边界。
- import-linter：禁止 UI → Controller/Driver、plugin → Driver/UI、`phoebe/` → legacy。
- pytest：L0/L1/L3/L4；L2 在有脱敏 transcript 后启用。
- 最小 CI workflow：无硬件、无 PyQt 的核心 gate；UI smoke test 可做独立 job。

插件 conformance test 还应检查 command 唯一、manifest 完整、无手写 `sleep`、sim 下可 pause/cancel/cleanup。

## 6. P1：先定义 Profile，再设计 Bundle

当前 PHOEBE 没有一等的 Profile 模型。“Profile Bundle”不能只是把 AppConfig 和几个文件压缩起来。至少要区分：

- **CalibrationAsset**：不可变的 LUT、波长映射、拟合结果或校准报告，包含来源和适用边界。
- **ExperimentConfig**：插件的强类型、可复现实验参数。
- **EnvironmentRequirement**：逻辑 role、instrument kind/model、capability、允许的绑定策略。
- **RunDraft**：以上对象的引用集合；尚未通过当前机器 admission，也不会自动运行。

完整 AppConfig 含 IP、VISA resource、DLL 绝对路径和本机 backend，默认不应进入可移植 Bundle。Bundle 应保存逻辑需求和可选的脱敏配置模板，由目标机器重新绑定本地设备。

### 6.1 建议格式

```text
profile.zip
├── manifest.json
├── experiment-config.json
├── environment-requirements.json
├── assets/
│   ├── calibration/...
│   └── recipes/...
└── notes.md                    # optional, inert text only
```

`manifest.json` 是唯一 checksum 来源，每个 file entry 至少包含：

- 规范化相对路径、media type、字节数、SHA-256、schema version。
- 资产类型、来源 run、创建时间、生成器/算法版本。
- 绑定策略：`strict_serial`、`model` 或 `portable`；默认由资产类型决定，不能一律假设 serial 可迁移。
- plugin id/version、PHOEBE API 范围和 Bundle format version。

SHA-256 只能发现内容与 manifest 不一致；如果攻击者能同时替换二者，它不提供真实性。签名和信任根应作为独立决策，不要把 checksum 描述成签名。

### 6.2 安全导入协议

`preflight()` 只读文件并返回 import plan/diff：

1. 限制 archive 总大小、文件数、单文件大小和压缩比。
2. 拒绝绝对路径、`..`、重复路径、Windows 大小写碰撞、symlink 和特殊文件。
3. 解压到同卷临时目录，逐项验证 size、checksum 和 schema。
4. 检查 plugin/API compatibility、校准 binding 和迁移路径。
5. 显示将新增、复用、冲突或拒绝的资产；不覆盖原 Bundle。
6. 用户显式确认后，才以原子 rename 发布到新的受控目录。

导入不得加载 DLL、import Python、连接仪器、切换 backend 或启动 run；密码、token、任意绝对路径和可执行文件不得入包。密钥只允许保存外部 key 名称。

### 6.3 验收标准

- 干净 sim 环境可把合法 Bundle 解析成 RunDraft，并给出确定的 admission 预览。
- ZIP Slip、zip bomb、symlink、重复/大小写冲突、缺文件、篡改和未知 schema 全部被测试。
- 从历史 run 导出的 Bundle 回链到 source run 和 manifest hash。
- 环境不匹配只生成可解释的拒绝/重绑定计划，不产生设备 I/O。

## 7. P1：协议回放、Run Catalog 与诊断

### 7.1 真实协议 replay

现有 `TranscriptReplayTransport` 已验证命令顺序，但仓库没有真实 fixture。每个支持的真机型号至少录制并脱敏：

- connect/identity；
- 常用 configure/acquire；
- timeout、协议错误和设备错误队列；
- 必要的 binary query/write。

每份 fixture 带 vendor、model、firmware、录制日期、脱敏版本和期望结束位置。binary write 应校验 payload 长度与 digest，而不只校验 command prefix。测试结束必须断言 transcript exhausted。

fixture 应小、脱敏、目的单一；不要提交完整实验日志、真实用户名/主机、序列号或原始测量数据。

### 7.2 本地 Run Catalog

先做 PyQt 内的只读 `RunsPanel` / `DiagnosticsPanel`：

- 扫描 run manifest 和 Run Journal，按 plugin、时间、设备、Profile hash、execution outcome、finalization status 筛选。
- 比较两个 run 的实验 Config、instrument identity、校准资产和代码状态。
- 从历史实验 Config 生成未执行 RunDraft，绝不“重放 run”。
- 展示 timeline、health freshness、EventBus dropped、cleanup 降级和 operator-review 状态。

主数据仍在 run 目录。索引可以使用 SQLite 提速，但必须可从文件重建，不能成为唯一实验事实来源。服务逻辑先放入 UI 无关的 `RunCatalogService` / `DiagnosticsService`，让 PyQt 与未来 API 复用。

### 7.3 受限日志桥与支持包

复用现有 EventBus，不再引入第二个 LogBroker。新增 loguru sink adapter：

- 全量、结构化日志继续写 `experiment.jsonl`。
- UI 只接收经过级别、长度和脱敏限制的 `LogEvent` excerpt。
- sink 不得递归记录自身错误，不得承载数组、密钥或原始设备 dump。
- EventBus 的有界订阅与丢弃策略继续生效；日志流不承担命令可靠性。

支持包采用 allowlist，而不是“先收集全部再删秘密”：选定 run manifest、journal、有限日志摘录、health snapshot、软件版本和可选脱敏 transcript；默认不包含 HDF5 原始数据。

## 8. P2 与条件式工作

### 8.1 Schema 驱动表单的适用边界

可自动生成：连接参数、timeout、通用设备 options、存储路径、队列策略、health 阈值。

不应自动生成：校准、编码分析、TPA 优化、光谱判断等具有实验语义和可视化需求的 workflow。它们继续采用“一个明确实验任务对应一个专用 Panel”：Panel 组装强类型 Config，经 Gateway 发命令，再订阅事件。

### 8.2 Web/API 的触发条件

AstrBot Dashboard 的 Vue 3 + TypeScript + Vite + Vuetify 架构说明 Web 管理面可以成熟地承载配置、日志与状态，但这不是 PHOEBE 当前的需求证明。[refactor.md](refactor.md) 中的 gRPC/Web 前端阶段应视为条件式方向，而不是默认承诺。

只有同时满足以下条件才立项：

- 明确需要多人查看、跨机器访问或控制进程无头运行；
- 已有身份认证、角色权限、审计、维护模式和网络隔离方案；
- S1/S2/O1 已完成，远程层能够复用同一 admission、lease、journal 和服务；
- 已明确只读与控制权限边界，并完成威胁建模。

落地顺序应是只读 run/health API → 审计与权限验证 → 受限 command submit。永远不暴露 raw SCPI、任意 Python 或绕过 Gateway 的 Controller 调用。

## 9. 明确不照搬的 AstrBot 能力

- Agent、LLM Provider、聊天会话、知识库、IM 平台适配和聊天 Pipeline 业务阶段。
- 插件市场、运行时 pip/Git 安装、自动更新、文件 watcher 和热重载。
- 为单机实验台直接引入完整 FastAPI + SQLAlchemy + Vue 运维栈。
- 用 EventBus 代替可靠指令通道，或让 UI/API 直接调用 Driver/Controller。
- 用一张万能 schema 表单取代专用实验 workflow。
- 自动恢复 real run，或在进程重启后重放最后一条硬件命令。

## 10. 推荐的 PR 切分

每个 PR 应可独立回滚，并带故障注入或离线验证。

| PR | 范围 | 完成标志 |
| --- | --- | --- |
| 1 | Ruff/Pyright/import-linter/pytest 配置与 CI | 当前代码通过；边界违规 fixture 会让 CI 失败 |
| 2 | `CommandLedger`、typed admission code、maintenance gate | duplicate/conflict/restart 测试通过 |
| 3 | `RunJournal` 与显式 finalization | 四个中断点均能在重启后解释 |
| 4 | `TaskManager.shutdown()` 与统一退出路径 | close/SIGINT/timeout/paused run 测试通过 |
| 5 | `PluginManifest`、availability report、run manifest 扩展 | 内置插件均有 manifest；DI 无重复来源 |
| 6 | admission stages 与 health/calibration policy | 每个 stage 和 reason code 有单测 |
| 7 | Profile/Calibration 契约与 Bundle preflight/import | 安全 archive 测试矩阵通过，整个流程零设备 I/O |
| 8 | 首批真实 transcript 与 plugin conformance | 每个已支持型号至少一条高价值 replay |
| 9 | Run Catalog、日志桥、诊断与支持包 | 可查、可比、可脱敏导出；索引可重建 |

PR 2–4 优先于便捷功能；PR 5 可与 PR 2–4 并行设计，但不要在安全生命周期稳定前扩大插件加载面。

## 11. 全局 Definition of Done

任何上述功能只有同时满足以下条件才算完成：

- sim 环境可离线验证，不依赖 VISA、NI-DAQ 或真实仪器。
- admission、Profile preflight、Catalog 和恢复扫描不产生隐式设备 I/O。
- 重试不会创建重复 run；real run 重启后不会自动继续。
- execution outcome、cleanup/finalization 和 operator review 都可持久化并查询。
- 所有大数组继续只走 RunWriter；EventBus 只承载有界事件、preview 或 pointer。
- UI 和未来 API 仍只通过 Gateway 提交命令。
- 新契约有 schema version；未知新版本 fail closed，已支持迁移有测试。
- 失败路径留下机器可读 reason code 和面向人的安全说明。
- 文档中的“已有”能由当前代码证明，“计划”不会写成已实现事实。

## 12. 开放决策

实施前仍需显式决定：

1. Command Ledger 与 Run Journal 是否共用一个 SQLite 文件；推荐共用事务基础设施，但保持独立表和接口。
2. 哪些 CalibrationAsset 必须绑定 serial，哪些允许 model 级或 portable 复用。
3. cleanup degraded 时 UI 的主状态、告警级别和人工确认流程。
4. Bundle 是否需要签名；checksum 不能替代真实性验证。
5. Web/API 的业务触发者、威胁模型和只读阶段成功标准。

## 13. 代码证据索引

| 主题 | AstrBot 参考 | PHOEBE 当前落点 |
| --- | --- | --- |
| versioned backup、manifest、precheck | [exporter.py](AstrBot/astrbot/core/backup/exporter.py)、[importer.py](AstrBot/astrbot/core/backup/importer.py) | [writer.py](phoebe/core/writer.py)，未来 Profile Bundle |
| 插件元数据与生命周期 | [star.py](AstrBot/astrbot/core/star/star.py)、[star_manager.py](AstrBot/astrbot/core/star/star_manager.py) | [plugin.py](phoebe/core/plugin.py)、[bootstrap.py](phoebe/app/bootstrap.py) |
| 有序处理阶段 | [stage.py](AstrBot/astrbot/core/pipeline/stage.py)、[scheduler.py](AstrBot/astrbot/core/pipeline/scheduler.py)、[stage_order.py](AstrBot/astrbot/core/pipeline/stage_order.py) | [gateway.py](phoebe/core/gateway.py)、[task_manager.py](phoebe/core/task_manager.py) |
| cancel/terminate/wait lifecycle | [core_lifecycle.py](AstrBot/astrbot/core/core_lifecycle.py) | [bootstrap.py](phoebe/app/bootstrap.py)、[task_manager.py](phoebe/core/task_manager.py) |
| 有界日志分发 | [log.py](AstrBot/astrbot/core/log.py) | [events.py](phoebe/core/events.py)、[bus.py](phoebe/core/bus.py)、[bridge.py](phoebe/ui/bridge.py) |
| Web 管理界面与配置 schema | [dashboard/package.json](AstrBot/dashboard/package.json)、[vite.config.ts](AstrBot/dashboard/vite.config.ts) | 当前保留 [PyQt UI](phoebe/ui/)；未来仅条件式 API |
| 仿真、协议回放与数据面 | AstrBot 的适配器/测试治理思路 | [mock.py](phoebe/transports/mock.py)、[tests/](tests/)、[writer.py](phoebe/core/writer.py) |

最终判断标准不是 PHOEBE 是否拥有和 AstrBot 一样多的模块，而是：每增加一个插件、仪器型号、配置资产或操作者，都不会降低硬件安全性、实验可复现性和故障可解释性。
