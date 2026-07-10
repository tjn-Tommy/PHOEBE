# Transport 与协议 Workflow

`core/transport.py` 定义 `ScpiTransport` 协议和 IEEE 488.2 binary block helper；`phoebe/transports/` 提供可替换实现。Transport 只解决“怎样收发”，不决定设备业务语义。

## 实现地图

| 文件 | 适用场景 | 关键行为 |
| --- | --- | --- |
| `core/transport.py` | 公共协议 | `open/close/write/query`、binary block 打包/解析 |
| `transports/tcp.py` | raw TCP/Telnet SCPI | framing、编码、连接/读取超时 |
| `transports/visa.py` | VISA/VXI-11/HiSLIP 等 | 延迟导入 PyVISA，保持 sim/test 不依赖它 |
| `transports/mock.py` | 单元测试 | `MockScpiTransport` 规则匹配和 `TranscriptReplayTransport` |
| `core/worker.py` | blocking backend | 在线程中执行阻塞 SDK，再把 Future 交还 asyncio loop |

## 真实设备调用路径

```text
Controller coroutine
  -> Driver method (pure command translation)
  -> ScpiTransport.query/write
  -> BlockingDeviceWorker / VISA / TCP
  -> response parse
  -> typed domain result
```

`async` 不表示底层 I/O 一定非阻塞；它表示阻塞部分已经被封装进 worker，不应堵住 PHOEBE 的专用 asyncio loop。

## Binary block Workflow

```text
payload bytes
  -> make_ieee_block()
  -> b"#<digit-count><payload-length><payload>"
  -> instrument
  -> raw reply
  -> parse_ieee_block()
  -> payload bytes
```

截断、空回复、缺少 `#`、长度字段非法都应转换为 `InstrumentProtocolError`，让上层按稳定的错误类型处理。

## Mock 与 replay

- `MockScpiTransport` 适合验证 Driver 是否发送正确的命令和参数。
- `TranscriptReplayTransport` 适合保存真实设备的脱敏会话，验证固件差异、错误回复和读取顺序。
- transcript 不应包含密码、真实网络凭据或完整实验数据；每份 fixture 应附型号、firmware、录制时间和期望状态。

## 选择 Transport 的规则

```text
设备配置 transport = visa/tcp/sdk/vendor_dll
             │
             ├─ real: Factory 注入对应 Transport/SDK
             └─ sim:  Factory 注入 Sim Controller
```

新设备不要为了复用方便而伪装成 SCPI：例如 Santec DLL 和 NI-DAQ 应直接包住其 SDK，但仍遵守 Controller/Worker/Capability 边界。VISA、nidaqmx 和 PyQt 都应保持 lazy import，使 sim 和离线测试不需要安装硬件依赖。

