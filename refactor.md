# 实验控制平台架构设计文档

**版本**: v2.0 · **日期**: 2026-07-07 · **状态**: Review Draft 

------

## v1 → v2 变更摘要

v2 保留 v1 的全部静态分层结论（组合优于继承、Driver/Controller/Capability 三层、三类 Registry 分离、Transport 可注入、统一错误模型、Mock 先行），并补齐 v1 缺失的**运行时语义**与**跨界数据契约**。主要变更：

1. **控制面 / 数据面分离**：主数据不再经 EventBus 落盘。实验循环直写 `RunWriter`（唯一 HDF5 写者，带背压），总线只承载"指针事件 + 降采样预览"。总线事件由类型系统强制不可携带大数组。
2. **中断与安全态契约**：Controller 基类新增 `stop() / safe_state() / stage() / unstage()`；TaskManager 引入显式 Run 状态机、checkpoint 式 pause/resume 与 Suspender（信号越界自动挂起）。
3. **Settled 完成语义**：`display_pattern()` 等硬件动作，返回即代表物理稳定（液晶 settle 完成），settle 参数进入配置并写入 run 元数据。
4. **资源模型重写**：Lease 采用 ownership 表（同步原子获取、try-all-or-release-all、无 hold-and-wait）；引入 **Lease 继承**使实验子流程可组合；增加 TTL + 心跳回收。
5. **DI 消歧**：`Depends(role=...)` + 配置绑定表，解决多台同类设备的注入歧义。
6. **线程模型显式化**：Qt 主线程 / asyncio loop 线程 / 每设备阻塞 worker 线程（DLL 设备拥有独立 Win32 消息泵线程）三层拓扑与桥接规则。
7. **EventBus 实现规范**：topic → 每订阅者有界队列的 fan-out；显式丢弃策略；retained last event；发布侧节流。指令通道与观测通道分离。明确结论：进程内 asyncio 总线，不引入 ZeroMQ。
8. **Pydantic 严格类型契约层**：所有跨边界数据（配置、事件、capability 请求/响应、run 元数据）必须是 `ContractModel`；capability 调用在 Registry 收口处做 schema 校验；JSON Schema 可导出供未来 Tauri/gRPC 前端生成类型。
9. **数据规范细化**：相位掩膜按面板原生量化位深存储（uint16 + LUT 引用）、参数化掩膜只存 seed + 生成器版本；双时间戳；run 前后 baseline snapshot + config hash + git commit；Parquet 运行中以 JSONL 缓冲、结束时 compact。
10. **Sweep Helper 与物理仿真闭环**：提供 `grid_scan` 等声明式扫描原语；将 TPA 五模块物理仿真挂接到 Mock 设备后端，实现全链路离线闭环测试并作为 CI gate。
11. 修正 v1 文本问题：`TPACalfig` typo、`SpectrumAnalyzer` 接口两处定义不一致（统一为不含 `connect/disconnect` 的能力视图）、章节编号断档。

------

## 目录

1. 目标、约束与非目标
2. 总体拓扑：控制面与数据面
3. 类型契约层（Pydantic）
4. 硬件抽象：Transport / Driver / Controller / Capability
5. 设备构建：Factory、配置与身份校验
6. 资源模型：Lease、原子获取与继承
7. 依赖注入（DI）
8. TaskManager：状态机、暂停/取消、Suspender 与清理
9. EventBus：进程内观测总线
10. 数据面：RunWriter 与存储规范
11. 实验插件层与 Sweep Helper
12. 线程与并发模型
13. Gateway 与 UI Shell 契约
14. Mock、物理仿真与 CI
15. 统一错误模型
16. 端到端生命周期
17. 演进路线与止损判据
18. 硬性规则（Architecture Invariants）

附录 A：参考系统对照表 · 附录 B：术语表

------

## 1. 目标、约束与非目标

### 1.1 架构目标

本平台面向 SLM、OSA 及未来更多实验室仪器的统一控制需求，在保留真实硬件能力差异的前提下，建立稳定的实验编排、设备管理与跨进程通信边界：

- **依赖倒置与自动注入**：实验逻辑绝不主动申请设备锁；框架解析签名并自动注入已锁定的能力实例。
- **事件驱动解耦**：UI 与仪器之间无直接调用；状态变化经统一 `EventBus` 广播，指令经 `Gateway` 下发。
- **数据完整性优先**：主数据（trace、掩膜、相机帧）走独立数据面直写磁盘，永不依赖可丢弃的事件通道。
- **高维数据可溯源**：HDF5 存矩阵（memory-mapping 友好，供 JAX/PyTorch 离线读取）；标量时序以 Parquet/JSONL 结构化落盘；每个 run 携带完整环境快照。
- **平滑迁移**：当前 PyQt 阶段即以序列化契约（Pydantic → JSON Schema）约束 UI 边界，未来切换 gRPC + Tauri/React 时核心零改动。
- 支持不同品牌、型号与通信方式（TCP / VISA / 串口 / 厂商 DLL / SDK）。

### 1.2 现实约束

- Python ≥ 3.11（依赖 `asyncio.timeout`、`StrEnum`、`typing` 现代语法）。
- Santec SLM-200 经厂商 C++ DLL 控制，DLL 依赖 Win32 消息泵，且为阻塞式调用。
- PyVISA / socket 底层调用为阻塞式。
- 单机部署；UI 当前为 PyQt/PySide。

### 1.3 非目标（Non-goals）

明确不做的事，与要做的事同等重要：

- **不追求软件硬实时**。软件循环提供的是"编排级"时序（~10 ms 量级）。任何需要 µs 级同步的场景（如脉冲触发对齐、门控采集）交由硬件触发线 / 延时发生器完成，软件只负责 arm 与 read。
- **当前不做分布式多机控制**。进程边界预留在 Gateway 一层；核心内部不为分布式付复杂度税（见 §9.8、§17）。
- **不自建通用 workflow 引擎**。实验编排以 Python 协程 + Sweep Helper 表达，不引入 DAG/DSL。若未来需求逼近 RunEngine 级别的 rewind/re-plan，触发 §17 的止损判据，重新评估直接采用 Bluesky。

------

## 2. 总体拓扑：控制面与数据面

v2 最重要的结构性修正：系统存在**两条性质完全不同的通路**，必须物理分离。

- **控制面（Control Plane）**：指令、状态、进度、健康度。特征是小、可序列化、允许对慢消费者降级（丢弃/节流）。载体是 Gateway（指令，必达、点对点）与 EventBus（观测流，fan-out、可丢）。
- **数据面（Data Plane）**：光谱 trace、相位掩膜、相机帧、指标时序。特征是大、一个都不能丢。载体是实验循环内直连的 `RunWriter`（唯一写者，有界队列产生背压）。

```text
┌────────────────────────────────────────────────────────────────────┐
│  Qt UI Shell (PyQt/PySide)                          [Qt 主线程]     │
│  MainWindow / Panels / pyqtgraph                                   │
└───────────┬───────────────────────────────────────▲────────────────┘
   指令下发  │ call_soon_threadsafe        Qt Signal │ (QueuedConnection)
            │                                       │ UiEventBridge
┌───────────▼───────────────────────────────────────┴────────────────┐
│  Gateway（指令通道，必达）        EventBus（观测通道，fan-out 可丢）  │
│  CommandEnvelope 校验/路由        指针事件·进度·状态·健康度          │
├────────────────────────────────────────────────────────────────────┤
│  Experiment Plugin Layer                        [asyncio loop 线程] │
│  @register 插件 · Sweep Helper · 只依赖 Capability                  │
│      │ 主数据直写（await → 背压）                                    │
│      ▼                                                             │
│  RunWriter ──────────► runs/<id>/artifacts.h5 · metrics · logs     │
│  （唯一 HDF5 写者）        （数据面：不经过总线）                     │
├────────────────────────────────────────────────────────────────────┤
│  TaskManager + DeviceManager                                       │
│  状态机 · DI 解析 · Lease 原子获取/继承 · Suspender · Cleanup        │
├────────────────────────────────────────────────────────────────────┤
│  Controller 层（统一实验室语义 · 原子操作锁 · settled 语义）          │
│  AQ637XController / SantecSLM200Controller / ...                   │
├────────────────────────────────────────────────────────────────────┤
│  Driver 层（厂商协议私有实现）                                       │
│      │ await worker.call(...)                                      │
│      ▼                                                             │
│  Per-device Worker Threads              [每设备一个阻塞 worker 线程] │
│  [VISA 阻塞调用] [Santec DLL + Win32 消息泵] [Serial] ...           │
└────────────────────────────────────────────────────────────────────┘
```

阅读本图的三个要点：

1. **横向看线程**：三层线程域（Qt / asyncio loop / 每设备 worker），跨域只允许经指定桥接原语（§12）。
2. **纵向看数据**：主数据从插件层直接向下进 `RunWriter`，与总线无关；总线上只有携带 `run_id + dataset + index + preview` 的指针事件（§9、§10）。
3. **指令 ≠ 事件**：Gateway→TaskManager 是必达的点对点调用；EventBus 是可丢的观测广播。二者不共用队列，避免"必达消息挤占可丢通道"的经典事故。

------

## 3. 类型契约层（Pydantic）

本层是 v2 的地基：**所有穿越架构边界的数据必须是经过 Pydantic 严格校验的契约模型**。边界包括：Gateway 指令、EventBus 事件、capability 请求/响应、启动配置、run 元数据。类型系统在此不仅做校验，还直接**强制执行架构约束**（例如：大数组在类型上就无法进入总线事件）。

### 3.1 两类对象的划分

| 类别                             | 定义                       | 约束                                                    | 例子                                          |
| -------------------------------- | -------------------------- | ------------------------------------------------------- | --------------------------------------------- |
| **契约模型 ContractModel**       | 穿越边界、需要序列化的数据 | 不可变、禁止未知字段、严格类型、可 JSON 化              | 配置、事件、capability 请求/响应、RunManifest |
| **数据面对象 Data-plane object** | 只在实验进程内流动的大数据 | 允许持有 `np.ndarray`；**永不**放上总线、永不直接序列化 | `SpectrumTrace`、相位掩膜帧、相机帧           |

数据面对象的元数据部分仍然是 ContractModel（可入 HDF5 attrs / 事件），数组本体走 `RunWriter`。

### 3.2 基类与受约束标量

```python
# core/contracts.py
from __future__ import annotations
from typing import Annotated, NewType
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime

class ContractModel(BaseModel):
    """跨边界契约基类：不可变、禁止未知字段、严格类型（不做隐式字符串转数值）。"""
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,          # "1.5" 不会被静默转成 float；int→float 仍允许
        validate_default=True,
    )

# 语义化 ID —— NewType 防止 task_id / instrument_id 互相串用
InstrumentId = NewType("InstrumentId", str)
TaskId       = NewType("TaskId", str)
RunId        = NewType("RunId", str)
LeaseId      = NewType("LeaseId", str)
CapabilityId = NewType("CapabilityId", str)

# 带物理约束的标量 —— 越界在进入系统的第一个边界就报错
Nanometer  = Annotated[float, Field(gt=0, lt=20_000)]
Dbm        = Annotated[float, Field(ge=-120, le=40)]
Seconds    = Annotated[float, Field(gt=0)]
Millisecond = Annotated[float, Field(ge=0)]
```

### 3.3 领域模型示例

```python
# domain/spectrum.py
from typing import Literal
from pydantic import model_validator

class SpectrumScanConfig(ContractModel):
    center_nm: Nanometer
    span_nm: Annotated[float, Field(gt=0, le=1500)]
    points: Annotated[int, Field(ge=101, le=50_001)]
    resolution_nm: Annotated[float, Field(gt=0)]
    sensitivity: Literal["norm", "mid", "high1", "high2", "high3"] = "mid"
    average_count: Annotated[int, Field(ge=1, le=999)] = 1

    @model_validator(mode="after")
    def _resolution_vs_span(self) -> "SpectrumScanConfig":
        if self.resolution_nm > self.span_nm:
            raise ValueError("resolution_nm 不得大于 span_nm")
        return self

class TraceRequest(ContractModel):
    scan: SpectrumScanConfig
    trace_name: Literal["TRA", "TRB", "TRC"] = "TRA"

class Peak(ContractModel):
    wavelength_nm: Nanometer
    power_dbm: Dbm

class PeakSearchRequest(ContractModel):
    threshold_dbm: Dbm = -60.0
    max_peaks: Annotated[int, Field(ge=1, le=64)] = 8
```

数据面对象示例——注意它不是 ContractModel，且元数据内嵌契约模型：

```python
# domain/trace.py
import numpy as np
from dataclasses import dataclass

class TraceMeta(ContractModel):
    instrument_id: InstrumentId
    scan: SpectrumScanConfig
    t_wall: AwareDatetime
    t_mono_ns: int

@dataclass(frozen=True, slots=True)
class SpectrumTrace:
    """数据面对象：仅在实验进程内流动，本体经 RunWriter 落盘。"""
    x_nm: np.ndarray          # float64, shape (N,)
    y_dbm: np.ndarray         # float32, shape (N,)
    meta: TraceMeta

    def preview(self, n: int = 256) -> "TracePreview":
        idx = np.linspace(0, len(self.x_nm) - 1, min(n, len(self.x_nm))).astype(int)
        return TracePreview(
            x_nm=self.x_nm[idx].tolist(),
            y_dbm=self.y_dbm[idx].astype(float).tolist(),
        )
```

### 3.4 事件模型：封闭联合与 payload 纪律

事件继承 `ContractModel`。因为基类**不允许任意类型**，`np.ndarray` 字段在类定义阶段即报错——"大数组不上总线"由类型系统强制，而非靠 code review。

```python
# core/events.py
from typing import ClassVar, Literal, Annotated, Union
from pydantic import Field

class BusEvent(ContractModel):
    schema_version: int = 1
    seq: Annotated[int, Field(ge=0)]
    task_id: TaskId | None = None
    t_wall: AwareDatetime
    t_mono_ns: int               # 单调钟，用于与数据行精确对齐（§10.5）

class TracePreview(ContractModel):
    x_nm: list[float] = Field(max_length=256)     # 预览上限写进 schema
    y_dbm: list[float] = Field(max_length=256)

class DataPointerEvent(BusEvent):
    event_type: Literal["data_pointer"] = "data_pointer"
    run_id: RunId
    dataset: str                 # 例: "artifacts.h5:/traces/spectrum"
    index: int
    preview: TracePreview | None = None

class ProgressEvent(BusEvent):
    event_type: Literal["progress"] = "progress"
    step: int
    total: int | None = None
    metrics: dict[str, float] = Field(default_factory=dict, max_length=32)

class RunStateEvent(BusEvent):
    event_type: Literal["run_state"] = "run_state"
    state: "RunState"            # §8.1 定义
    reason: str | None = None

class DeviceHealthEvent(BusEvent):
    event_type: Literal["device_health"] = "device_health"
    instrument_id: InstrumentId
    status: Literal["ok", "degraded", "error", "offline"]
    detail: str | None = None

class ErrorEvent(BusEvent):
    event_type: Literal["error"] = "error"
    error_type: str              # InstrumentError 子类名
    message: str
    instrument_id: InstrumentId | None = None

# Gateway 序列化用的封闭联合：新事件类型必须显式登记，否则无法出境
GatewayEvent = Annotated[
    Union[DataPointerEvent, ProgressEvent, RunStateEvent,
          DeviceHealthEvent, ErrorEvent],
    Field(discriminator="event_type"),
]
```

payload 纪律：单事件序列化后 ≤ 64 KB。`TracePreview.max_length` 已在 schema 层保证常规事件不越界；总线在 dev/sim 模式下额外对 `model_dump_json()` 长度断言，作为第二道防线。

### 3.5 配置模型：启动即校验，fail fast

TOML 配置在进程启动时一次性解析为强类型 `AppConfig`；任何字段拼写错误、越界、缺失都在硬件被触碰之前报错。

```python
# core/config.py
class VisaConnection(ContractModel):
    transport: Literal["visa"] = "visa"
    resource_name: str
    timeout_s: Seconds = 10.0

class TcpConnection(ContractModel):
    transport: Literal["tcp"] = "tcp"
    host: str
    port: Annotated[int, Field(ge=1, le=65_535)]
    timeout_s: Seconds = 10.0

class DllConnection(ContractModel):
    transport: Literal["vendor_dll"] = "vendor_dll"
    dll_path: str
    device_index: Annotated[int, Field(ge=0)] = 0

Connection = Annotated[
    Union[VisaConnection, TcpConnection, DllConnection],
    Field(discriminator="transport"),
]

class InstrumentConfig(ContractModel):
    instrument_id: InstrumentId
    kind: str                    # "spectrum_analyzer" / "pattern_modulator" / ...
    vendor: str
    model: str
    role: str                    # DI 绑定用（§7），如 "main_osa"
    backend: Literal["real", "sim"] = "real"
    connection: Connection
    options: dict[str, object] = Field(default_factory=dict)
    # options 为两段式校验：此处保持通用，Factory 构造时由该型号的
    # Options 契约模型（如 SlmOptions）二次 model_validate，仍是严格校验。

class AppConfig(ContractModel):
    instruments: tuple[InstrumentConfig, ...]
    plugin_bindings: dict[str, dict[str, str]] = Field(default_factory=dict)
    dispatch_policy: Literal["reject", "queue"] = "reject"
    bus_default_queue_size: Annotated[int, Field(ge=16, le=4096)] = 256
```

### 3.6 校验边界、性能开关与 Schema 导出

- **单一收口**：capability 请求/响应校验只发生在 `CapabilityRegistry.invoke()`（§4.5）；指令 payload 校验只发生在 Gateway→TaskManager 的 dispatch 入口。层内调用不重复校验，避免热路径开销失控。
- **性能开关**：响应校验在 `dev/sim` 模式全量开启；`prod` 模式下热路径（如 100 fps 采集回调）可按配置降为抽样校验。请求校验永远全量——坏参数打到硬件的代价远高于一次 `model_validate`。
- **Schema 导出**：`GatewayEvent`、各插件 Config、各 capability 的请求/响应模型统一 `model_json_schema()` 导出，供未来 Tauri 前端经 codegen 生成 TypeScript 类型。这是"迁移 gRPC 时核心零改动"承诺的技术兑现点。

------

## 4. 硬件抽象：Transport / Driver / Controller / Capability

### 4.1 组合而非继承

严禁让 SLM 继承 `ScpiInstrument`。通信实现与仪器能力是两个正交维度，系统以组合（Composition）构建仪器：

```text
通信实现（Driver 私有）          仪器能力（实验可见）
    ├── SCPI over TCP               ├── SpectrumAnalyzer
    ├── SCPI over VISA              ├── PatternModulator
    ├── Serial                      ├── PowerMeter
    ├── Vendor DLL                  ├── Camera / Detector
    └── Vendor SDK                  └── MotionController
```

通用实验依赖**稳定的能力**而非具体型号：TPA 编码或乘法器校准的逻辑层只声明需要 `SpectrumAnalyzer` 与 `PatternModulator`。未来更换 Meadowlark SLM 或 Anritsu OSA，只需新增 Driver + Controller 并注册 Factory；实验插件层代码行数变更必须为 0。

### 4.2 Transport：Driver 的可替换依赖

SCPI 型设备的 Driver 依赖极薄的 transport 协议，而非硬编码 PyVISA / socket：

```python
class ScpiTransport(Protocol):
    async def write(self, command: str) -> None: ...
    async def query(self, command: str) -> str: ...
    async def close(self) -> None: ...
```

实现族：`VisaScpiTransport` / `TcpScpiTransport` / `SerialScpiTransport` / `MockScpiTransport` / `TranscriptReplayTransport`。同一 Driver 因此可在真机、单测、SCPI 回放回归、不同运行环境间复用。厂商 SDK 型设备（Santec DLL）直接持有 SDK client，不伪装成 SCPI。

注意：transport 的 `async` 方法**不意味着底层非阻塞**——阻塞调用被封装进该设备的 worker 线程（§12.3），transport 只是向 worker 投递并 `await` future。

### 4.3 Driver：型号与协议适配层

Driver 只回答"这台具体仪器如何通信"，允许包含完整厂商命令、回复格式与异常细节。

```python
class AQ637XDriver:
    def __init__(self, transport: ScpiTransport) -> None:
        self._t = transport

    async def identify(self) -> str:
        return await self._t.query("*IDN?")

    async def configure_scan(self, scan: SpectrumScanConfig) -> None:
        await self._t.write(f":SENS:WAV:CENT {scan.center_nm:.6f}NM")
        await self._t.write(f":SENS:WAV:SPAN {scan.span_nm:.6f}NM")
        await self._t.write(f":SENS:SWE:POIN {scan.points}")

    async def trigger_single(self) -> None:
        await self._t.write(":INIT:SMOD SING; :INIT")

    async def sweep_complete(self) -> bool:
        return (await self._t.query(":STAT:OPER:COND?")).strip() == "0"

    async def abort(self) -> None:
        await self._t.write(":ABOR")

    async def read_trace_y_dbm(self, trace: str) -> list[float]:
        raw = await self._t.query(f":TRAC:DATA:Y? {trace}")
        return self._parse_trace_values(raw)
```

**Driver 负责**：SCPI 格式化、二进制块解析、SDK 参数转换；设备回复/错误队列/厂商状态码解析；通信超时与断线的底层诊断；把不同通信介质封装为最小 transport 调用。

**Driver 不得负责**：实验 recipe、跨仪器协调、UI 状态；租约与资源锁；数据落盘、事件广播；向上层暴露未规范化的厂商专有对象。

Driver 仅由所属 Controller 私有持有。Experiment、Gateway 与 UI **禁止** import 或调用 Driver（用 import-linter 分层契约在 CI 强制，§18）。

### 4.4 Controller：统一实验室语义、原子操作与运行时契约

Controller 是设备面向系统的正式门面：持有 Driver，将厂商协议翻译为领域模型，并承载 v2 新增的四项运行时契约。

```python
class InstrumentController(ABC):
    def __init__(self, instrument_id: InstrumentId) -> None:
        self.instrument_id = instrument_id
        self.capabilities = CapabilityRegistry(owner=instrument_id)

    # ---- 生命周期与可观测性（v1 已有）----
    @property
    @abstractmethod
    def descriptor(self) -> InstrumentDescriptor: ...
    @abstractmethod
    async def connect(self) -> None: ...
    @abstractmethod
    async def disconnect(self) -> None: ...
    @abstractmethod
    async def get_identity(self) -> DeviceIdentity: ...
    @abstractmethod
    async def get_health(self) -> DeviceHealth: ...
    @abstractmethod
    async def get_snapshot(self) -> InstrumentSnapshot: ...

    # ---- 运行时契约（v2 新增）----
    async def stage(self) -> None:
        """run 开始前置入已知态（清 error queue、设定触发模式等）。默认无操作。"""
    async def unstage(self) -> None:
        """run 正常结束后恢复 idle 行为（如恢复 keepalive/连续扫描）。默认无操作。"""
    @abstractmethod
    async def stop(self) -> None:
        """快速中止当前动作，设备保持可恢复。必须可与进行中的操作并发调用，
        是唯一允许绕过 operation lock 的方法。"""
    @abstractmethod
    async def safe_state(self) -> None:
        """无条件进入物理安全态（关调制输出、快门等）。用于异常路径。"""

class InstrumentDescriptor(ContractModel):
    instrument_id: InstrumentId
    kind: str
    vendor: str
    model: str
    provides: tuple[str, ...]        # 声明式基础能力，如 ("spectrum_analyzer",)
```

**基础能力接口**：以 `Protocol` 表达，注意——能力视图**不含** `connect/disconnect`（修正 v1 的不一致）。实验拿到的是已连接、已租约的实例；生命周期属于 DeviceManager 的职权。

```python
class SpectrumAnalyzer(Protocol):
    async def acquire_trace(
        self, request: TraceRequest, *, context: InvocationContext
    ) -> SpectrumTrace: ...

class PatternModulator(Protocol):
    def get_frame_spec(self) -> PatternSpec: ...
    async def display_pattern(
        self, frame: np.ndarray, *, context: InvocationContext
    ) -> None: ...
    async def set_enabled(self, enabled: bool) -> None: ...
```

**契约一：原子操作锁。** 锁位于 Controller 的原子操作边界，而非 Driver 单条命令边界。一次 OSA acquisition 的配置、触发、等待、读取必须在同一把锁下完成，避免不同任务的 SCPI 指令交错：

```python
class AQ637XController(InstrumentController):
    def __init__(self, instrument_id: InstrumentId, driver: AQ637XDriver) -> None:
        super().__init__(instrument_id)
        self._driver = driver
        self._op_lock = asyncio.Lock()

    async def acquire_trace(
        self, request: TraceRequest, *, context: InvocationContext
    ) -> SpectrumTrace:
        async with self._op_lock:
            await self._driver.configure_scan(request.scan)
            await self._driver.trigger_single()
            while not await self._driver.sweep_complete():
                context.ensure_not_cancelled()        # 取消可达进行中的等待
                await asyncio.sleep(0.05)
            y = await self._driver.read_trace_y_dbm(request.trace_name)
            return self._to_domain_trace(y, request)

    async def stop(self) -> None:
        await self._driver.abort()                    # 不取 op_lock（见基类注释）

    async def safe_state(self) -> None:
        await self._driver.abort()
```

**契约二：settled 完成语义。** 硬件动作"返回即物理完成"。LCOS 有液晶响应时间，DLL 调用返回≠相位已稳定；若 OSA 在 settle 前触发，乘法器校准会带系统性偏差。settle 由 Controller 内部保证并进入配置与 run 元数据，实验代码禁止手写 `sleep` 补偿：

```python
class SlmOptions(ContractModel):
    settle_ms: Millisecond = 50.0        # 写入 RunManifest，可复现
    lut_id: str                          # 相位标定 LUT 引用（§10.3）

class PatternSpec(ContractModel):
    height: Annotated[int, Field(gt=0)]  # SLM-200: 1200
    width: Annotated[int, Field(gt=0)]   # SLM-200: 1920
    levels: Annotated[int, Field(gt=1)]  # 10-bit → 1024

def validate_frame(frame: np.ndarray, spec: PatternSpec) -> None:
    """ndarray 无法由 Pydantic 直接校验，用 spec 契约驱动的守卫函数收口。"""
    if frame.shape != (spec.height, spec.width):
        raise InstrumentContractError(f"frame shape {frame.shape} != {spec}")
    if frame.dtype != np.uint16:
        raise InstrumentContractError("frame 必须为 uint16 原生量化级")
    if int(frame.max(initial=0)) >= spec.levels:
        raise InstrumentContractError("frame 存在超出量化级的像素值")

class SantecSLM200Controller(InstrumentController):
    async def display_pattern(
        self, frame: np.ndarray, *, context: InvocationContext
    ) -> None:
        validate_frame(frame, self._spec)
        async with self._op_lock:
            await self._worker.call(self._driver.write_frame, frame)
            await asyncio.sleep(self._options.settle_ms / 1000)   # settled 后才返回
```

**契约三/四：`stop()/safe_state()` 与 `stage()/unstage()`** 由 TaskManager 在清理路径无条件调用（§8.5）。`CancellationToken` 只能停协程停不了硬件——cancel 之后 OSA 可能仍在 sweep、SLM 仍挂着上一帧，这两对方法就是补上"硬件侧取消"的缺口。

**Controller 其余职责**（承自 v1）：单位/坐标/数据类型归一化（`"778NM"` → float 波长数组）；输入范围验证与状态机检查；timeout 与 cancellation 传播；多条 Driver 调用组合为对上层原子的操作；底层异常映射为统一 `InstrumentError`（§15）；型号独有 capability 的注册与实现。

### 4.5 Capability：声明式注册 + Pydantic 收口校验

基础能力由 `Protocol` 提供；型号独有功能经 Controller 内部的 typed capability registry 暴露。**能力判定必须是声明式的**——`descriptor.provides` 与 `CapabilityRegistry` 的注册表是唯一事实来源，禁止 runtime `isinstance` 嗅探 Protocol（结构化子类型只查方法名不查语义，两个 `display_pattern` 可能物理含义完全不同）。

Capability 描述符是代码级常量（frozen dataclass），其请求/响应类型必须可被 Pydantic 校验：

```python
from pydantic import TypeAdapter

RequestT = TypeVar("RequestT", bound=ContractModel)
ResponseT = TypeVar("ResponseT")

@dataclass(frozen=True, slots=True)
class Capability(Generic[RequestT, ResponseT]):
    id: CapabilityId                     # org.lab.osa.peak-analysis.v1.find-peaks
    request_type: type[RequestT]
    response_adapter: TypeAdapter[ResponseT]
    requires_exclusive_lock: bool = True

OSA_FIND_PEAKS = Capability(
    id=CapabilityId("org.lab.osa.peak-analysis.v1.find-peaks"),
    request_type=PeakSearchRequest,
    response_adapter=TypeAdapter(list[Peak]),
)
```

Registry 是校验的**单一收口**：无论请求来自本地代码还是未来经 gRPC 的 dict payload，都在这里被强制转为契约模型；响应同样过 `TypeAdapter`（受 §3.6 性能开关控制）：

```python
class CapabilityRegistry:
    def register(self, cap: Capability, handler, *, provider: str) -> None: ...
    def supports(self, cap: Capability) -> bool: ...
    def list_ids(self) -> tuple[CapabilityId, ...]: ...

    async def invoke(
        self, cap: Capability[RequestT, ResponseT],
        request: RequestT | dict, context: InvocationContext,
    ) -> ResponseT:
        handler = self._handlers.get(cap.id)
        if handler is None:
            raise UnsupportedCapabilityError(cap.id, owner=self._owner)
        if isinstance(request, dict):                       # 远程/序列化入口
            request = cap.request_type.model_validate(request)
        elif not isinstance(request, cap.request_type):     # 本地类型错用
            raise CapabilityContractError(cap.id, type(request))
        context.ensure_not_cancelled()
        response = await handler(request, context)
        if self._settings.validate_responses:
            response = cap.response_adapter.validate_python(response)
        return response
```

Controller 直接把私有 bound method 注册为 handler：

```python
class AQ637XController(InstrumentController):
    def __init__(self, instrument_id, driver) -> None:
        super().__init__(instrument_id)
        self._driver = driver
        self.capabilities.register(
            OSA_FIND_PEAKS, self._find_peaks, provider="yokogawa.aq637x")

    async def _find_peaks(
        self, request: PeakSearchRequest, context: InvocationContext
    ) -> list[Peak]:
        async with self._op_lock:
            context.ensure_not_cancelled()
            return await self._driver.find_peaks_vendor_mode(request)
```

三者关系保持 v1 表述：**Capability 描述实验需要什么；Handler 描述某型号如何实现它；Registry 描述当前这台已连接设备到底提供什么。** capability id 采用 `org.lab.<kind>.<group>.v<N>.<op>` 命名并显式版本化——schema 演进时新增 v2 id 与旧 id 并存，而非原地改语义。

**能力晋升（事不过三）**：专有功能先留在型号 capability；当同一功能被三台以上不同设备或三个独立实验稳定依赖时，才晋升为 `Protocol` 级基础能力并加入 `provides`。

------

## 5. 设备构建：Factory、配置与身份校验

### 5.1 三类 Registry 严格分离

| Registry                    | 所在位置                 | 解决的问题                                                |
| --------------------------- | ------------------------ | --------------------------------------------------------- |
| `ControllerFactoryRegistry` | 应用启动的组合根         | 配置中的 `kind+vendor+model` 应构造哪个 Driver/Controller |
| Device Instance Registry    | `DeviceManager` 内部     | 逻辑设备 ID（`osa.main`）对应哪个存活 Controller 实例     |
| `CapabilityRegistry`        | 每个 Controller 实例内部 | 该具体型号在基础接口之外还提供什么（§4.5）                |

三者职责完全不同，禁止合并为一个全局字典。

### 5.2 ControllerFactoryRegistry

```python
@dataclass(frozen=True, slots=True)
class ControllerKey:
    kind: str
    vendor: str
    model: str

class ControllerFactoryRegistry:
    def register(self, key: ControllerKey, factory: ControllerFactory) -> None:
        if key in self._factories:
            raise ValueError(f"Duplicate controller key: {key}")
        self._factories[key] = factory

    def create(self, cfg: InstrumentConfig, deps: AppDependencies) -> InstrumentController:
        key = ControllerKey(cfg.kind, cfg.vendor, cfg.model)
        try:
            return self._factories[key](cfg, deps)
        except KeyError as exc:
            raise UnsupportedInstrumentModelError(key) from exc

def build_aq637x(cfg: InstrumentConfig, deps: AppDependencies) -> InstrumentController:
    match cfg.connection:                                  # 判别联合已保证类型
        case VisaConnection() as c:
            transport = VisaScpiTransport(c.resource_name, timeout_s=c.timeout_s,
                                          worker=deps.worker_pool.for_device(cfg.instrument_id))
        case TcpConnection() as c:
            transport = TcpScpiTransport(c.host, c.port, timeout_s=c.timeout_s,
                                         worker=deps.worker_pool.for_device(cfg.instrument_id))
        case _:
            raise UnsupportedInstrumentModelError(cfg.instrument_id)
    return AQ637XController(cfg.instrument_id, AQ637XDriver(transport))

controller_factories.register(
    ControllerKey("spectrum_analyzer", "yokogawa", "aq6370"), build_aq637x)
```

工厂内完成 `options` 的二段式校验（如 `SlmOptions.model_validate(cfg.options)`），坏配置在实例化前爆炸。

### 5.3 配置示例

用户与实验只引用逻辑设备 ID / role，具体型号由配置与 Factory 决定：

```toml
[[instruments]]
instrument_id = "osa.main"
kind   = "spectrum_analyzer"
vendor = "yokogawa"
model  = "aq6370"
role   = "main_osa"
backend = "real"
  [instruments.connection]
  transport = "visa"
  resource_name = "TCPIP0::192.168.1.50::inst0::INSTR"
  timeout_s = 30.0

[[instruments]]
instrument_id = "slm.primary"
kind   = "pattern_modulator"
vendor = "santec"
model  = "slm-200"
role   = "primary_slm"
backend = "real"
  [instruments.connection]
  transport = "vendor_dll"
  dll_path = "C:/santec/slm200.dll"
  device_index = 0
  [instruments.options]
  settle_ms = 50.0
  lut_id = "slm200_phase_lut_2026q2"

[plugins."org.lab.tpa_multiplier".bindings]
osa = "main_osa"          # 插件参数名 → role（§7.2）
slm = "primary_slm"
```

### 5.4 Device Instance Registry 与身份校验

`DeviceManager` 管理已连接、已验明正身的 Controller 实例，负责：按配置调 Factory 创建；connect/disconnect/shutdown；启动后身份验证与周期 health 检查（结果以 `DeviceHealthEvent` 上总线）；维护逻辑 ID → 实例映射；向 TaskManager 提供依赖解析与 lease 服务（§6）；输出 inventory / health / capability snapshot。

它**不得**实现 `acquire_trace()`、`display_pattern()` 等具体仪器动作——不允许演化为 God Object。

**身份校验是硬要求**：连接后必须以 `*IDN?`、序列号或厂商 SDK identity API 校验真实设备；配置文件不是事实来源。型号、序列号或资源地址不匹配时，Controller 不得进入 `ready` 状态。

------

## 6. 资源模型：Lease、原子获取与继承

**彻底废弃实验代码手动 `acquire()`**。资源分配由框架接管，模型如下。

### 6.1 Ownership 表与 Lease 契约

租约不用 `asyncio.Lock` 表达，而是 DeviceManager 内的**所有权表**——这使"获取"成为不含 `await` 的同步操作，在单事件循环内天然原子，且崩溃后可按表回收：

```python
class Lease(ContractModel):
    lease_id: LeaseId
    instrument_id: InstrumentId
    holder_task_id: TaskId
    parent_lease_id: LeaseId | None = None
    acquired_at: AwareDatetime
    ttl_s: Seconds = 600.0

class LeaseSet:
    """一次 run 持有的全部租约 + 引用计数；支持子流程继承。"""
    def holds(self, instrument_id: InstrumentId) -> bool: ...
```

### 6.2 原子获取：try-all-or-release-all，无 hold-and-wait

```python
class DeviceManager:
    def try_acquire_all(
        self, task_id: TaskId,
        requirements: Sequence[ResolvedRequirement],
        parent: LeaseSet | None = None,
    ) -> LeaseSet:
        """同步方法：过程中无 await → 单 loop 内原子。
        任一设备不可得 → 回滚本次全部已授出租约 → 抛 LeaseUnavailableError。
        绝不持有部分租约等待剩余租约（no hold-and-wait）→ 死锁在结构上不可能。"""
        granted: list[Lease] = []
        for req in requirements:
            if parent is not None and parent.holds(req.instrument_id):
                parent.incref(req.instrument_id)           # 继承，见 §6.3
                continue
            owner = self._owners.get(req.instrument_id)
            if owner is not None:
                self._rollback(granted, parent, requirements)
                raise LeaseUnavailableError(req.instrument_id,
                                            holder=owner.holder_task_id)
            lease = self._grant(task_id, req)
            granted.append(lease)
        return LeaseSet.merge(parent, granted)
```

获取失败的默认语义是**立即拒绝**（Gateway 收到 `423 Locked` 语义的 `CommandAck`），绝不半途阻塞；是否转入排队由 dispatch 策略决定（§8.2）。这条"无 await 即原子"的性质依赖单进程单 loop；将来若多进程化，本节的显式规范（全有或全无、无 hold-and-wait）是防止真死锁的迁移契约。

### 6.3 Lease 继承：让实验可组合

这是最容易被漏、代价最大的一条。TPA run 中途调用 calibration 子流程，子流程再声明依赖同一台 OSA——若无继承，它会对自己死锁或吃 423，实验代码将无法复用只能复制粘贴。规则：

- `InvocationContext` 携带当前 `LeaseSet`；子任务/子流程的依赖解析**优先从父上下文满足**（引用计数 +1），不足部分再走正常获取。
- 释放按引用计数：子流程结束只减计数，物理释放发生在根 run 的 cleanup。
- 直接函数调用式的子流程（同一协程栈内，§11.3）天然共享 ctx，无需任何显式操作。

这补回了 Bluesky plan 天然可嵌套组合的能力，而不引入 generator-based plan 风格。

### 6.4 TTL、心跳与崩溃回收

每个 lease 带 `ttl_s`；`RunContext.checkpoint()`（§8.3）在每次调用时 touch 全部持有租约作为心跳。DeviceManager 的 reaper 任务发现超时租约（任务崩溃/挂死）时：广播 `ErrorEvent` → 对涉事设备执行 `stop()` + `safe_state()` → 回收租约。心跳缺失是"实验粒度过粗"的信号，同时暴露为 health 指标。

------

## 7. 依赖注入（DI）

### 7.1 声明语法

```python
# core/di.py
class Depends:
    def __init__(self, role: str | None = None) -> None:
        self.role = role
```

实验逻辑只声明需要的能力与（可选的）role，禁止出现任何加锁代码：

```python
class CalibrationRunner:
    async def run(
        self,
        config: CalibrationConfig,
        ctx: RunContext,                                   # 框架服务聚合注入
        osa: SpectrumAnalyzer = Depends(role="main_osa"),
        slm: PatternModulator = Depends(role="primary_slm"),
    ) -> None:
        await slm.display_pattern(config.init_mask_frame, context=ctx)
```

`RunContext` 聚合了框架侧服务（log、cancel、checkpoint、emit、writer、leases），避免签名被五六个框架参数淹没；设备能力仍逐个 `Depends`，保持依赖显式可见。

### 7.2 解析算法与多设备消歧

纯按类型解析在两台同类设备（如双 OSA）下不成立。TaskManager 在 dispatch 前用 `inspect.signature` + `typing.get_type_hints` 解析，按以下优先级绑定每个参数到 `instrument_id`：

1. 参数上显式的 `Depends(role=...)`；
2. 配置绑定表 `[plugins."<id>".bindings]` 中 参数名 → role 的映射（§5.3）；
3. 若该能力类型在 inventory 中**唯一**，直接绑定；
4. 否则在 dispatch 阶段立即报错（fail fast），绝不静默猜测。

能力类型 → kind 字符串的映射（如 `SpectrumAnalyzer → "spectrum_analyzer"`）由显式登记表维护，配合 `descriptor.provides` 完成声明式匹配（呼应 §4.5：不做 isinstance 嗅探）。

### 7.3 注入流程

解析产物是 `ResolvedRequirement(param_name, instrument_id, capability)` 列表 → `DeviceManager.try_acquire_all()` 原子获取 → 注入的是**已租约、已 stage 的能力视图**。实验拿到 `osa` 那一刻，它已经是被独占锁定的实例——这就是"依赖注入接管设备生命周期"的完整含义。

------

## 8. TaskManager：状态机、暂停/取消、Suspender 与清理

### 8.1 Run 状态机

```python
class RunState(StrEnum):
    QUEUED    = "queued"
    RUNNING   = "running"
    PAUSING   = "pausing"      # 已请求暂停，等待下一个 checkpoint 生效
    PAUSED    = "paused"
    STOPPING  = "stopping"     # 已请求取消，正在执行硬件 stop + cleanup
    COMPLETED = "completed"
    FAILED    = "failed"
    ABORTED   = "aborted"      # 用户取消

_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.QUEUED:   frozenset({RunState.RUNNING, RunState.ABORTED}),
    RunState.RUNNING:  frozenset({RunState.PAUSING, RunState.STOPPING,
                                  RunState.COMPLETED, RunState.FAILED}),
    RunState.PAUSING:  frozenset({RunState.PAUSED, RunState.STOPPING}),
    RunState.PAUSED:   frozenset({RunState.RUNNING, RunState.STOPPING}),
    RunState.STOPPING: frozenset({RunState.ABORTED, RunState.FAILED}),
    # 终态无出边
}
```

每次迁移广播 `RunStateEvent`；UI、Suspender、排队器全部依赖它，禁止各自维护影子状态。

### 8.2 Dispatch：策略化的准入

```python
class TaskManager:
    async def dispatch(self, cmd: CommandEnvelope) -> CommandAck:
        plugin = self._plugins[cmd.command]
        config = plugin.config_type.model_validate(cmd.payload)   # 契约校验收口
        reqs = self._resolver.resolve(plugin, config)             # §7.2
        task_id = new_task_id()
        try:
            leases = self._dm.try_acquire_all(task_id, reqs)
        except LeaseUnavailableError as e:
            if self._policy is DispatchPolicy.REJECT:             # 交互场景默认
                return CommandAck(command_id=cmd.command_id, accepted=False,
                                  reason=f"423 locked by {e.holder}")
            self._queue.append(PendingRun(cmd, reqs))             # 夜间批量寻优
            return CommandAck(command_id=cmd.command_id, accepted=True,
                              task_id=task_id, queued=True)
        run_dir = create_run_directory(task_id)
        sink_id = logger.add(                                     # 注意 §8.6 的回收
            run_dir / "experiment.jsonl", serialize=True, enqueue=True,
            filter=lambda r, tid=task_id: r["extra"].get("task_id") == tid)
        ctx = self._build_context(task_id, run_dir, leases)
        asyncio.create_task(self._execute(plugin, config, ctx, leases, sink_id))
        return CommandAck(command_id=cmd.command_id, accepted=True, task_id=task_id)
```

`reject / queue` 是 dispatch 策略而非硬编码：交互操作要 423 立即失败，夜间批量寻优要排队。策略可全局配置，亦可由指令携带覆盖（参考 bluesky-queueserver 的形态，但实现保持极简 FIFO）。

### 8.3 Checkpoint：pause / cancel / 心跳的统一生效点

```python
class RunContext:
    async def checkpoint(self, name: str, **state: float | int | str) -> None:
        self.log.debug("checkpoint", name=name, **state)
        self._dm.touch(self.leases)                       # lease 心跳（§6.4）
        if self._pause_requested.is_set():
            self._set_state(RunState.PAUSED)
            await self._resume_event.wait()
            self._set_state(RunState.RUNNING)
        self.cancel_token.raise_if_cancelled()
        await asyncio.sleep(0)                            # 主动让出 loop
```

约定：实验循环每个迭代至少一次 `checkpoint`（Sweep Helper 自动做，§11.2）。pause 只在 checkpoint 生效——这给了"暂停点必然处于设备安全间隙"的保证；cancel 除 checkpoint 外还经 `context.ensure_not_cancelled()` 达到 Controller 的进行中等待（§4.4），并触发 `stop()` 中止硬件侧动作。

### 8.4 Suspender：越界自动挂起

长时间寻优（数小时的 TPA 编码循环）必须能对环境异常自愈。Suspender 订阅总线上的监控信号，越界→请求 pause；回到范围并稳定 `grace_s`→自动 resume：

```python
class SuspenderConfig(ContractModel):
    watch_topic: Literal["device_health", "progress"]
    metric: str                    # 例: "pump_power_mw"
    min_value: float | None = None
    max_value: float | None = None
    grace_s: Seconds = 5.0
```

典型配置：pump laser 功率跌出阈值 → 挂起 → 功率恢复且稳定 5 s → 从上一个 checkpoint 继续。由于生效点是 checkpoint，实验的 checkpoint 粒度直接决定 Suspender 的响应粒度。

### 8.5 Cleanup：无条件、有序、有超时

```python
async def _execute(self, plugin, config, ctx, leases, sink_id) -> None:
    failed = False
    try:
        await self._stage_all(leases)                     # Controller.stage()
        self._set_state(RunState.RUNNING)
        await plugin.run(config=config, ctx=ctx, **self._inject(leases))
        self._set_state(RunState.COMPLETED)
    except CancelledByUser:
        self._set_state(RunState.ABORTED)
    except Exception:
        failed = True
        ctx.log.exception("run failed")
        self._set_state(RunState.FAILED)
    finally:
        try:
            async with asyncio.timeout(CLEANUP_TIMEOUT_S):    # 清理自身也要有界
                for ctrl in reversed(self._controllers_of(leases)):
                    await ctrl.stop()
                    if failed:
                        await ctrl.safe_state()
                    await ctrl.unstage()
        except Exception:
            ctx.log.exception("cleanup degraded")             # 记录但不再抛
        await ctx.writer.aclose()                             # flush 数据面（§10）
        await self._snapshot_all(ctx.run_dir / "baseline_post.json")
        self._dm.release(ctx.leases)
        logger.remove(sink_id)
        self._emit_final_state(ctx)
        self._maybe_start_next_queued()
```

顺序有讲究：先硬件（stop → safe_state? → unstage），再数据（writer flush/close、post snapshot），最后资源（lease、日志 sink）。清理路径任何一步失败都记录并继续，不允许因清理异常导致租约或 sink 泄漏。

### 8.6 结构化日志规范（含 loguru 陷阱）

loguru 的 `logger.add(filter=...)` 是**全局累积**的：不回收会导致每条日志过一遍所有历史 filter，且句柄泄漏。规则：dispatch 时保存 `sink_id`，cleanup 的 finally 中 `logger.remove(sink_id)`（上两节代码已体现）；sink 一律 `enqueue=True`（跨线程安全 + 不阻塞 loop）、`serialize=True`（JSONL 机读）。所有实验内日志经 `ctx.log = logger.bind(task_id=..., plugin=...)`，保证每条记录可归因到 run。

------

## 9. EventBus：进程内观测总线

### 9.1 语义定位

总线只承载**可丢弃、可重放快照**的观测流：进度、状态、健康度、数据指针、日志摘录。它不承载指令（走 Gateway 点对点，必达），不承载主数据（走 RunWriter，§10）。这一分工使总线可以理直气壮地对慢消费者丢帧，而不背"丢了关键消息"的锅。

### 9.2 实现：fan-out + 每订阅者有界队列

裸 `asyncio.Queue` 是点对点，不是总线。正确形态是 topic → 订阅者集合，每个订阅者一条**有界**队列与显式丢弃策略：

```python
class DropPolicy(StrEnum):
    DROP_OLDEST = "drop_oldest"    # UI 类订阅默认：保新弃旧，计数
    ERROR       = "error"          # 内部严肃订阅者：溢出即暴露 bug

class Subscription:
    def __init__(self, topics, maxsize, policy) -> None:
        self._q: asyncio.Queue[BusEvent] = asyncio.Queue(maxsize)
        self.policy = policy
        self.dropped = 0
    async def __aiter__(self): ...

class EventBus:
    def subscribe(self, topics: Iterable[str], *,
                  maxsize: int = 256,
                  policy: DropPolicy = DropPolicy.DROP_OLDEST) -> Subscription:
        sub = Subscription(topics, maxsize, policy)
        for t in topics:
            self._subs[t].add(sub)
            if t in self._retained:            # 迟到订阅者先收快照再收流
                sub._offer(self._retained[t])
        return sub

    def publish(self, event: BusEvent) -> None:
        assert threading.get_ident() == self._loop_thread_id, \
            "跨线程发布必须走 publish_threadsafe"
        if self._settings.dev_mode:
            assert len(event.model_dump_json()) <= 64_000    # payload 第二道防线
        topic = topic_of(event)
        self._retained[topic] = event                        # retained last event
        for sub in self._subs.get(topic, ()):
            try:
                sub._q.put_nowait(event)
            except asyncio.QueueFull:
                if sub.policy is DropPolicy.DROP_OLDEST:
                    sub._q.get_nowait(); sub._q.put_nowait(event)
                    sub.dropped += 1                         # 暴露为 health 指标
                else:
                    raise BusOverflowError(sub)

    def publish_threadsafe(self, event: BusEvent) -> None:
        self._loop.call_soon_threadsafe(self.publish, event)
```

要点：`publish` 是同步、非阻塞、O(订阅者数) 的——发布者（实验循环）永远不会被慢 UI 拖住；`retained` 使 UI 重连/迟到时立即获得每个 topic 的最新状态（RunState、DeviceHealth 尤其受益）；`dropped` 计数上报 health，长期非零说明订阅端消费能力或节流配置有问题。

### 9.3 发布侧节流（前端降频原则）

高频源（100 fps 探测器）全量进数据面，但进总线的 ProgressEvent 必须在**发布侧**统一降频（如 10 fps），而不是让每个订阅者各自扛：

```python
class ThrottledEmitter:
    def __init__(self, bus: EventBus, min_interval_s: float = 0.1) -> None: ...
    def emit(self, event: BusEvent) -> None:
        """区间内多次调用只保留最后一个事件，到点发布（trailing-edge throttle）。"""
```

`RunContext.emit_progress()` 内置该节流器，插件作者无需关心。

### 9.4 线程边界

worker 线程或 Qt 线程产生的事件一律经 `publish_threadsafe`（内部 `call_soon_threadsafe` 弹回 loop）；loop → Qt 方向由 `UiEventBridge` 承接（§12.5）。总线本体的数据结构只在 loop 线程被触碰，因此无锁。

### 9.5 为什么是 asyncio 而不是 ZeroMQ

结论：**现阶段进程内 asyncio 总线，不引入 ZeroMQ。** 三个理由：

1. 本系统的进程边界在 Gateway（未来对 Tauri 暴露 gRPC/WebSocket），不在核心内部。核内上 ZMQ 只买到当前不需要的进程隔离，代价是 numpy 序列化、重连语义与调试复杂度。
2. 只要执行 §3.4 的指针事件纪律，事件天然小且可序列化——将来更换传输层是 Gateway 一层的事，核心零改动。
3. 真正需要进程隔离的场景是"某台 DLL 设备不稳定拖垮主进程"，那是 per-device daemon（yaq 模式，§17）的 **Driver 层局部决策**，不构成全局总线换 ZMQ 的理由。

Bluesky 同样是这个格局：核内同步订阅，边界用可选的 0MQ RemoteDispatcher。

------

## 10. 数据面：RunWriter 与存储规范

### 10.1 原则

主数据在实验循环内经 `await ctx.writer.append_*()` 直写——`await` 一条有界队列，写盘慢时**背压自然传导**到实验循环使其减速，而不是丢数据或撑爆内存。h5py 是单写者模型：每个 run 一个 `RunWriter` 实例、一个专属 writer task，是该 HDF5 文件的**唯一**写入者。UI 实时可视化消费总线上的 preview；确需读原始文件时启用 SWMR（writer 开 `swmr_mode`，读者只读附着）。

```python
class RunWriter:
    async def append_array(self, dataset: str, arr: np.ndarray,
                           attrs: ContractModel | None = None) -> DataPointer:
        """追加至可扩展 dataset（axis 0 可变、chunked、lzf 压缩）。
        返回 DataPointer 供发指针事件。队列满时 await → 背压。"""
    async def append_metric(self, row: "MetricRow") -> None: ...
    async def aclose(self) -> None: ...

class DataPointer(ContractModel):
    run_id: RunId
    dataset: str
    index: int
```

### 10.2 Run 目录布局

```text
runs/2026-07-07T15-30-12_org.lab.tpa_multiplier_a3f9/
  ├── run.json              RunManifest：config 全文 + hash、git commit、
  │                         代码版本、仪器 identity/serial、settle 参数、LUT id
  ├── baseline_pre.json     run 前全设备 snapshot（DeviceManager.snapshot_all）
  ├── baseline_post.json    run 后全设备 snapshot
  ├── experiment.jsonl      loguru 结构化日志（含 checkpoint 与每次驱动调用耗时）
  ├── metrics.jsonl         运行中标量时序（追加安全、崩溃安全）
  ├── metrics.parquet       finalize 时由 jsonl compact 生成（pyarrow），列式极速查询
  └── artifacts.h5          矩阵数据：traces、masks、camera frames
```

Parquet 不可追加是硬事实，两条路线：默认**运行中 JSONL、结束时 compact**（简单、崩溃安全，崩溃后 jsonl 仍可离线 compact）；高频指标场景可选 pyarrow 增量写 row-group 的 `ArrowMetricsWriter`。

### 10.3 数组存储策略

- **相位掩膜按面板原生量化级存储**：SLM-200 为 10-bit → `uint16`，配 `RunManifest.lut_id` 引用相位标定 LUT。相比存 float64 弧度体积降 4 倍，且消除"存的相位"与"打的电平"之间的二义性。
- **参数化掩膜只存配方**：随机搜索/优化器生成的掩膜存 `MaskRecipe(generator, version, seed, params)`（ContractModel，入 h5 attrs），需要时重建。对长寻优 run 这是数量级的空间差异；抽检若干 step 同时存原帧用于校验重建一致性。
- **trace 存 y 本体（float32 足够覆盖 dBm 动态范围）**；x 轴由 `scan` 配置参数化重建，config 存 attrs，避免每条 trace 重复存一份相同的波长数组。

### 10.4 双时间戳与对齐

每条事件、每个数据行、每个 checkpoint 同时带 `t_wall`（RFC3339，供人）与 `t_mono_ns`（`time.monotonic_ns()`，供机器）。SLM 更新与 OSA trace 的因果对应、驱动调用耗时分析，一律以单调钟为准——墙钟会被 NTP 拨动。

### 10.5 可复现性检查单

一个 run 目录必须自足回答：跑的是哪版代码（git commit + dirty 标记）、哪套配置（全文 + hash）、哪几台真实设备（identity/serial）、设备当时什么状态（pre/post baseline）、每一步何时发生（双时间戳日志）、大数据在哪如何重建（datasets + recipes + LUT id）。缺任何一项即视为数据面 bug。

------

## 11. 实验插件层与 Sweep Helper

### 11.1 插件形态

实验是完全独立的插件：不 import 任何 Driver/Controller，不持有任何 UI 引用，只订阅指令、只依赖能力、只向 writer 写数据、只向总线发观测。以 TPA 乘法器寻优为例（修正 v1 的 `TPACalfig` typo）：

```python
@register(plugin_id="org.lab.tpa_multiplier")
class TPAMultiplierPlugin(Plugin):
    config_type = TPAConfig            # Gateway 校验 payload 的依据

    @on_command("start_tpa_run")
    async def run(
        self,
        config: TPAConfig,
        ctx: RunContext,
        slm: PatternModulator = Depends(role="primary_slm"),
        osa: SpectrumAnalyzer = Depends(role="main_osa"),
    ) -> None:
        ctx.log.info("TPA 编码循环启动", max_steps=config.max_steps)
        spec = slm.get_frame_spec()
        rng = np.random.default_rng(config.seed)

        for step in range(config.max_steps):
            await ctx.checkpoint("tpa_step", step=step)        # pause/cancel/心跳生效点

            recipe = MaskRecipe(generator="uniform_random", version="1",
                                seed=config.seed, params={"step": step})
            frame = make_mask(rng, spec)                        # uint16 原生量化级
            await slm.display_pattern(frame, context=ctx)       # 返回即 settled

            trace = await osa.acquire_trace(config.trace_request, context=ctx)

            ptr = await ctx.writer.append_array(                # 数据面直写（背压）
                "traces/spectrum", trace.y_dbm, attrs=trace.meta)
            if step % 100 == 0:                                 # 抽检存原帧，校验 recipe 重建
                await ctx.writer.append_array("masks/spot_check", frame, attrs=recipe)
            ctx.emit_progress(step=step, total=config.max_steps,
                              metrics={"peak_dbm": float(trace.y_dbm.max())},
                              pointer=ptr, preview=trace.preview())   # 节流后上总线
```

对比 v1 版本的三处本质变化：`display_pattern` 之后不再需要担心 settle；光谱不再作为事件广播而是落盘 + 指针；循环内出现 `checkpoint`，run 因此天然支持 pause/suspend/心跳。

### 11.2 Sweep Helper：把 300 行 for-loop 变成 20 行声明

90% 的实验是"嵌套扫描 × 采集 × 落盘"。平台提供声明式扫描原语，checkpoint、取消、写入、进度节流全部内置：

```python
class ScanAxis(ContractModel):
    name: str
    values: tuple[float, ...] = Field(min_length=1)

async def grid_scan(
    ctx: RunContext,
    axes: Sequence[ScanAxis],
    apply: Callable[[dict[str, float]], Awaitable[None]],     # 设定一个格点
    acquire: Callable[[], Awaitable[SpectrumTrace]],          # 采一个点
    *,
    dataset: str = "traces/grid_scan",
) -> None:
    points = list(itertools.product(*(a.values for a in axes)))
    for i, combo in enumerate(points):
        point = {a.name: v for a, v in zip(axes, combo)}
        await ctx.checkpoint("scan_point", index=i, **point)
        await apply(point)
        trace = await acquire()
        ptr = await ctx.writer.append_array(dataset, trace.y_dbm,
                                            attrs=ScanPointMeta(index=i, point=point,
                                                                trace=trace.meta))
        ctx.emit_progress(step=i, total=len(points), pointer=ptr,
                          preview=trace.preview())
```

后续按需扩展 `adaptive_scan`（下一格点由回调基于历史结果决定，覆盖寻优类实验）；扩展发生在 helper 层，插件作者面对的心智模型不变。

### 11.3 子流程组合

依托 §6.3 的 lease 继承，子流程就是普通 async 函数——共享 ctx、复用父租约、可独立测试：

```python
async def calibrate_slm_response(ctx: RunContext,
                                 osa: SpectrumAnalyzer,
                                 slm: PatternModulator) -> CalibrationResult:
    ...   # 内部同样使用 ctx.checkpoint / ctx.writer，无需重新申请任何设备

# 主实验中直接调用：
cal = await calibrate_slm_response(ctx, osa, slm)
```

若子流程以独立 task 派发（少见），TaskManager 以 `parent=ctx.leases` 调用 `try_acquire_all`，同设备依赖自动继承。

------

## 12. 线程与并发模型

这是第一周就会撞的墙，必须在写第一行驱动前定死。

### 12.1 三层线程域

```text
[Qt 主线程]            仅 UI。禁止任何硬件/文件 IO。
      ▲ Qt Signal (QueuedConnection)      │ call_soon_threadsafe
      │  UiEventBridge                    ▼
[asyncio loop 线程]    Gateway·EventBus·TaskManager·Controller·RunWriter task。
      │ await worker.call(fn, *args)      ▲ future.set_result (threadsafe)
      ▼                                   │
[每设备 worker 线程]   阻塞调用的唯一居所：VISA·socket·串口·厂商 DLL。
```

选择独立 loop 线程而非 qasync 单线程方案：UI 不受 loop 停顿影响；且该拓扑与未来 gRPC 进程边界**同构**——迁移时只是把线程间的桥换成网络，控制流不变。

### 12.2 loop 线程纪律

loop 内禁止任何可能阻塞 >10 ms 的调用（PyVISA、DLL、大文件同步 IO、重型 numpy 归约）。开发期开启 `loop.slow_callback_duration` 与 debug 模式，慢回调直接告警。重型数值计算（JAX 编译、批量拟合）按需下放 `asyncio.to_thread`。

### 12.3 BlockingDeviceWorker：每台阻塞设备一个专职线程

PyVISA 是阻塞的；共享线程池会让一台慢设备饿死其他设备。每台设备独占一个 worker，Driver 的 async 方法即"向该线程投递 + await future"：

```python
class BlockingDeviceWorker:
    def __init__(self, name: str) -> None:
        self._jobs: queue.Queue[tuple] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    async def call(self, fn: Callable[..., T], *args, **kwargs) -> T:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[T] = loop.create_future()
        self._jobs.put((fn, args, kwargs, fut, loop))
        return await fut

    def _loop(self) -> None:
        while True:
            fn, args, kwargs, fut, loop = self._jobs.get()
            try:
                result = fn(*args, **kwargs)
                loop.call_soon_threadsafe(fut.set_result, result)
            except BaseException as exc:
                loop.call_soon_threadsafe(fut.set_exception, exc)
```

由 `deps.worker_pool.for_device(instrument_id)` 统一发放（§5.2），保证"一设备一线程"的不变量。Controller 的 operation lock 留在 async 侧不变——它保护的是操作序列的原子性，与线程无关。

### 12.4 DLL 消息泵线程（Santec SLM-200）

带 Win32 消息泵的厂商 DLL 必须由**创建它的那个线程**独占服务。该设备的 worker 线程在 job 处理间隙持续泵消息：

```python
def _loop(self) -> None:
    self._dll = load_santec_dll(self._cfg)        # DLL 在本线程初始化
    while not self._stopping:
        self._pump_win32_messages()               # PeekMessage / Dispatch
        try:
            job = self._jobs.get(timeout=0.005)
        except queue.Empty:
            continue
        self._run_job(job)
```

DLL 句柄绝不跨线程传递；loop 侧所有交互一律经 `worker.call`。

### 12.5 Qt 桥

进 loop：Panel 发指令 `loop.call_soon_threadsafe(gateway.submit, envelope)`。出 loop：**必须**经 Qt Signal——从 loop 线程直接触碰 widget 会随机崩：

```python
class UiEventBridge(QObject):
    event_received = Signal(object)               # QueuedConnection 跨线程投递

    def start(self, bus: EventBus, loop: asyncio.AbstractEventLoop) -> None:
        sub = bus.subscribe(["progress", "run_state", "data_pointer",
                             "device_health", "error"],
                            policy=DropPolicy.DROP_OLDEST)
        async def pump() -> None:
            async for ev in sub:
                self.event_received.emit(ev)      # emit 本身线程安全
        asyncio.run_coroutine_threadsafe(pump(), loop)
```

Panel 在 Qt 线程 `bridge.event_received.connect(self.on_event)`，pyqtgraph 更新全部发生在 Qt 线程。

------

## 13. Gateway 与 UI Shell 契约

### 13.1 指令通道

UI 下发的一切都是 `CommandEnvelope`；Gateway 只做三件事：反序列化 → 查命令注册表（`command → plugin`）→ 交给 TaskManager（payload 的契约校验发生在 dispatch，§8.2）。回执是 `CommandAck`。

```python
class CommandEnvelope(ContractModel):
    command_id: str
    command: str                       # "start_tpa_run" / "pause" / "cancel"
    payload: dict[str, object] = Field(default_factory=dict)
    issued_by: str = "local_ui"
    t_wall: AwareDatetime

class CommandAck(ContractModel):
    command_id: str
    accepted: bool
    task_id: TaskId | None = None
    queued: bool = False
    reason: str | None = None          # 例: "423 locked by task_..."
```

`pause / resume / cancel` 是平台内建指令，直达 TaskManager 状态机，不经插件。

### 13.2 Panel 契约

`QMainWindow` 与 Dock/Tab 内的 Panel 只做两件事：把表单组装为强类型 Config 经 `gateway.send_command()` 下发；经 `UiEventBridge` 订阅事件更新进度条与 pyqtgraph。UI 层**绝对禁止** import 任何 Driver/Controller 模块或直接调用仪器实例——该禁令由 import-linter 在 CI 强制（§18）。

### 13.3 迁移到 Tauri / gRPC 的兑现路径

替换只发生在 Gateway 一层：`CommandEnvelope` ↔ gRPC unary；`GatewayEvent` 判别联合 ↔ server-streaming。事件与指令模型的 JSON Schema（§3.6）导出后 codegen 出 TypeScript 类型，前后端契约同源。核心（插件、TaskManager、Controller、数据面）零改动——这是本架构所有隔离投资兑现的地方。

------

## 14. Mock、物理仿真与 CI

### 14.1 四级替身体系

| 级别    | 替身                                            | 验证对象                            |
| ------- | ----------------------------------------------- | ----------------------------------- |
| L1 单元 | `MockScpiTransport`（脚本化问答）               | Driver 的命令格式化与解析           |
| L2 回归 | `TranscriptReplayTransport`（真机会话录制回放） | Driver 对真实回复的兼容性           |
| L3 集成 | `MockController`（固定/参数化返回值）           | DI、锁、TaskManager、总线、存储链路 |
| L4 闭环 | **物理仿真后端**（见下）                        | 完整实验逻辑与优化器行为            |

### 14.2 物理仿真闭环：把 TPA 仿真挂到 Mock 后面

Mock 不回固定 trace，而是根据"当前 SLM 掩膜"计算物理自洽的光谱。已有的五模块 TPA 乘法器仿真封装为 `tpa_model` 后端，Sim 设备共享一个 `SimContext`：

```python
class SimContext:
    """同一 run 内 Sim 设备间的共享物理态。"""
    def __init__(self, model: TpaPhysicsModel) -> None:
        self.model = model
        self.current_mask: np.ndarray | None = None

class SimPatternModulator(InstrumentController):
    async def display_pattern(self, frame, *, context) -> None:
        validate_frame(frame, self._spec)
        self._sim.current_mask = frame
        await asyncio.sleep(self._options.settle_ms / 1000)     # 连 settle 都仿真

class SimSpectrumAnalyzer(InstrumentController):
    async def acquire_trace(self, request, *, context) -> SpectrumTrace:
        y = self._sim.model.spectrum(self._sim.current_mask, request.scan)
        y = self._sim.model.add_shot_noise(y, request.scan)
        await asyncio.sleep(self._sim.model.sweep_time_s(request.scan))
        return self._to_domain_trace(y, request)
```

配置侧 `backend = "sim"` 即完成切换（§3.5），Factory 据此选择 Sim Controller 并注入共享 `SimContext`。至此，**plugin → SLM → OSA → optimizer 的整条闭环可离线端到端运行**，DI、租约、总线、HDF5 存储全链路同时被验证；优化器的收敛行为可以先在仿真上调好再上真机。

### 14.3 CI Gate

CI 必跑：L1/L2 全量；L4 全链路 sim 下执行一次 20 格点 `grid_scan` + 一次含 pause/resume/cancel 的 TPA 短 run，断言：run 目录完整（§10.5 检查单逐项）、无租约泄漏、无 sink 泄漏、总线 `dropped` 计数为零、cleanup 走完。开发笔记本脱离实验室硬件即可全功能开发，是"离线 Mock 先行"原则的落地形态。

------

## 15. 统一错误模型

Driver 保留完整厂商错误细节；Controller 向系统输出统一领域错误：

```python
class InstrumentError(Exception): ...
class InstrumentConnectionError(InstrumentError): ...
class InstrumentTimeoutError(InstrumentError): ...
class InstrumentProtocolError(InstrumentError): ...
class DeviceReportedError(InstrumentError): ...
class InvalidInstrumentStateError(InstrumentError): ...
class SafetyViolationError(InstrumentError): ...
class UnsupportedCapabilityError(InstrumentError): ...
class InstrumentContractError(InstrumentError): ...      # v2：契约校验失败
class LeaseUnavailableError(Exception): ...              # 资源层，非仪器错
```

映射示例：VISA `TimeoutError` / SDK HRESULT 超时码 → `InstrumentTimeoutError`；SCPI error queue 非空 → `DeviceReportedError`。映射时保留 resource name、最后一条命令与原始 exception 作为诊断上下文（进 `experiment.jsonl`）；同时向总线发 `ErrorEvent`（脱敏为类型 + 消息）。

系统默认不暴露 Driver 或任意 raw SCPI 写入能力。确有调试需求时，提供经权限控制、审计记录、显式命名的 diagnostic capability（如 `org.lab.diag.v1.raw-scpi`，默认不注册，仅维护模式启用），而不是允许 UI 直发任意命令。

------

## 16. 端到端生命周期

```text
[ PyQt Panel ]  构造强类型 Config → CommandEnvelope → call_soon_threadsafe → Gateway
      ↓
[ Gateway ]     命令注册表路由 → TaskManager.dispatch
      ↓
[ TaskManager ] payload 契约校验 → DI 解析（Depends/role/绑定表）
              → DeviceManager.try_acquire_all（原子；失败 → 423 或入队）
              → 建 run 目录 / RunManifest / baseline_pre snapshot
              → logger.add(sink) 记录 sink_id → 构造 RunContext（含 RunWriter）
              → stage() 全部设备 → RunState: RUNNING
      ↓
[ Plugin ]      循环：checkpoint（pause/cancel/心跳生效点）
              → display_pattern（返回即 settled）→ acquire_trace
              → writer.append（数据面，背压）→ emit_progress（节流 → 总线指针事件）
      ↓                                    ↘
[ EventBus ]    fan-out → UiEventBridge → Qt Signal → Panel 刷新
              → Suspender 监控 → 越界请求 pause / 恢复后 resume
      ↓
[ 结束/异常/取消 ]
              → finally（有超时）：stop() → [失败时 safe_state()] → unstage()
              → writer flush/close → baseline_post → 释放租约（引用计数归零）
              → logger.remove(sink_id) → 终态 RunStateEvent → 启动队列中下一个 run
```

------

## 17. 演进路线与止损判据

**Phase 0（当前）**：类型契约层 + EventBus + TaskManager/DeviceManager + L1–L3 Mock，全链路离线跑通。 **Phase 1**：AQ6370 与 SLM-200 真机 Driver/Controller 上线（含 DLL 消息泵 worker）；TPA 编码实验迁入插件。 **Phase 2**：Sweep Helper、Suspender、queue 策略、L4 物理仿真闭环进 CI。 **Phase 3**：Gateway 外化为 gRPC/WebSocket，Tauri/React 前端接入；PyQt 与新前端并行期靠同一事件联合。 **Phase 4（按需触发）**：某台设备（大概率是 DLL 设备）稳定性拖累主进程时，将其 Driver+worker 下沉为独立 per-device daemon（yaq 形态：每仪器一进程，RPC over TCP）。这是 Driver 层局部决策，Controller 以上无感。

**采用 Bluesky 的止损判据**：本方案自研的合理性建立在 Qt 集成、Santec DLL、Tauri/gRPC 三个约束之上。但若发现自己在重新实现 RunEngine 级别的能力——checkpoint **rewind**（暂停后回滚重放若干步）、计划级 re-plan、跨 run 的资源图调度——立即停下，重新评估直接采用 Bluesky/ophyd-async 并把本平台降级为其 Qt 外壳。已从 Bluesky 吸收且不再重复发明的四件事：checkpoint 式 pause/resume、suspender、settled 完成语义（Status 思想）、baseline snapshot。

**硬件触发边界**（重申 §1.3）：软件循环只做编排级时序；µs 级同步（脉冲对齐、门控）走硬件触发线，软件 arm/read。任何"用更快的软件循环逼近硬同步"的尝试都是路线错误。

------

## 18. 硬性规则（Architecture Invariants）

以下规则违反即打回，不接受"临时方案"：

1. 大数组不上总线。事件必须是 `ContractModel`（类型层已强制），单事件序列化 ≤ 64 KB。
2. 主数据只经 `RunWriter` 落盘；每个 HDF5 文件唯一写者；总线只发 `DataPointer` + 降采样 preview。
3. 实验插件零锁代码、零 Driver/Controller import、零 UI 引用；设备一律经 `Depends` 注入。
4. 每个 Controller 必须实现 `stop()/safe_state()`，多命令操作必须在同一把 operation lock 内。
5. 运动/显示类动作返回即 settled；settle 参数入配置并写入 RunManifest；实验代码禁止手写补偿 sleep。
6. 一切跨边界数据（指令、事件、capability 请求/响应、配置、run 元数据）必须是 `ContractModel`；capability 校验只在 Registry 收口。
7. 阻塞调用只存在于 per-device worker 线程；DLL 由创建线程独占并自带消息泵；loop 内无 >10 ms 阻塞。
8. 租约获取 try-all-or-release-all、无 hold-and-wait；子流程经 lease 继承复用父租约；checkpoint 兼作心跳。
9. UI 只经 Gateway 与 EventBus 与系统交互；loop → Qt 必须走 Qt Signal。
10. 每个 run 目录满足 §10.5 可复现性检查单。
11. loguru sink 按 run 添加、finally 移除；一律 `enqueue=True` + JSONL。
12. 全链路必须可在 `backend = "sim"` 下运行；L4 闭环是 CI gate。
13. 分层依赖以 import-linter 契约固化：`ui → gateway → plugin → task/device → controller → driver → transport`，禁止逆向与跨层 import。

------

## 附录 A：参考系统对照表

| 本平台概念                   | Bluesky/Ophyd                    | PyMeasure  | QCoDeS             | yaq      | AstrBot（形态借鉴） |
| ---------------------------- | -------------------------------- | ---------- | ------------------ | -------- | ------------------- |
| Controller                   | ophyd Device                     | Instrument | Instrument         | daemon   | —                   |
| Capability Protocol          | Readable/Movable/Stoppable       | —          | —                  | trait    | —                   |
| TaskManager                  | RunEngine                        | Worker     | —                  | —        | 指令调度            |
| checkpoint/suspender         | RunEngine checkpoint / Suspender | —          | —                  | —        | —                   |
| EventBus 事件                | event-model documents            | signals    | —                  | —        | 事件总线            |
| baseline snapshot            | baseline stream                  | —          | Station.snapshot() | —        | —                   |
| @register 插件 + DI          | plan                             | Procedure  | —                  | —        | 插件 + Depends      |
| per-device daemon（Phase 4） | —                                | —          | —                  | 核心形态 | —                   |

借鉴 AstrBot 的是插件注册、命令路由与 DI 的人体工学；**不**借鉴"一切走总线"——聊天事件小、可丢、handler 近似无状态；实验室硬件状态持久且危险、数据一个不能丢、任务长期独占资源、取消必须到达硬件。控制面/数据面分离（§2）正是这一差异的架构表达。

## 附录 B：术语表

- **ContractModel**：可序列化、不可变、严格校验的跨边界数据基类（§3.2）。
- **数据面对象**：进程内流动、可含 ndarray、永不上总线的对象（§3.1）。
- **Capability**：版本化的能力描述符（id + 请求/响应契约类型）；Handler 为其型号实现；Registry 为在场设备的能力清单（§4.5）。
- **Lease / LeaseSet**：设备独占租约及其引用计数集合，支持父子继承（§6）。
- **checkpoint**：pause、cancel、lease 心跳、让出 loop 的统一生效点（§8.3）。
- **Suspender**：监控信号越界自动挂起、恢复后自动继续的守护器（§8.4）。
- **settled**：硬件动作返回即物理稳定的完成语义（§4.4）。
- **RunWriter / DataPointer**：run 级唯一数据写者与其发往总线的定位凭据（§10.1）。
- **retained event**：总线为每 topic 保留的最新事件，供迟到订阅者获取快照（§9.2）。
