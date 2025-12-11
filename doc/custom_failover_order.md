# LLM 故障转移插件配置增强文档

## 问题概述

在 v1.0.2 版本中，插件的故障转移顺序依赖框架的 `primary` 提供商设置，即始终将当前会话的首选提供商置于第一位，其他提供商按框架返回的顺序排列。这种设计存在以下局限性：

1. **无法完全自定义顺序**：用户无法按照自己的需求自由排列所有提供商的优先级。
2. **依赖框架配置**：故障转移顺序受框架的 `primary` 设置约束，无法独立于框架配置。
3. **缺少细粒度控制**：无法针对特定场景（如免费模型限流频繁）灵活调整故障转移策略。

本次改进的目标是：**通过插件配置实现完全自定义的故障转移顺序，不再依赖框架的 `primary` 设置，用户可以完全重排所有提供商的优先级。**

---

## 备选方案与取舍

### 方案一：仅支持调整非 primary 提供商的顺序
**描述**：保留 `primary` 始终在第一位，仅允许用户调整其他提供商的顺序。

**优点**：
- 实现简单，改动小。
- 兼容现有逻辑，降低风险。

**缺点**：
- 无法满足需求：用户仍然无法完全自定义顺序。
- 若首选提供商频繁限流，用户依然无法将备用提供商提前。

**结论**：不采用，因为无法满足"完全自定义"的核心需求。

---

### 方案二：完全基于配置重排顺序（选用方案）
**描述**：新增 `provider_order` 配置项（列表类型），用户填写提供商 ID 的排列顺序。插件按照该顺序构建故障转移列表，不再依赖框架的 `primary` 设置。

**优点**：
- 满足核心需求：用户可以完全自定义故障转移顺序。
- 配置灵活：支持留空（使用默认逻辑）或完全自定义。
- 向后兼容：未配置时保留原有行为，已有用户无感知。

**缺点**：
- 配置复杂度略高：用户需要手动填写提供商 ID。
- 需要在 WebUI 查看提供商 ID，增加一定操作成本。

**结论**：采用此方案。通过 Schema 的 `hint` 字段提供详细说明，引导用户配置。

---

### 方案三：动态权重策略
**描述**：为每个提供商设置权重或优先级分数，插件根据权重动态排序。

**优点**：
- 更灵活，可支持复杂的优先级计算。

**缺点**：
- 实现复杂度高，增加维护成本。
- 用户配置难度大，不直观。
- 与需求不符：需求是"完全自定义顺序"，而非动态计算。

**结论**：不采用，过度设计。

---

## 实施步骤与关键改动

### 1. 创建插件配置 Schema

在插件根目录新建 `_conf_schema.json`，定义四个配置项：

```json
{
  "enabled": {
    "description": "是否启用故障转移功能",
    "type": "bool",
    "hint": "启用后将为所有聊天类模型提供商添加故障转移能力",
    "default": true
  },
  "provider_order": {
    "description": "故障转移顺序（提供商ID列表）",
    "type": "list",
    "hint": "按优先级从高到低排列提供商ID。留空时使用框架默认顺序。支持完全自定义首选提供商及备选顺序，不依赖框架的 primary 设置。",
    "default": []
  },
  "log_enabled": {
    "description": "是否启用详细日志",
    "type": "bool",
    "hint": "启用后将记录每次故障转移的详细过程到 llm_failover.log 文件",
    "default": true
  },
  "retry_delay": {
    "description": "故障重试延迟（秒）",
    "type": "float",
    "hint": "在切换到下一个提供商前的等待时间，默认为0秒（立即切换）",
    "default": 0.0
  }
}
```

**配置说明**：
- `enabled`：全局开关，可快速禁用故障转移功能。
- `provider_order`：核心配置，支持完全自定义顺序。留空时使用默认逻辑（`primary` 优先）。
- `log_enabled`：控制本地日志文件的写入，减少磁盘 I/O（AstrBot 全局日志始终记录）。
- `retry_delay`：在切换提供商前等待指定时间，避免瞬时故障导致过快切换。

---

### 2. 修改插件主类以加载配置

修改 `main.py:22-28`，在 `__init__` 方法中接收 `config` 参数：

```python
class LLMFailoverPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # 加载插件配置，支持用户通过 WebUI 自定义故障转移行为
        self.config = config
        self._log_path = Path(__file__).resolve().parent / "llm_failover.log"
        self._log("LLM Failover 插件初始化，准备安装故障转移包装。")
        self._install_provider_failover()
```

**关键点**：
- 新增 `config: AstrBotConfig` 参数，符合 AstrBot 插件配置规范（见 `doc/plugin.md:910-926`）。
- 导入 `AstrBotConfig` 类型：`from astrbot.api import AstrBotConfig`。

---

### 3. 重构 `_iter_fallback_providers` 方法

修改 `main.py:137-192`，实现完全自定义的顺序逻辑：

```python
def _iter_fallback_providers(self, primary):
    """
    构建故障转移的提供商顺序列表。

    优先级规则：
    1. 如果用户配置了 provider_order，完全按照该顺序排列，不再将 primary 置于首位
    2. 如果未配置 provider_order，则按原有逻辑：primary 优先，其他提供商按框架顺序排列

    Args:
        primary: 当前会话正在使用的提供商

    Returns:
        提供商顺序列表
    """
    providers = [
        provider
        for provider in self.context.get_all_providers()
        if provider.meta().provider_type == ProviderType.CHAT_COMPLETION
    ]

    # 获取用户配置的提供商顺序
    configured_order = self.config.get("provider_order", [])

    if configured_order:
        # 用户配置了自定义顺序，完全按照该顺序排列，不依赖框架的 primary
        order = []
        provider_map = {
            provider.provider_config.get("id", ""): provider
            for provider in providers
        }

        # 按照配置顺序添加提供商
        for provider_id in configured_order:
            if provider_id in provider_map:
                order.append(provider_map[provider_id])

        # 将未在配置中的提供商追加到末尾
        for provider in providers:
            if provider not in order:
                order.append(provider)

        self._log_failover(
            f"使用配置的提供商顺序: {', '.join(p.provider_config.get('id', 'unknown') for p in order)}"
        )
    else:
        # 未配置自定义顺序，使用默认逻辑：primary 优先
        order = []
        if primary:
            order.append(primary)
        for provider in providers:
            if provider is primary:
                continue
            if provider not in order:
                order.append(provider)

    return order if order else [primary] if primary else []
```

**关键改动**：
1. **配置优先**：若 `provider_order` 非空，完全按照该顺序排列，忽略 `primary`。
2. **向后兼容**：若 `provider_order` 为空，保留原有逻辑（`primary` 优先）。
3. **容错处理**：未在配置中的提供商会被追加到末尾，避免遗漏。
4. **日志增强**：记录最终使用的顺序，方便调试。

---

### 4. 其他配置项集成

#### 4.1 控制日志写入

修改 `main.py:36-47`，仅在 `log_enabled` 为 `True` 时写入本地日志：

```python
def _log(self, message: str):
    """写入本地日志文件，记录插件运行时信息"""
    # 仅在配置允许时写入本地日志文件
    if not self.config.get("log_enabled", True):
        return
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass
```

---

#### 4.2 启用全局开关

修改 `main.py:53-58`，检查 `enabled` 配置：

```python
def _install_provider_failover(self):
    """为所有聊天类提供商安装故障转移包装器"""
    # 检查是否启用故障转移功能
    if not self.config.get("enabled", True):
        self._log_failover("故障转移功能已禁用，跳过安装。")
        return
    # ... 后续逻辑
```

---

#### 4.3 支持重试延迟

修改 `main.py:243-307` 和 `main.py:309-410`，在切换提供商前等待：

```python
# 在 _execute_with_failover 和 _execute_stream_with_failover 中添加：
retry_delay = self.config.get("retry_delay", 0.0)

# 在异常处理逻辑中，切换前等待：
if self._should_failover(exc) and index < len(order) - 1:
    errors.append((provider_id, exc))
    self._log_failover(f"提供商 {provider_id} 异常: {exc}，尝试下一个提供商。")
    if retry_delay > 0:
        await asyncio.sleep(retry_delay)
    continue
```

**作用**：避免因瞬时网络抖动导致过快切换，给服务端一定恢复时间。

---

### 5. 添加详细注释

为所有关键方法添加中文注释，说明功能、参数、返回值及异常处理逻辑，提升代码可读性和可维护性。主要涉及：
- `_install_provider_failover`：安装包装器的逻辑
- `_iter_fallback_providers`：构建顺序列表的逻辑
- `_should_failover`：异常判断逻辑
- `_execute_with_failover` / `_execute_stream_with_failover`：故障转移执行逻辑
- `_extract_response_text`：响应文本提取逻辑

---

### 6. 更新版本号与元数据

修改 `metadata.yaml`，更新版本号和描述：

```yaml
name: astrbot_plugin_llm_failover
desc: AstrBot 文本类大模型故障转移插件，遇到限速/瞬时故障时自动切换备用模型。支持通过插件配置完全自定义故障转移顺序，不依赖框架的 primary 设置。
version: v1.1.0
author: 万一
repo: https://github.com/Ryan7t/astrbot_plugin_llm_failover
```

---

## 优缺点与结论

### 优点

1. **完全自定义**：用户可以通过 `provider_order` 完全控制故障转移顺序，不再受框架 `primary` 设置约束。
2. **向后兼容**：未配置 `provider_order` 时，保留原有逻辑，已有用户无感知升级。
3. **配置灵活**：支持全局开关、日志控制、重试延迟等细粒度配置，适应不同场景需求。
4. **易于使用**：通过 AstrBot WebUI 的可视化配置界面，用户无需修改代码即可调整行为。
5. **日志增强**：记录最终使用的顺序，方便排查故障转移是否按预期执行。

---

### 缺点

1. **配置复杂度**：用户需要手动填写提供商 ID，对于不熟悉 AstrBot 配置的用户有一定门槛。
   - **缓解措施**：在 Schema 的 `hint` 中提供详细说明，引导用户在 WebUI 查看提供商 ID。

2. **配置错误风险**：若用户填写了无效的提供商 ID，可能导致该提供商被跳过。
   - **缓解措施**：插件会将未在配置中的提供商追加到末尾，确保不会遗漏。

3. **日志冗余**：若 `log_enabled` 默认开启，长期运行可能积累较多日志文件。
   - **缓解措施**：用户可按需关闭本地日志，AstrBot 全局日志始终记录关键信息。

---

### 结论

本次改进成功实现了通过插件配置完全自定义故障转移顺序的需求，不再依赖框架的 `primary` 设置。同时，通过新增的配置项（`enabled`、`log_enabled`、`retry_delay`），用户可以灵活控制插件行为，适应不同场景的需求。

实施过程中严格遵循 AstrBot 插件开发规范，确保兼容性和可维护性。代码中添加了详细的中文注释，便于后续维护和扩展。

---

## 配置示例

### 场景一：完全自定义顺序

假设用户有三个提供商：
- `siliconflow-free`（硅基流动免费模型，限流频繁）
- `deepseek-v3`（DeepSeek V3，稳定但付费）
- `openai-gpt4`（OpenAI GPT-4，最稳定但昂贵）

用户希望优先使用 `deepseek-v3`，其次 `openai-gpt4`，最后才是 `siliconflow-free`。

**配置**：
```json
{
  "enabled": true,
  "provider_order": ["deepseek-v3", "openai-gpt4", "siliconflow-free"],
  "log_enabled": true,
  "retry_delay": 1.0
}
```

**效果**：无论框架的 `primary` 设置为哪个提供商，插件都会按照 `deepseek-v3 -> openai-gpt4 -> siliconflow-free` 的顺序尝试。

---

### 场景二：禁用故障转移

用户仅使用单一提供商，不需要故障转移。

**配置**：
```json
{
  "enabled": false
}
```

**效果**：插件不会安装任何包装器，直接使用框架的原始调用逻辑。

---

### 场景三：使用默认逻辑

用户不配置 `provider_order`，保留原有行为。

**配置**：
```json
{
  "enabled": true,
  "provider_order": [],
  "log_enabled": false,
  "retry_delay": 0.0
}
```

**效果**：插件按照原有逻辑，将 `primary` 提供商置于第一位，其他提供商按框架顺序排列。同时关闭本地日志。

---

## 参考文档

- AstrBot 插件开发文档：`doc/plugin.md`
- 插件配置规范：`doc/plugin.md:832-931`
- 故障转移概述：`doc/llm_failover_overview.md`
- 流式故障转移详解：`doc/llm_failover_stream_fallback.md`
