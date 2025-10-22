# LLM Failover

AstrBot 插件，为聊天类模型提供商自动注入故障转移包装。

- 在插件初始化时扫描所有 `CHAT_COMPLETION` Provider，将 `text_chat` 统一替换为带降级逻辑的包装器。
- 支持在 AstrBot 启动完成后重复扫描，确保热加载的新 Provider 也具备相同行为。
- 所有日志同时写入 AstrBot 核心日志和插件本地 `llm_failover.log` 文件，以便排查。

如需了解实现细节与演进建议，请参阅 `doc/llm_failover_overview.md` 与 `doc/llm_failover_report.md`。
