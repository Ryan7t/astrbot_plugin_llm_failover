# LLM 故障转移流式修复记录

## 问题概述
- 线上配置了 Gemini 与 OpenAI 双提供商，预期在 Gemini 抛出 500/限流等错误时自动切换到 OpenAI，当前仅同步 `text_chat` 路径被包装，流式 `text_chat_stream` 抛错时未进入故障转移，导致请求直接失败。

## 备选方案与取舍
- 方案 A：在业务层（如 runner）重试并手动切 provider，侵入面广、耦合上层逻辑，放弃。
- 方案 B：在插件层补齐流式包装，沿用现有故障转移判断，保持对业务代码的透明性，最终选用。
- 方案 C：统一降级为非流式 `text_chat` 调用再包装，无法满足实时输出需求，放弃。

## 实施步骤/关键改动
- 为 Provider 增加流式包装：缓存 `_llm_failover_original_text_chat_stream`，绑定新的 `text_chat_stream` 包装器，复用现有故障判定并避免重复包裹。
- 新增 `_execute_stream_with_failover`：在未产出内容时捕获异常并按顺序切换候选 Provider，已有输出时为避免混流直接抛出；成功后沿用日志预览能力。
- 提取 `_get_prompt_preview` 复用提示截断逻辑，并扩展 `_extract_response_text` 以兼容流式首片、字符串等返回类型，保证日志不报属性错误。

## 优缺点与结论
- 优点：覆盖流式场景的故障转移，Gemini 出现 500/限流后可自动切换到 OpenAI，Gemini 恢复后仍作为首选；日志依旧保留响应预览方便排查。
- 缺点：流式调用一旦输出部分内容即停止继续切换，以避免混合多模型输出；仍需确保备用 Provider 支持聊天接口。
- 结论：通过插件层扩展流式包装，满足“Gemini 异常自动回落、恢复后再用 Gemini”的诉求，无需修改上层业务代码。
