# LLM 故障转移插件 - 自定义顺序配置实施文档

## 问题概述

原实现中的 `_iter_fallback_providers` 方法总是将**当前激活的 primary Provider** 放在首位，其余 Provider 的顺序不确定（取决于 `get_all_providers()` 的返回顺序）。这导致用户无法通过配置完全控制故障转移顺序，尤其是当用户希望首选一个非 primary 的 Provider 时，框架仍会优先尝试 primary。

需求明确：**插件配置必须能够完全重排所有聊天类 Provider 的调用顺序**，而不是依赖框架的 primary 放在首位。

## 备选方案与取舍

### 方案一：仅允许配置备用 Provider 列表（不含 primary）

- **优点**：改动最小，保留 primary 优先的默认行为。
- **缺点**：无法满足"完全重排"的需求，用户依然无法让非 primary Provider 成为首选。

### 方案二：配置完整的 Provider 顺序列表（**已采用**）

- **优点**：
  - 用户可自由决定任意 Provider 作为首选，完全掌控故障转移顺序。
  - 配置为空时，插件仍保留原行为（primary 优先），向后兼容。
- **缺点**：
  - 配置项需要用户填写所有 Provider ID，略显冗长。
  - 若用户新增或删除 Provider 后忘记更新配置，需要插件代码进行兜底（自动追加未在配置中的 Provider）。

**取舍结论**：方案二能够彻底解决"完全重排"需求，且通过代码兜底机制避免配置过时导致的故障，因此采用方案二。

## 实施步骤/关键改动

### 1. 创建插件配置 Schema（`_conf_schema.json`）

按照 AstrBot 插件开发文档（`doc/plugin.md:832-931`），在插件根目录创建 `_conf_schema.json`：

```json
{
  "provider_order": {
    "description": "故障转移顺序（自定义模型提供商的完整排序）",
    "type": "list",
    "hint": "指定所有聊天类 Provider 的尝试顺序，必须包含所有可用的模型提供商 ID。留空则使用当前激活的 Provider 作为首选，其余顺序不确定。",
    "default": []
  }
}
```

- **字段说明**：
  - `type: "list"`：用户在 WebUI 上将看到一个列表输入组件。
  - `default: []`：默认为空列表，保持向后兼容（未配置时行为不变）。
  - `hint`：提示用户填写完整的 Provider ID 列表。

### 2. 修改 `__init__` 方法以接收插件配置（`main.py:21-26`）

根据文档（`doc/plugin.md:912-926`），插件初始化时传入 `config: AstrBotConfig`：

```python
from astrbot.api import AstrBotConfig, logger as astr_logger

class LLMFailoverPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config  # 插件配置
        self._log_path = Path(__file__).resolve().parent / "llm_failover.log"
        self._log("LLM Failover 插件初始化，准备安装故障转移包装。")
        self._install_provider_failover()
```

- **关键点**：导入 `AstrBotConfig` 并在 `__init__` 中接收 `config` 参数，存储为实例属性。

### 3. 重构 `_iter_fallback_providers` 方法（`main.py:123-163`）

原方法始终将 `primary` 放在首位。修改后的逻辑：

```python
def _iter_fallback_providers(self, primary):
    """根据用户配置的 provider_order 生成故障转移顺序。

    Args:
        primary: 当前正在使用的 Provider 实例

    Returns:
        按配置顺序排列的 Provider 列表，若配置为空则将 primary 放在首位
    """
    # 获取所有聊天类 Provider
    providers = [
        provider
        for provider in self.context.get_all_providers()
        if provider.meta().provider_type == ProviderType.CHAT_COMPLETION
    ]
    # 构建 ID -> Provider 的映射
    provider_map = {
        provider.provider_config.get("id", ""): provider for provider in providers
    }
    # 读取用户配置的顺序
    configured_order = self.config.get("provider_order", [])

    order = []
    if configured_order:
        # 完全按照用户配置的顺序重排
        for provider_id in configured_order:
            if provider_id in provider_map:
                order.append(provider_map[provider_id])
        # 追加未在配置中的 Provider（以防配置过时）
        for provider in providers:
            if provider not in order:
                order.append(provider)
    else:
        # 配置为空时，保留原行为：primary 在首位，其余不确定
        if primary:
            order.append(primary)
        for provider in providers:
            if provider not in order:
                order.append(provider)

    return order if order else [primary]
```

- **核心逻辑**：
  1. 读取 `self.config.get("provider_order", [])`。
  2. 若非空，按配置顺序添加 Provider；若某 ID 不存在于当前 Provider 列表，则跳过。
  3. 将配置中未提及的 Provider 追加到末尾（兜底机制，防止配置遗漏）。
  4. 若配置为空，回退到原行为：primary 优先，其余顺序不确定。
- **注释要求**：添加详细的函数级和关键步骤的行内注释。

### 4. 更新 `_install_provider_failover` 方法日志（`main.py:47-117`）

在安装故障转移时，根据是否配置 `provider_order` 输出不同的日志：

```python
# 读取用户配置的顺序，记录日志
configured_order = self.config.get("provider_order", [])
if configured_order:
    order_desc = ", ".join(configured_order)
    self._log_failover(f"已启用故障转移，用户配置顺序: {order_desc}")
else:
    order_desc = ", ".join(
        provider.provider_config.get("id", "unknown") for provider in providers
    )
    self._log_failover(
        f"已启用故障转移，未配置 provider_order，当前提供商顺序: {order_desc}"
    )
```

- **目的**：让用户在日志中清晰看到插件是否读取到配置，以及最终生效的顺序。

### 5. 添加代码注释（覆盖所有修改）

- `_install_provider_failover` 方法：添加函数级注释和关键步骤行内注释（如"缓存原始 text_chat 方法"、"仅对尚未包装的 Provider 进行普通聊天包装"等）。
- `_iter_fallback_providers` 方法：函数级注释、Args/Returns 说明、每个逻辑分支的行内注释。
- 配置读取处：注释说明"读取用户配置的顺序"。

## 优缺点与结论

### 优点

1. **完全重排能力**：用户可以在 WebUI 上配置 `provider_order` 列表，插件严格按照该顺序调用 Provider，不再强制 primary 优先。
2. **向后兼容**：配置为空时，插件行为与原实现一致（primary 优先），不影响现有部署。
3. **兜底机制**：若配置中遗漏了某些 Provider，插件会自动将其追加到末尾，避免遗漏可用资源。
4. **可观测性**：日志明确记录配置读取结果和最终顺序，便于用户排障。

### 缺点

1. **配置冗长**：用户需要填写完整的 Provider ID 列表，若 Provider 数量较多，配置较繁琐。
   - **缓解措施**：可在文档或 WebUI 提示中说明如何获取 Provider ID，或未来扩展为 WebUI 下拉选择组件。
2. **配置过时风险**：用户新增或删除 Provider 后，若未同步更新配置，可能导致顺序不符合预期。
   - **缓解措施**：插件代码已自动追加未在配置中的 Provider，且日志会提示当前实际顺序，用户可据此调整。

### 结论

本实施完全满足"插件配置必须能够完全重排所有聊天类 Provider 的调用顺序"的需求，且通过合理的兜底机制和日志输出，在可维护性和用户体验之间取得了平衡。建议在插件文档或 WebUI 配置页面增加说明，指导用户如何获取 Provider ID 并填写配置。

## 测试建议

1. **场景一**：配置 `provider_order` 为 `["provider_b", "provider_a"]`，确保故障转移时首先尝试 `provider_b`，而非当前激活的 `provider_a`。
2. **场景二**：配置 `provider_order` 为空，确保插件行为与原实现一致（primary 优先）。
3. **场景三**：配置 `provider_order` 中遗漏某个 Provider，确认日志中该 Provider 被自动追加到末尾。
4. **场景四**：配置 `provider_order` 包含不存在的 Provider ID，确认插件跳过该 ID 且不报错。

## 参考资料

- AstrBot 插件配置文档：`doc/plugin.md:832-931`
- 插件实现：`main.py`
- 故障转移概述：`doc/llm_failover_overview.md`
- 详细报告：`doc/llm_failover_report.md`
