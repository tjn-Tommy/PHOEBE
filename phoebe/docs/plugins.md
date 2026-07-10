# Experiment Plugins

插件是实验编排层，不是设备驱动层。当前显式加载入口是 `phoebe/plugins/__init__.py` 的 `load_builtin_plugins()`；它导入模块时触发 `@register(...)`，这是可审计的注册动作。

## 当前插件

| 文件 | 命令 | 作用 |
| --- | --- | --- |
| `plugins/tpa_multiplier.py` | `start_tpa_run` | SLM 显示、OSA 采集、checkpoint、指标和优化循环 |
| `plugins/spectrum_grid.py` | `start_grid_scan` | 按 SLM level 网格扫描 OSA spectrum |

## 注册与运行路径

```text
plugins.load_builtin_plugins()
  -> @register(plugin_id)
  -> PluginRegistry.register_class()
  -> PluginSpec(command, plugin_cls, config_type)

UI form / API payload
  -> CommandEnvelope(command, payload)
  -> TaskManager.validate(config_type)
  -> DI resolve Depends(role=...)
  -> acquire LeaseSet
  -> instantiate Plugin
  -> await entrypoint(config, ctx, capabilities)
```

插件入口通常是：

```python
@register(plugin_id="org.lab.example")
class ExamplePlugin(Plugin):
    config_type = ExampleConfig

    @on_command("start_example")
    async def run(self, config, ctx,
                  slm: PatternModulator = Depends(role="primary_slm")):
        await ctx.checkpoint("step", index=0)
        await slm.display_pattern(frame, context=ctx)
        pointer = await ctx.writer.append_array("traces/example", values)
        ctx.emit_progress(step=1, total=1, pointer=pointer)
```

## 插件可以做什么

- 声明强类型 `config_type`。
- 通过 `Depends(role=...)` 使用 capability Protocol。
- 通过 `ctx.checkpoint()` 实现暂停、取消和 lease heartbeat。
- 通过 `ctx.writer` 写原始数组和 manifest 元数据。
- 通过 `ctx.emit_progress()`、状态事件和结构化日志通知 UI。
- 通过 `core/sweep.py` 复用声明式扫描，而不是复制 checkpoint/写入样板。

## 插件不能做什么

- 不 import Driver、Controller 或 Qt。
- 不持有 asyncio.Lock、线程锁或设备 lease；资源由 TaskManager 管理。
- 不手写 `sleep` 等待硬件 settle；settled 语义属于 Controller。
- 不把完整数组作为 EventBus payload；只能写 RunWriter 并发布 pointer/preview。
- 不直接发 raw SCPI；调试命令必须成为显式、受权限控制的 capability。

## 实验 Workflow

```text
validate config
  -> resolve instruments by role
  -> acquire all required devices
  -> create run manifest
  -> for each step:
       checkpoint()
       apply/display configuration
       acquire typed measurement
       append data + emit small preview/progress
  -> finally:
       stop/safe_state
       flush writer
       release lease
       final RunStateEvent
```

## 新插件 Checklist

1. 先写 Config 和边界校验，再写 loop。
2. 明确需要的 capability/role 和输出 dataset schema。
3. 先用 sim 完成一条完整 run，再接真实设备。
4. 对 pause、cancel、异常和 cleanup 各写一个测试。
5. 如果有专用流程，创建独立 Panel；不要把一整个实验塞进通用设置页。

