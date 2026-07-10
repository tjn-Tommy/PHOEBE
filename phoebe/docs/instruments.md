# `phoebe.instruments`：设备适配层

设备适配采用组合而不是深层继承：

```text
Transport -> Driver -> Controller -> Capability Protocol
                                  ▲
                            Plugin / DI role
```

## 目录与职责

| 路径 | 设备 | Driver 负责 | Controller 负责 |
| --- | --- | --- | --- |
| `santec_slm200/` | Santec SLM-200 | DLL/命令和 frame 传输 | settle、分辨率、safe state、SLM capability |
| `yokogawa_aq637x/` | Yokogawa AQ637X OSA | Telnet/SCPI 配置和 trace 读取 | acquisition 原子锁、scan 生命周期 |
| `rs_rto6/` | R&S RTO6 scope | SCPI channel/trigger/waveform | configure、trigger、monitor 和采集语义 |
| `ni_daq/` | NI-DAQ | nidaqmx 调用 | sample/read trace、设备状态 |
| `tek_awg5204/` | Tektronix AWG5204 | SCPI waveform/marker/trigger | output setup、safe shutdown |
| `sim/` | 所有五类能力 | 无真实 I/O | 共享 `SimContext` 的物理闭环 |

公共入口：

- `protocols.py`：五类 capability Protocol；实验只依赖这些接口。
- `registry.py`：注册内置 Factory；由 `kind/vendor/model` 选择实现。
- 每个设备目录的 `driver.py` 是纯协议翻译，不能负责锁、lease 或 asyncio 编排。
- 每个设备目录的 `controller.py` 负责操作原子性、settled 语义和安全状态。

## 文件级索引

| 文件 | 说明 |
| --- | --- |
| `protocols.py` | `PatternModulator`、`SpectrumAnalyzer`、`Oscilloscope`、`Daq`、`WaveformGenerator` 等 capability Protocol |
| `registry.py` | `register_builtin_factories()`，把所有内置型号注册到 FactoryRegistry |
| `santec_slm200/driver.py` | SLM vendor DLL/设备线程调用 |
| `santec_slm200/controller.py` | SLM frame、settle、panel identity 和安全状态 |
| `santec_slm200/csvio.py` | Santec 厂商 CSV 的读写边界 |
| `yokogawa_aq637x/driver.py` | OSA SCPI/Telnet 协议翻译 |
| `yokogawa_aq637x/controller.py` | scan、trigger、trace acquisition 原子操作 |
| `rs_rto6/driver.py` | RTO6 channel、trigger、waveform 和 measurement 命令 |
| `rs_rto6/controller.py` | scope 配置、monitor 和采集生命周期 |
| `ni_daq/driver.py` | nidaqmx 调用封装 |
| `ni_daq/controller.py` | analog read/sample capability |
| `tek_awg5204/driver.py` | AWG waveform、marker、trigger SCPI |
| `tek_awg5204/controller.py` | AWG output setup 和安全停止 |
| `sim/context.py` | 共享的 mask/optical state 和简化物理关系 |
| `sim/controllers.py` | 五类 capability 的无硬件 Controller |

## 启动与连接 Workflow

```text
AppConfig.instruments
       │ kind + vendor + model + backend
       ▼
ControllerFactoryRegistry
       ├─ backend = real -> vendor Controller + Driver + Transport
       └─ backend = sim  -> Sim Controller + shared SimContext
       ▼
DeviceManager.start()
       ├─ construct
       ├─ connect/open
       ├─ identity check (*IDN? / serial / SDK identity)
       ├─ stage/ready
       └─ publish DeviceHealthEvent
```

配置文件不是设备事实来源。型号、serial 或 SDK identity 不匹配时，Controller 不能进入 ready。

## 一次硬件操作的 Workflow

```text
Plugin capability call
  -> Controller operation lock
  -> Driver configure / trigger / poll / read
  -> Controller settled / timeout / metadata
  -> RunContext writer + EventBus pointer
```

例如 OSA acquisition 的 configure、trigger、等待和读取必须在同一个 Controller 原子操作中，避免两个 run 的 SCPI 命令交错。SLM `display_pattern()` 返回时代表 DLL 调用和配置的 settle 时间都完成；实验代码不能手写补偿 `sleep`。

## 添加新型号

1. 在 `domain/` 定义或复用 request/trace contract。
2. 在 `instruments/<vendor_model>/driver.py` 实现协议翻译。
3. 在同目录 `controller.py` 实现 capability、锁、settled 和 safe state。
4. 在 `registry.py` 注册 factory，并加入 identity 校验。
5. 在 `sim/` 或 Mock/replay 中提供离线路径。
6. 增加 L1 driver test、L2 transcript（如有真机）和 L4 sim 验证。

禁止：在插件里 import Driver、从 UI 直接拿 Controller、用 `time.sleep` 代替 settled、把 vendor SDK import 到公共 core。
