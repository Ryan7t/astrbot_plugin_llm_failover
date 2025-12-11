# LLM 故障转移自定义顺序实现文档

## 问题概述

原有的 `astrbot_plugin_llm_failover` 插件在多提供商故障转移时,使用框架默认顺序:当前调用的提供商(primary)优先,其余提供商按 `context.get_all_providers()` 返回的顺序依次尝试。这导致用户无法灵活控制故障转移的优先级,例如希望优先使用成本更低或响应更快的提供商。

### 核心需求

- **自定义故障转移顺序**:允许用户通过插件配置完全重排提供商优先级,而不是依赖框架的 primary 放在首位。
- **配置驱动**:通过 AstrBot 插件配置系统(`_conf_schema.json`)提供可视化配置界面。
- **向下兼容**:未配置自定义顺序时,保持原有框架默认行为。

## 备选方案与取舍

### 方案一:修改 `_iter_fallback_providers` 方法,读取配置文件自定义顺序

**优点**:
- 插件级配置,用户可在 AstrBot WebUI 中直接修改。
- 完全重排提供商顺序,不依赖 primary 优先的框架默认行为。
- 易于扩展(如添加黑名单、权重等)。

**缺点**:
- 需要修改插件代码,添加配置文件解析逻辑。

**选择理由**:符合 AstrBot 插件开发规范,提供最佳用户体验和灵活性。✅ **采用此方案**

### 方案二:通过环境变量或外部配置文件指定顺序

**优点**:
- 不修改插件代码,通过外部配置控制。

**缺点**:
- 需要手动维护配置文件,无法使用 AstrBot WebUI 可视化配置。
- 不符合 AstrBot 插件配置规范。

**放弃理由**:用户体验差,不符合框架设计理念。❌

### 方案三:在框架层面修改 `context.get_all_providers()` 返回顺序

**优点**:
- 全局生效,所有插件自动继承自定义顺序。

**缺点**:
- 需要修改 AstrBot 框架核心代码,侵入性强。
- 不同插件可能需要不同的提供商顺序,全局配置灵活性不足。

**放弃理由**:超出插件职责范围,不应修改框架代码。❌

## 实施步骤与关键改动

### 步骤 1:创建配置文件 Schema

**文件路径**:`/workspace/_conf_schema.json`

**内容**:
```json
{
  "provider_order": {
    "description": "故障转移提供商顺序",
    "type": "list",
    "hint": "按优先级从高到低排列提供商ID。留空则使用框架默认顺序。例如:[\"deepseek\", \"openai\", \"claude\"]",
    "default": []
  }
}
```

**设计说明**:
- `type: "list"`:配置值为字符串列表,每个元素为提供商 ID。
- `default: []`:默认为空列表,保持原有框架默认行为。
- `hint`:提供示例配置,引导用户正确填写提供商 ID。

**参考文档**:根据 `doc/plugin.md:832-927` 中插件配置规范,AstrBot 会自动解析该 Schema 并在 WebUI 提供可视化配置界面。

### 步骤 2:修改插件构造函数,接收配置对象

**文件路径**:`/workspace/main.py:20-34`

**修改内容**:
```python
# 修改前
class LLMFailoverPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._log_path = Path(__file__).resolve().parent / "llm_failover.log"
        self._log("LLM Failover 插件初始化,准备安装故障转移包装。")
        self._install_provider_failover()

# 修改后
class LLMFailoverPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        """初始化故障转移插件。

        Args:
            context: AstrBot 上下文对象,提供框架核心组件访问能力。
            config: 插件配置对象,包含用户自定义的故障转移提供商顺序。
        """
        super().__init__(context)
        # 插件配置,包含用户定义的 provider_order 列表
        self.config = config
        # 日志文件路径
        self._log_path = Path(__file__).resolve().parent / "llm_failover.log"
        self._log("LLM Failover 插件初始化,准备安装故障转移包装。")
        self._install_provider_failover()
```

**关键点**:
- 新增 `config: AstrBotConfig` 参数,接收插件配置。
- 导入 `AstrBotConfig` 类型:`from astrbot.api import AstrBotConfig`。
- 版本号升级至 `1.0.2`。

**参考文档**:`doc/plugin.md:909-926` 说明插件配置会在实例化时传入 `__init__()`。

### 步骤 3:重构 `_iter_fallback_providers` 方法,支持自定义顺序

**文件路径**:`/workspace/main.py:131-177`

**修改内容**:
```python
# 修改前(仅框架默认顺序)
def _iter_fallback_providers(self, primary):
    providers = [
        provider
        for provider in self.context.get_all_providers()
        if provider.meta().provider_type == ProviderType.CHAT_COMPLETION
    ]
    order = []
    if primary:
        order.append(primary)
    for provider in providers:
        if provider is primary:
            continue
        if provider not in order:
            order.append(provider)
    return order if order else [primary]

# 修改后(支持自定义顺序)
def _iter_fallback_providers(self, primary):
    """根据配置构建故障转移提供商顺序。

    Args:
        primary: 当前主调用的提供商实例。

    Returns:
        按优先级排序的提供商列表。如果配置了 provider_order,
        则严格按配置顺序排列;否则采用框架默认顺序(primary 优先)。
    """
    # 获取所有聊天类提供商
    providers = [
        provider
        for provider in self.context.get_all_providers()
        if provider.meta().provider_type == ProviderType.CHAT_COMPLETION
    ]

    # 读取用户配置的提供商顺序
    custom_order = self.config.get("provider_order", []) if self.config else []

    if custom_order:
        # 使用自定义顺序:按配置中的 ID 顺序重新排列提供商
        provider_map = {
            provider.provider_config.get("id", ""): provider
            for provider in providers
        }
        ordered = []
        for pid in custom_order:
            if pid in provider_map:
                ordered.append(provider_map[pid])
        # 将配置中未列出的提供商追加到末尾
        for provider in providers:
            if provider not in ordered:
                ordered.append(provider)
        self._log_failover(f"使用自定义顺序: {custom_order}")
        return ordered if ordered else [primary]
    else:
        # 框架默认顺序:primary 优先,其余提供商按框架返回顺序排列
        order = []
        if primary:
            order.append(primary)
        for provider in providers:
            if provider is primary:
                continue
            if provider not in order:
                order.append(provider)
        return order if order else [primary]
```

**关键逻辑**:
1. **读取配置**:`custom_order = self.config.get("provider_order", [])` 获取用户配置的提供商 ID 列表。
2. **构建提供商映射**:`provider_map` 将提供商 ID 映射到提供商实例,便于按配置顺序重排。
3. **重排逻辑**:
   - 按 `custom_order` 中的 ID 顺序,从 `provider_map` 中查找对应提供商并添加到 `ordered` 列表。
   - 将配置中未列出的提供商追加到末尾(兼容新增提供商场景)。
4. **日志记录**:配置生效时记录 `[LLM切换] 使用自定义顺序: [...]`,便于调试。
5. **向下兼容**:若 `custom_order` 为空,保持原有框架默认行为(primary 优先)。

### 步骤 4:完善代码注释

为所有关键方法添加详细的中文注释,包括:

- `__init__`:说明 `config` 参数的作用。
- `_install_provider_failover`:说明包装器安装逻辑。
- `_iter_fallback_providers`:说明自定义顺序与默认顺序的切换逻辑。
- `_should_failover`:说明异常判断规则(HTTP 状态码、关键词匹配)。
- `_execute_with_failover`:说明普通聊天的故障转移流程。
- `_execute_stream_with_failover`:说明流式聊天的特殊处理(已有输出则不切换)。

**参考规范**:`doc/plugin.md:296-302` 中的开发原则要求"包含良好的注释"。

### 步骤 5:语法检查

运行 `python3 -m py_compile main.py` 验证语法正确性,确保无编译错误。

## 优缺点与结论

### 优点

1. **用户友好**:通过 AstrBot WebUI 可视化配置,无需手动编辑代码或配置文件。
2. **灵活性强**:用户可根据成本、响应速度、稳定性等因素自定义提供商优先级。
3. **向下兼容**:未配置时保持原有框架默认行为,无破坏性变更。
4. **易于扩展**:配置系统可轻松扩展为支持提供商黑名单、权重、按时段切换等高级功能。
5. **日志透明**:配置生效时记录日志,便于调试和审计。

### 缺点

1. **配置复杂性**:用户需要了解提供商 ID(可通过日志查看),错误的配置可能导致故障转移失效。
2. **无配置验证**:当前未对 `provider_order` 中的 ID 合法性进行校验,填写不存在的 ID 不会报错(会被忽略)。

### 改进建议

1. **配置验证**:在 `_iter_fallback_providers` 中检测无效 ID,记录警告日志。
2. **提供商选择器**:参考 `doc/plugin.md:904` 中的 `_special: select_provider` 特性,在 WebUI 提供下拉选择提供商 ID 的界面(需 AstrBot >= v4.0.0)。
3. **配置示例**:在插件文档中提供完整的配置示例,降低用户配置门槛。

### 结论

本次修改通过插件配置系统实现了**自定义故障转移顺序**的核心需求,完全符合 AstrBot 插件开发规范。用户可通过 WebUI 配置 `provider_order` 列表,灵活控制故障转移优先级,同时保持向下兼容性。代码注释完善,便于后续维护和功能扩展。

## 测试建议

1. **基础功能**:配置 `provider_order: ["openai", "deepseek"]`,触发 OpenAI 限流错误,观察日志是否记录"使用自定义顺序"并切换到 DeepSeek。
2. **向下兼容**:清空 `provider_order`,验证是否恢复框架默认行为(primary 优先)。
3. **无效配置**:配置 `provider_order: ["non_existent_id"]`,验证是否自动回退到所有可用提供商。
4. **流式场景**:测试流式聊天时的故障转移行为,确保已有输出后不切换提供商。

## 参考资料

- AstrBot 插件开发文档:`doc/plugin.md`
- 插件配置规范:`doc/plugin.md:832-927`
- 插件配置使用示例:`doc/plugin.md:909-926`
- 故障转移详解:`doc/llm_failover_report.md`
- 平台适配开发:`doc/plugin-platform-adapter.md`
