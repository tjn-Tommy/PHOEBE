# 测试与验证 Workflow

测试应按“越靠近硬件越可替换、越靠近用户越接近全链路”分层。默认 sim 后端不应需要 VISA、NI-DAQ 或 PyQt 才能运行核心测试。

## 当前测试地图

| 测试文件 | 层级 | 覆盖内容 |
| --- | --- | --- |
| `test_contracts.py` | L0 | strict model、边界校验、配置和 role binding |
| `test_bus_and_worker.py` | L0/L1 | EventBus 队列、丢帧、worker 线程交接 |
| `test_lease_and_di.py` | L1 | lease 原子性、TTL、父子上下文、依赖注入 |
| `test_drivers_l1.py` | L1 | Mock transport 下的 Driver 协议命令 |
| `test_e2e_sim.py` | L4 | Plugin → SLM sim → OSA sim → writer 的闭环 |
| `conftest.py` | fixture | sim config、临时 run root、异步测试设置 |

## 推荐验证层级

```text
L0  Contract / pure helper / error mapping
 └─ L1  Driver + MockScpiTransport
     └─ L2  TranscriptReplayTransport (real session, sanitized)
         └─ L3  Plugin + SimContext + pause/cancel/cleanup
             └─ L4  full runtime: config -> factory -> run -> artifacts
```

### L0：契约和纯函数

验证字段范围、单位、schema version、错误类型、IEEE block 和 `grid_scan` 的点序列。测试不应启动线程或连接设备。

### L1：Driver 协议

使用 `MockScpiTransport` 检查命令顺序、参数格式、错误响应和 binary block 解析。Controller 的锁、settled 和 cleanup 可以用短的 fake transport 验证。

### L2：真实会话回放

在真实设备上录制最小 connect/identity/configure/acquire/error 会话，脱敏后交给 `TranscriptReplayTransport`。它用于固件兼容性回归，不取代真实设备 smoke test。

### L3：插件仿真

运行每个插件的完整 sim workflow，并注入：pause、resume、cancel、设备错误、writer 异常、lease TTL 和 cleanup 异常。检查最终状态、run manifest、数据 pointer 和资源释放。

### L4：离线全链路

```text
sim TOML
  -> load_app_config
  -> build_runtime(connect=False)
  -> load_builtin_plugins
  -> Gateway.submit
  -> TaskManager / DI / lease
  -> Sim Controller
  -> RunWriter
  -> assert run.json + datasets + final state
```

## 常用命令

```powershell
# 在项目约定的 phoebe 环境中运行全套测试
conda run -n phoebe python -m pytest tests/ -q

# 只跑 sim 闭环
conda run -n phoebe python -m pytest tests/test_e2e_sim.py -q

# 运行无 GUI 的 demo
conda run -n phoebe python examples/run_sim_demo.py

# GUI 在支持 PyQt5 的环境中启动 sim
conda run -n phoebe python -m phoebe.ui.app --config config/sim.toml
```

## 新功能的 Definition of Done

- 有一个纯 contract/config 测试。
- 有 Mock 或 sim 路径，不需要真实仪器才能验证核心逻辑。
- 有 pause/cancel/异常清理测试。
- 完整数据落到 RunWriter，EventBus 只发小事件或 pointer。
- UI 只通过 Gateway 发送命令，未引入反向 import。
- 若新增设备，补 L1；若有真实会话，补 L2；若新增实验，补 L3/L4。

