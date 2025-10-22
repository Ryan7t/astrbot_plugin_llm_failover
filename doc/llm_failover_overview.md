# LLM 故障转移插件说明

本插件（`astrbot_plugin_llm_failover`）在 AstrBot 启动后为所有 `ProviderType.CHAT_COMPLETION` 类型的 Provider 注入统一的故障转移包装，确保主力大模型发生限流、认证失效或网络异常时能自动切换到备用 Provider，而无需改动任何上层指令代码。

## 背景

最初的故障转移逻辑内嵌在 `wanbot1` 插件中，随着维护成本提升，现已拆分到独立插件中，便于复用与模块化演进。除 `wanbot1` 之外的其他插件同样受益，只要它们最终调用 `Provider.text_chat(...)` 或 `event.request_llm(...)`。

## 关键组件

- `LLMFailoverPlugin.__init__`（`main.py:19-24`）
  - 记录日志并立即调用 `_install_provider_failover()`。
- `LLMFailoverPlugin.on_bot_loaded`（`main.py:26-30`）
  - 监听 `filter.on_astrbot_loaded` 钩子，在 AstrBot 完全就绪后再次尝试安装，覆盖热加载或延迟注册的 Provider。
- `_install_provider_failover`（`main.py:45-75`）
  - 收集可用 Provider，缓存原始的 `text_chat`，并通过 `types.MethodType` 替换为 `_execute_with_failover` 包装器。
- `_execute_with_failover`（`main.py:137-179`）
  - 构建主备顺序，逐个调用原始 `text_chat`。若命中 `_should_failover` 定义的错误，即记录异常并尝试下一个 Provider。
- `_should_failover`（`main.py:109-136`）
  - 根据 HTTP 状态码与常见错误关键词判断是否继续切换。
- `_log_failover` / `_log_failover_result`（`main.py:41-92`）
  - 将事件写入 AstrBot 全局日志与本地 `llm_failover.log`，方便集中排错。

## 工作流程

1. **安装阶段**：插件初始化或 AstrBot 完成启动时执行 `_install_provider_failover()`：
   - 若聊天类 Provider 少于两个，仅记录“数量不足”并退出；
   - 否则为每个 Provider 缓存 `_llm_failover_original_text_chat` 并绑定包装器；
   - 防止重复安装，已包装过的 Provider 会带 `_llm_failover_installed` 标记。
2. **请求阶段**：业务代码调用 `provider.text_chat(...)` / `event.request_llm(...)` 时自动进入 `_execute_with_failover()`；
3. **故障判定**：若返回异常且满足 `_should_failover()` 条件，会顺序尝试下一个候选 Provider；
4. **终止条件**：任意 Provider 成功返回即结束；若全部失败，则抛出最后一次异常并记录汇总日志；若无可用 Provider 则抛出 `RuntimeError("没有可用的模型服务提供商")`。

## 日志与排障

- 全局日志：所有事件会写入 AstrBot 内置的 `logger`，前缀为 `[llm_failover][LLM]`；
- 本地落盘：`llm_failover.log` 会追加 `[LLM切换]` 前缀日志，包含调用 Provider 队列、异常摘要与响应预览（截断至 120 字符）。

调试建议：

1. 至少配置两个聊天类 Provider；
2. 临时让主 Provider 触发 401/429 等限流错误，观察日志是否记录“尝试下一个提供商”；
3. 如需定位 Prompt，可查看日志中的 `prompt` 预览字段。

## 与其他插件的协同

- `wanbot1` 中的 `/今日运势`、`/答案之书`、`/一句话故事` 等指令依旧透明享受故障转移能力（参见 `astrbot_plugin_wanbot1/Commands/fortune.py:1-73`、`Commands/answer.py:1-60`、`Commands/story.py:1-88`）。
- 任何新插件只要复用 AstrBot Provider API，即可自动接入故障转移，无需额外编码。

## 下一步扩展方向

- 完善 `_should_failover` 的异常分类，覆盖更多第三方 SDK 自定义错误；
- 为流式输出、图像生成等其他 Provider 类型提供类似包装；
- 若未来引入退避或统计信息，可在 `_execute_with_failover` 中扩展协程安全的状态管理。

## 参考资料

- 插件实现：`main.py`
- 故障转移详解：`doc/llm_failover_report.md`
- AstrBot Provider/会话文档：`astrbot_plugin_wanbot1/doc/plugin.md`
