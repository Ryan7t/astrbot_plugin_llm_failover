# LLM Failover

AstrBot 插件，为聊天类模型提供商注入自动故障转移包装，初衷是防止”硅基流动”等免费模型服务商触发限速时影响用户体验，因而在限速或瞬时故障出现时可以无缝切换到备用 LLM。

- 初始化阶段扫描所有 `CHAT_COMPLETION` Provider，将 `text_chat` 统一包裹为带降级逻辑的调用链。
- AstrBot 完全启动后再次扫描，确保热加载或延迟注册的 Provider 也具备同样能力。
- 日志同时写入 AstrBot 核心日志与插件本地 `llm_failover.log`，方便定位故障与回退路径。

如需了解实现细节与演进建议，请参阅 `doc/llm_failover_overview.md` 与 `doc/llm_failover_report.md`。
