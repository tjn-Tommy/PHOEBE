# Application 与 PyQt UI

`phoebe/app/` 是组合根，`phoebe/ui/` 是 Qt 表现层。Qt 不拥有设备，也不执行实验；它只构造命令并显示事件。

## 文件级索引

| 文件 | 作用 |
| --- | --- |
| `app/bootstrap.py` | 创建 EventBus、WorkerPool、Factory、DeviceManager、TaskManager 和 Gateway |
| `ui/app.py` | CLI 参数、插件/配置加载、LoopThread、Qt 生命周期和 shutdown |
| `ui/bridge.py` | asyncio EventBus 到 Qt `pyqtSignal` 的唯一出口 |
| `ui/main_window.py` | Device、Run Control、Plot、Log 以及实验表单 Panel |
| `ui/__init__.py` | Qt 相关导入保持 lazy，core 不依赖 Qt |

## 线程拓扑

```text
Qt Main Thread                         Dedicated asyncio Loop
─────────────────                      ─────────────────────────
MainWindow / Panel                     Gateway / TaskManager
DevicePanel / PlotPanel                EventBus / DeviceManager
LogPanel                               Plugins / Controllers
       │ submit_threadsafe(envelope)            │
       └────────────────────────────────────────┘
       │ UiEventBridge (pyqtSignal)
       └─────────────────────── events back to Qt

Blocking vendor SDK calls -> per-device BlockingDeviceWorker threads
```

## 启动入口

`python -m phoebe.ui.app --config config/sim.toml` 的实际顺序：

1. `load_builtin_plugins()`：显式注册实验命令。
2. `load_app_config()`：TOML 在硬件触碰前完成严格校验。
3. `LoopThread.start()`：创建专用 asyncio loop。
4. `build_runtime()`：Factory、DeviceManager、TaskManager、Gateway 组合。
5. `UiEventBridge.start()`：订阅 EventBus。
6. `MainWindow`：创建 Devices、Run Control、Plot 和 Log 等面板。
7. 触发 `health_check_all()`，填充设备表。
8. Qt 退出后先 `bridge.stop()`，再等待 runtime shutdown，最后停止 loop thread。

## 面板职责

| 组件 | 当前作用 | 输入/输出 |
| --- | --- | --- |
| `DevicePanel` | 显示 inventory 和 health | `DeviceHealthEvent` |
| `_ScanForm` | 复用 OSA center/span/points 字段 | payload fragment |
| `TpaForm` | 构造 TPA search 参数 | `start_tpa_run` |
| `GridForm` | 构造 SLM level 网格参数 | `start_grid_scan` |
| `RunControlPanel` | Start/Pause/Resume/Cancel 和状态 | `CommandEnvelope` + `RunStateEvent` |
| `PlotPanel` | spectrum preview 和 metric history | `DataPointerEvent`/`ProgressEvent` |
| `LogPanel` | 显示短日志和 Gateway ack | `LogEvent`/ack |
| `UiEventBridge` | loop -> Qt 的唯一事件桥 | `pyqtSignal` |

## UI Workflow

```text
button click
  -> form.payload()
  -> CommandEnvelope(command_id, command, payload)
  -> gateway.submit_threadsafe(...)
  -> async CommandAck
  -> EventBus events
  -> UiEventBridge signal
  -> panel state/plot/log refresh
```

表单可以做轻量输入检查，但最终 schema 校验必须在 `TaskManager.dispatch()` 进行。ack 被拒绝时使用 status bar/log，不在命令回调中弹 modal；这样不会阻塞 loop 或自动化/离屏运行。

## 增加新 Panel

1. 在 `MainWindow` 增加独立 Panel/Tab，保持实验边界清晰。
2. 定义一个 domain Config 或复用现有 Config model。
3. 表单只组装 payload，通过 Gateway 发命令。
4. 在 `UiEventBridge`/MainWindow 中订阅状态、进度、preview 和错误。
5. 不从 UI import `phoebe.instruments.*`，不保存 Controller 引用。

通用连接参数可以由 schema 辅助生成；标定、编码分析、TPA 优化等具有实验语义的流程仍应使用专用 Panel。
