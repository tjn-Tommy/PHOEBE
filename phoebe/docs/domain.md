# `phoebe.domain`：实验领域模型

`phoebe/domain/` 只描述“实验要表达什么数据和参数”，不负责打开 VISA、调用 DLL 或更新 Qt 控件。这里的模型大多继承 `ContractModel`，可以跨 Gateway、Plugin、Controller 和 RunWriter 边界。

## 模块地图

| 文件 | 模型方向 | 使用位置 |
| --- | --- | --- |
| `pattern.py` | SLM 掩膜、frame、量化和 pattern recipe | SLM capability、TPA/Grid plugin |
| `spectrum.py` | scan request、trace、峰值和光谱元数据 | OSA Controller、sim、plot preview |
| `scope.py` | 触发、通道、采集和示波器 trace | RTO6 Controller/Driver |
| `daq.py` | sample rate、duration、analog read trace | NI-DAQ Controller/Driver |
| `awg.py` | analog channel、marker、trigger、output setup | Tek AWG Controller/Driver |

## 领域数据 Workflow

```text
Panel / TOML / Plugin config
       │
       ▼
ContractModel (units + range + cross-field validation)
       │
       ├─ Controller.configure/acquire -> real or sim instrument
       ├─ Driver translates only protocol details
       └─ RunWriter stores trace + model metadata + timestamps
```

参数对象和测量对象要分开：配置决定“怎样测”，trace/metadata 表示“测到了什么”。完整数组进入 data plane；EventBus 只收到 `DataPointerEvent` 和受限 preview。

## 重要约束

- 尽量在模型中表达单位、范围、离散枚举和最小/最大长度。
- 不在 domain model 中保存 Controller、Transport、Qt widget 或线程对象。
- 可重建的 pattern 只保存 seed、recipe 和 generator version；不能把无法解释的隐式状态当作实验输入。
- 任何会影响物理含义的选项都应写入 `RunManifest`，例如 SLM settle time、LUT id、OSA scan、设备 serial。

## 典型 TPA/扫描路径

```text
SpectrumScanConfig
  -> SLM display_pattern(frame)
  -> Controller settled
  -> OSA acquire_trace(request)
  -> SpectrumTrace
  -> RunWriter.append_array("traces/...", trace.y_dbm)
  -> DataPointerEvent(preview)
```

`core/sweep.py` 的 `ScanAxis` 和 `grid_scan()` 只是扫描编排；具体的 SLM frame、OSA trace 或优化参数仍属于 domain/plugin，而不是 core 中的硬件命令。

