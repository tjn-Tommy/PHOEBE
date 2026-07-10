# `phoebe.core`：运行时骨架

`phoebe/core/` 同时承载控制面和数据面基础设施。它不应该知道某个实验的具体优化算法，也不应该把厂商 SCPI 命令写进来。

## 模块职责

| 文件 | 主要对象 | 作用 |
| --- | --- | --- |
| `contracts.py` | `ContractModel`、ID 类型、`validate_boundary` | 所有跨边界配置/请求/事件的严格 schema |
| `config.py` | `AppConfig`、`InstrumentConfig`、`load_app_config` | TOML 解析、启动即校验、role 绑定和 config hash |
| `events.py` | `GatewayEvent`、`RunStateEvent`、`DataPointerEvent` 等 | 可序列化的观测事件；限制 preview 和事件尺寸 |
| `bus.py` | `EventBus` | retained 状态、每订阅者有界队列、节流和丢帧 |
| `gateway.py` | `CommandEnvelope`、`CommandAck`、`Gateway` | UI/外部入口；把命令交给 TaskManager |
| `task_manager.py` | `TaskManager`、`RunContext` | run 状态机、配置校验、依赖解析、清理和日志 sink |
| `lease.py` | `Lease`、`LeaseSet` | 设备所有权、继承、TTL heartbeat 和引用计数 |
| `device_manager.py` | `DeviceManager` | Factory 创建、连接、identity、health、lease 资源表 |
| `capability.py` | `Capability`、`CapabilityRegistry` | 能力描述、请求/响应校验和 handler 收口 |
| `di.py` | `Depends`、resolver | 把插件声明的 role 映射到已连接设备 |
| `factory.py` | `ControllerFactoryRegistry` | `(kind, vendor, model)` 到 Controller 的组合根注册 |
| `controller.py` | `InstrumentController` | operation lock、settled 语义、stop/safe_state、stage/unstage |
| `plugin.py` | `Plugin`、`PluginRegistry`、`PluginSpec` | `plugin_id -> command -> config_type` 注册表 |
| `worker.py` | `BlockingDeviceWorker`、`WorkerPool` | 在专用线程运行阻塞 SDK/Win32 调用 |
| `writer.py` | `RunWriter`、`RunManifest` | 唯一数据写入者、run 目录、HDF5/JSONL/Parquet |
| `transport.py` | `ScpiTransport`、IEEE block helpers | Driver 使用的异步协议边界和二进制块解析 |
| `sweep.py` | `ScanAxis`、`grid_scan` | 声明式笛卡尔扫描、checkpoint、写入和进度 |
| `errors.py` | `InstrumentError` 等 | 跨层稳定的错误类型；避免 UI 解析字符串 |

## Command Workflow

```text
CommandEnvelope
   │
   ▼
Gateway.submit()
   ├─ pause/resume/cancel -> TaskManager state transition
   └─ experiment command -> TaskManager.dispatch()
          ├─ lookup PluginSpec
          ├─ validate_boundary(config_type, payload)
          ├─ DI resolve roles -> capabilities
          ├─ acquire all leases or reject/queue
          ├─ create RunContext + RunWriter
          └─ asyncio.create_task(plugin entrypoint)
```

`CommandAck` 只回答“已接受/排队/拒绝”，不等同于实验完成。完成情况通过 `RunStateEvent`、`ProgressEvent` 和 run artifacts 观察。

## RunContext 与资源边界

插件拿到的 `RunContext` 是受控能力集合，通常包括：

- `checkpoint()`：暂停点、取消点和 lease heartbeat 的统一位置。
- `writer`：追加数组、写 manifest、记录实验元数据。
- `emit_progress()` / `emit_*()`：发布小型可丢弃事件。
- `log`：带 `task_id`、`run_id`、`plugin` 的结构化日志。
- 由 `Depends(role=...)` 注入的 capability Protocol。

插件不应该拿到 raw `DeviceManager`、Driver 或 Qt 对象。这样可以让同一个实验在真实设备、sim 和 replay transport 上复用。

## EventBus 与 RunWriter 的分工

```text
small state/progress/health/preview ──> EventBus ──> UI subscribers
full spectrum/waveform/array        ──> RunWriter ──> artifacts.h5
run metadata and logs               ──> RunWriter ──> run.json / JSONL
```

事件是 `GatewayEvent` closed union 的成员；新增事件必须加入 union。事件不能放 `numpy.ndarray`，单事件 JSON 目标上限为 64 KB，preview 由 schema 限制到 256 点。

## Lease 与清理

一个 run 获取多个设备时采用 all-or-release-all。子流程通过 `LeaseSet.merge()` 继承父 lease，不能重新竞争同一物理设备。插件退出后，TaskManager 必须按“硬件 stop/safe_state → writer flush/close → lease release → log sink remove”的顺序清理。

## 扩展规则

新增核心能力时优先修改契约和收口点，而不是在 UI 里加特例：

1. 先在 `contracts.py` 定义边界数据。
2. 在 `events.py` 或 `capability.py` 注册稳定的类型/能力。
3. 在 `TaskManager`、`DeviceManager` 或 `Registry` 的唯一入口执行校验。
4. 在 sim、Mock 或 replay 层补测试。
5. 最后让 Panel 组装 Config 并订阅事件。

