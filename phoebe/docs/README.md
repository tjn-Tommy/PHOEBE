# PHOEBE 模块与 Workflow 文档

这组文档对应当前 `phoebe/` 的实际代码路径。根目录的 `refactor.md` 是架构规范；这里负责把规范映射到可以跟踪、调试和扩展的模块。

## 模块地图

| 文档 | 代码目录 | 解决的问题 |
| --- | --- | --- |
| [core.md](core.md) | `phoebe/core/` | 契约、事件、任务、租约、依赖注入、运行数据 |
| [domain.md](domain.md) | `phoebe/domain/` | SLM、OSA、示波器、DAQ、AWG 的领域模型 |
| [instruments.md](instruments.md) | `phoebe/instruments/` | Driver、Controller、Factory、真实设备与仿真 |
| [transports.md](transports.md) | `phoebe/transports/` + `core/transport.py` | VISA、TCP、Mock、SCPI 回放和二进制块 |
| [plugins.md](plugins.md) | `phoebe/plugins/` + `core/plugin.py` | 实验插件、实验命令和 `RunContext` |
| [app-ui.md](app-ui.md) | `phoebe/app/` + `phoebe/ui/` | 启动组合根、Qt/asyncio 线程边界、面板 |
| [testing.md](testing.md) | `tests/` | L0-L4 测试分层、仿真闭环和真机回放 |

## 总体 Workflow

PHOEBE 的一条实验运行路径如下。控制命令和观测事件不是同一条队列；原始测量数据也不走 EventBus。

```text
Qt Main Thread
  Panel: form -> typed payload
        │ CommandEnvelope / submit_threadsafe
        ▼
Dedicated asyncio Loop
  Gateway (must-deliver command)
        │ validate -> plugin lookup -> DI -> lease
        ▼
  TaskManager / RunContext
        │ checkpoint: pause + cancel + lease heartbeat
        ▼
  Experiment Plugin
        │ capability protocol
        ▼
  Controller (lock / settled / safe_state)
        ▼
  Driver (vendor protocol translation)
        ▼
  Transport / Device Worker / Vendor SDK

  Plugin ── small observation ──> EventBus ──> UiEventBridge ──> Qt Panel
  Plugin ── arrays and metadata ─> RunWriter ──> run.json / JSONL / HDF5 / Parquet
```

## 启动 Workflow

```text
phoebe.ui.app.main()
  ├─ load_builtin_plugins()
  ├─ load_app_config(TOML) -> AppConfig (strict validation)
  ├─ LoopThread.start()
  ├─ build_runtime()
  │    ├─ create EventBus / WorkerPool / FactoryRegistry
  │    ├─ register_builtin_factories()
  │    ├─ DeviceManager.start()
  │    │    └─ connect -> identity check -> health state
  │    └─ create TaskManager -> Gateway
  ├─ create UiEventBridge + MainWindow
  └─ initial health_check_all()
```

设备身份校验失败时，启动应在触碰实验 UI 前失败；`backend = "sim"` 时仍使用同一套 Controller/Plugin 路径，只替换 Factory 产出的后端。

## 一次 run 的生命周期

```text
CommandEnvelope
  -> queued / running
  -> plugin checkpoint()
       ├─ request pause -> pausing -> paused -> resume
       ├─ request cancel -> stopping
       └─ normal loop
  -> writer flush + controller cleanup + lease release
  -> completed / failed / aborted (final event)
```

`RunStateEvent(reason="final")` 代表资源清理已经完成。UI 或下一个排队任务不能只根据第一次 terminal 状态判断“可以立即启动”。

## 数据与事件的三条边界

1. **Command path**：`Gateway -> TaskManager`，必达，适合开始/暂停/恢复/取消。
2. **Observation path**：`EventBus`，fan-out、有界、允许慢订阅者丢帧，适合状态、进度、health 和 preview。
3. **Data path**：`RunWriter`，唯一写入者，适合完整 trace、数组、JSONL 和结果文件。

新增功能首先要决定它属于哪条边界；不要把大数组放进事件，也不要让 UI 直接访问 Controller。

## 扩展时的依赖方向

```text
ui -> gateway -> plugin -> task/device -> controller -> driver -> transport
```

实验逻辑只依赖 capability Protocol 和 `RunContext`；新设备通过 `instruments/<vendor_model>/` 加入 Factory/Registry；新实验通过 `plugins/` 显式加载。`awg5204/` 和 `TPA_experiment/` 是迁移参考，不能从 `phoebe/` import。

