# PHOEBE 
![PHOEBE](Phoebe.jpg)
**P**hotonic **H**ardware **O**rchestrator for **E**xtensible **B**enchtop **E**xperiments

按 [refactor.md](refactor.md) v2 架构实现的实验室仪器统一控制平台，整合了原先独立的两套代码库：

- `awg5204/` — Tektronix AWG5204 任意波形发生器（原 tm_devices 实现，保留作参考）
- `TPA_experiment/` — TPA 实验（Santec SLM-200、Yokogawa AQ637X OSA、R&S RTO6 示波器、NI-DAQ，保留作参考）

新平台位于 `phoebe/`，旧代码库原样保留、不受影响。

## 快速开始（离线仿真，无需任何硬件）

```powershell
pip install -e .[dev,ui]       # 核心: pydantic numpy h5py loguru; UI: PyQt5 pyqtgraph
python examples/run_sim_demo.py            # 无 UI 的命令行演示
python -m phoebe.ui.app --config config/sim.toml   # PyQt5 图形界面
python -m phoebe.server --config config/sim.toml   # HTTP API + Web UI（需 pip install -e .[server]）
python -m pytest tests/ -q     # 151 项测试，含 L4 全链路仿真闭环
```

`config/sim.toml` 中每台仪器 `backend = "sim"`；改为 `"real"` 并填好连接参数即切换真机（OSA/Scope/AWG 需 `pip install .[visa]`，DAQ 需 `.[daq]`）。

## 目录结构 → 架构章节对照

| 路径 | 内容 | refactor.md |
| --- | --- | --- |
| `phoebe/core/contracts.py` | `ContractModel`、语义 ID、物理约束标量、`validate_boundary` | §3 |
| `phoebe/core/events.py` | `BusEvent` 封闭联合、`RunState`、指针事件/预览 | §3.4, §8.1 |
| `phoebe/core/config.py` | TOML → `AppConfig`，启动即校验 | §3.5, §5.3 |
| `phoebe/core/bus.py` | EventBus（fan-out + 有界队列 + retained + 节流） | §9 |
| `phoebe/core/worker.py` | 每设备阻塞 worker 线程（DLL 消息泵可选） | §12.3–12.4 |
| `phoebe/core/capability.py` | Capability 描述符 + Registry 收口校验 | §4.5 |
| `phoebe/core/controller.py` | Controller 基类（op lock、stop/safe_state、stage/unstage、settled） | §4.4 |
| `phoebe/core/lease.py` + `device_manager.py` | 所有权表、try-all-or-release-all、Lease 继承、TTL 回收、身份校验 | §5.4, §6 |
| `phoebe/core/di.py` | `Depends(role=…)` + 绑定表 + 唯一性解析 | §7 |
| `phoebe/core/task_manager.py` | Run 状态机、checkpoint 式 pause/cancel、Suspender、清理路径、loguru sink | §8 |
| `phoebe/core/writer.py` | RunWriter（唯一 HDF5 写者、背压）、run 目录、Parquet compact | §10 |
| `phoebe/core/gateway.py` | CommandEnvelope/CommandAck，内建 pause/resume/cancel | §13 |
| `phoebe/core/sweep.py` | `grid_scan` 声明式扫描原语 | §11.2 |
| `phoebe/domain/` | 各仪器族契约模型 + 数据面对象 | §3.1, §3.3 |
| `phoebe/transports/` | TCP / VISA / Mock / TranscriptReplay | §4.2, §14.1 |
| `phoebe/instruments/protocols.py` | 五个基础能力 Protocol + kind 登记表 | §4.4 |
| `phoebe/instruments/*/` | 各型号 Driver + Controller + 工厂 | §4.3–4.4, §5.2 |
| `phoebe/instruments/sim/` | 共享 `SimContext` 的物理仿真后端 | §14.2 |
| `phoebe/plugins/` | 实验插件（TPA 寻优、grid scan） | §11 |
| `phoebe/app/bootstrap.py` | 组合根 + 专用 loop 线程 | §16, §12.1 |
| `phoebe/ui/bridge.py` | Qt 事件桥（PyQt5，跨线程 Signal） | §12.5 |
| `phoebe/ui/main_window.py` | 设备面板 / 运行控制 / pyqtgraph 实时绘图 / 事件日志 | §13.2 |
| `phoebe/ui/app.py` | UI 入口：Qt 主线程 + phoebe loop 线程 | §12.1 |
| `phoebe/contracts/` | 全部可序列化契约（AckCode/事件/日志记录）+ JSON Schema 导出 | 演进计划 C-1/C-5 |
| `phoebe/services/` | 应用服务层（PyQt 与 HTTP 适配器共用同一表面） | 演进计划 C-4 |
| `phoebe/server/` | FastAPI 适配器：`/api/v1` + SSE 事件流 + 静态 Web UI + 安全阶梯 | 演进计划 Phase E |

## 迁移映射

| 旧代码 | 新位置 |
| --- | --- |
| `TPA_experiment/src/osa_module/driver` | `phoebe/instruments/yokogawa_aq637x/`（telnet 握手挪入 transport `on_open`；软件平均保留线性域算法） |
| `TPA_experiment/src/slm_module/driver` | `phoebe/instruments/santec_slm200/`（`_DeviceThread` → 平台级 `BlockingDeviceWorker(pump=True)`；settle 进 `SlmOptions.settle_ms` 并写入 RunManifest） |
| `TPA_experiment/src/slm_module/generator` | `phoebe/domain/pattern.py`（掩膜生成器）+ `santec_slm200/csvio.py`（厂商 CSV 格式） |
| `TPA_experiment/src/scope_module` | `phoebe/instruments/rs_rto6/`（`monitor_cycle` → `Oscilloscope.monitor_sample`） |
| `TPA_experiment/src/daq_module` | `phoebe/instruments/ni_daq/` |
| `awg5204/awg5204_tm/{driver,hardware,sequence,waveform}` | `phoebe/instruments/tek_awg5204/` + `phoebe/domain/awg.py`（**已去除 tm_devices 依赖**，命令树翻译为原生 SCPI） |
| TPA 编码寻优循环骨架 | `phoebe/plugins/tpa_multiplier.py` |

### 尚未迁移（有意留待后续）

- **两个旧 GUI 的完整功能**（`awg5204/awg5204_tm/ui`、`TPA_experiment/src/slm_module/gui/app.py`）：新平台已提供 PyQt5 UI Shell（设备健康、TPA/GridScan 启动表单、pause/resume/cancel、实时光谱/指标绘图、事件日志）；旧 GUI 中的专项页面（标定、编码分析等）应随其对应插件逐个移植为新 Panel——「表单 → Config → send_command」+「订阅事件刷新」。
- **深度分析/优化代码**（`optimization.py`、`tpa_phase.py`、`calibration_new.py`、`analysis.py` 等）：属实验域逻辑，应按需逐个移植为插件（模式见 `tpa_multiplier.py`，子流程复用见 §11.3 lease 继承）。
- **L2 真机会话录制**：`TranscriptReplayTransport` 已就绪，需在真机上录制后加入回归。

## 写一个新实验插件

```python
from phoebe.core.di import Depends
from phoebe.core.plugin import Plugin, on_command, register
from phoebe.instruments.protocols import PatternModulator, SpectrumAnalyzer

@register(plugin_id="org.lab.my_experiment")
class MyExperiment(Plugin):
    config_type = MyConfig                      # ContractModel 子类

    @on_command("start_my_experiment")
    async def run(self, config: MyConfig, ctx,
                  slm: PatternModulator = Depends(role="primary_slm"),
                  osa: SpectrumAnalyzer = Depends(role="main_osa")):
        for step in range(config.max_steps):
            await ctx.checkpoint("step", step=step)          # pause/cancel/心跳
            await slm.display_pattern(frame, context=ctx)    # 返回即 settled
            trace = await osa.acquire_trace(req, context=ctx)
            ptr = await ctx.writer.append_array("traces/x", trace.y_dbm,
                                                attrs=trace.meta)
            ctx.emit_progress(step=step, total=config.max_steps,
                              pointer=ptr, preview=trace.preview())
```

插件内**零锁代码、零 Driver import、零手写 sleep** —— 违反即打回（§18 硬性规则）。
