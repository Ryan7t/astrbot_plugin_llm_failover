# LLM Failover Mechanism Report

## 1. Purpose and Scope

This report summarises how the standalone `astrbot_plugin_llm_failover` plugin injects automatic failover across chat-completion providers while preserving AstrBot conversation state, persona bindings, and downstream integrations. It consolidates observations from the implementation in `main.py` together with references from the AstrBot documentation (`astrbot_plugin_wanbot1/doc/plugin.md`, `astrbot_plugin_wanbot1/doc/plugin-platform-adapter.md`).

## 2. High-Level Architecture

- **Hook point** – `LLMFailoverPlugin.__init__` executes during plugin construction and immediately installs the failover wrapper (`main.py:19-24`).
- **Boot refresh** – `LLMFailoverPlugin.on_bot_loaded` re-runs the installer once AstrBot signals that all providers are loaded, ensuring late-bound providers receive the wrapper (`main.py:26-30`).
- **Instrumentation** – `_install_provider_failover` walks the output of `context.get_all_providers()`, caches each provider's original `text_chat` under `_llm_failover_original_text_chat`, and rebinds the method via `types.MethodType` so future calls route through `_execute_with_failover` (`main.py:45-75`).
- **Non-invasive design** – The wrapper passes arguments through untouched, so existing integrations via `context.get_using_provider(...)`, `event.request_llm(...)`, `ConversationManager`, and `PersonaManager` remain unaffected (see sections 4 and 5 below).

## 3. Runtime Control Flow

1. **Initial binding** – `_install_provider_failover` captures all chat-completion providers. If fewer than two are available, it logs and exits without altering behaviour (`main.py:56-58`). The method also guards against double-wrapping by checking `_llm_failover_installed` on each provider (`main.py:60-71`).
2. **Wrapper execution** – Once wrapped, each provider's `text_chat` delegates into `_execute_with_failover(primary_provider, *args, **kwargs)` (`main.py:66-168`). `_iter_fallback_providers` builds an ordered list beginning with the active provider and then appending the remaining chat-completion providers (`main.py:93-107`).
3. **Invocation** – `_execute_with_failover` calls the cached `_llm_failover_original_text_chat`, logging a prompt preview plus all encountered failures (`main.py:146-171`). On success, it returns the original `LLMResponse`.
4. **Failover decision** – `_should_failover` accepts AstrBot's common transient errors (HTTP 401/402/403/408/409/429/500/502/503/504 and keywords such as “rate limit”, “timeout”) (`main.py:109-135`). If the error qualifies and additional candidates remain, control proceeds to the next provider; otherwise, the exception propagates to the caller.
5. **Observability** – Events are written both to AstrBot's global logger (prefix `[llm_failover][LLM]`) and to the plugin-local `llm_failover.log` file (`main.py:41-44`, `main.py:77-92`). Successful responses include a truncated preview via `_extract_response_text` (`main.py:183-191`).

If every provider fails, `_execute_with_failover` re-raises the last exception after logging a consolidated summary (`main.py:174-179`).

## 4. Preservation of Conversation Context

- **Argument passthrough** – `_execute_with_failover` forwards the inbound `*args` and `**kwargs` directly to the provider, guaranteeing that fields such as `session_id`, `conversation`, `system_prompt`, and tool payloads remain intact (`main.py:137-168`).
- **Framework contract** – AstrBot's `ConversationManager` represents each chat via the `Conversation` dataclass with fields like `cid`, `history`, and `persona_id` (`doc/plugin.md:1408-1464`). Because only the transport function is swapped, stored history and persona bindings remain untouched.
- **Call site example** – The `/一句话故事` command in `wanbot1` retrieves the active conversation and invokes `event.request_llm(...)` with `session_id` and `conversation` (`astrbot_plugin_wanbot1/Commands/story.py:57-82`). The invocation now routes through the failover plugin without modifying the conversation record.

## 5. Persona Consistency

- AstrBot's `PersonaManager` continues to manage persona CRUD and default selection (`doc/plugin.md:1573-1650`).
- Persona identifiers travel within the conversation object. The failover wrapper neither inspects nor mutates these fields, ensuring persona-aware behaviour remains consistent when providers switch.
- Example: `/今日运势` resolves the current provider via `context.get_using_provider(umo=event.unified_msg_origin)` and calls `provider.text_chat(...)` (`astrbot_plugin_wanbot1/Commands/fortune.py:48-67`). When failover occurs, subsequent providers reuse the same persona-aware settings.

## 6. Compatibility Matrix

- **Supported automatically** – Any code path invoking `provider.text_chat(...)` or `event.request_llm(...)`, including existing commands from `wanbot1` and future plugins adhering to AstrBot's provider API.
- **Excluded currently** – Streaming interfaces, embeddings, TTS/STT, image generation, or custom provider methods outside `text_chat`. Extending failover to those routines requires parallel wrappers.
- **Provider requirements** – At least two configured chat-completion providers. Otherwise the plugin logs “提供商数量不足…” and leaves the original implementation intact (`main.py:56-58`).

## 7. Observability and Troubleshooting

- Each attempt logs the provider identifier, prompt preview, and error summary, aiding diagnosis of rate limits or API faults (`main.py:146-171`).
- For verification, temporarily invalidate the primary provider's credentials; the `[LLM切换]` entries in `llm_failover.log` will confirm that fallback providers are engaged.
- All logs remain in UTF-8 for straightforward aggregation.

## 8. Operational Guidance

- The plugin automatically re-installs wrappers when AstrBot signals readiness. If providers are added dynamically at runtime, invoke `_install_provider_failover()` (e.g., via custom admin hook) so the new instances inherit failover behaviour.
- Guard against double wrapping by keeping the `_llm_failover_installed` flag intact when extending the code; companion plugins must avoid replacing `text_chat` without first restoring the cached `_llm_failover_original_text_chat`.

## 9. Known Limitations and Future Work

1. **Error taxonomy** – `_should_failover` relies on HTTP codes and string heuristics. Provider-specific exceptions may need to be added to prevent premature aborts (`main.py:109-135`).
2. **Non-chat providers** – Extending coverage beyond chat completion requires bespoke wrappers and potentially different response handling.
3. **Advanced retry policy** – The current implementation is coroutine-safe but stateless. Future enhancements such as exponential backoff or per-provider metrics should avoid shared mutable state or use asyncio-safe primitives.

## 10. Reference Summary

- Implementation: `astrbot_plugin_llm_failover/main.py`
- Command usage examples: `astrbot_plugin_wanbot1/Commands/fortune.py:1-73`, `astrbot_plugin_wanbot1/Commands/story.py:1-88`, `astrbot_plugin_wanbot1/Commands/answer.py:1-60`
- AstrBot documentation: `astrbot_plugin_wanbot1/doc/plugin.md:1129-1156`, `astrbot_plugin_wanbot1/doc/plugin.md:1408-1650`, `astrbot_plugin_wanbot1/doc/plugin-platform-adapter.md`

This report can be used by senior engineers to audit or further modularise the failover logic.
