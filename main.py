from __future__ import annotations

import types
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator

from astrbot.api import AstrBotConfig, logger as astr_logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import LLMResponse, ProviderType


@register(
    "llm_failover",
    "Wanbot Team",
    "为聊天类模型提供商添加自动故障转移能力",
    "1.0.0",
)
class LLMFailoverPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or AstrBotConfig()
        # 记录用户配置的故障转移顺序，配置为空时保持框架默认顺序。
        self._provider_order = self._normalize_provider_order(
            self.config.get("provider_order")
        )
        self._log_path = Path(__file__).resolve().parent / "llm_failover.log"
        self._log("LLM Failover 插件初始化，准备安装故障转移包装。")
        self._install_provider_failover()

    @filter.on_astrbot_loaded()
    async def on_bot_loaded(self):
        """AstrBot 启动完成后再次尝试安装故障转移，确保新加载的 Provider 被包装。"""
        self._log("AstrBot 已加载，刷新 Provider 故障转移包装。")
        self._install_provider_failover()

    def _log(self, message: str):
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{timestamp}] {message}\n")
        except Exception:
            pass

    def _log_failover(self, message: str):
        astr_logger.info(f"[llm_failover][LLM] {message}")
        self._log(f"[LLM切换] {message}")

    def _normalize_provider_order(self, raw_order: Any) -> list[str]:
        if not raw_order or not isinstance(raw_order, list):
            return []
        normalized = []
        for item in raw_order:
            value = str(item).strip()
            if not value or value in normalized:
                continue
            normalized.append(value)
        return normalized

    def _get_provider_id(self, provider) -> str:
        provider_config = getattr(provider, "provider_config", {}) or {}
        config_id = provider_config.get("id")
        if config_id:
            return str(config_id)
        meta_func = getattr(provider, "meta", None)
        if callable(meta_func):
            meta_obj = meta_func()
            meta_id = getattr(meta_obj, "id", None) or getattr(
                meta_obj, "provider_name", None
            ) or getattr(meta_obj, "name", None)
            if meta_id:
                return str(meta_id)
        return provider.__class__.__name__

    def _ordered_providers(self, providers: list, primary) -> list:
        if not providers:
            return []

        if self._provider_order:
            id_map = {self._get_provider_id(p): p for p in providers}
            ordered = []
            for pid in self._provider_order:
                provider = id_map.get(pid)
                if provider and provider not in ordered:
                    ordered.append(provider)

            # 未在配置中的 provider 仍然参与故障转移，但排在配置顺序之后。
            for provider in providers:
                if provider not in ordered:
                    ordered.append(provider)
            return ordered

        order = []
        if primary:
            order.append(primary)
        for provider in providers:
            if provider not in order:
                order.append(provider)
        return order

    def _install_provider_failover(self):
        try:
            providers = [
                provider
                for provider in self.context.get_all_providers()
                if provider.meta().provider_type == ProviderType.CHAT_COMPLETION
            ]
        except Exception as exc:
            self._log_failover(f"安装故障转移失败，无法获取提供商: {exc}")
            return

        if len(providers) <= 1:
            self._log_failover("提供商数量不足，不启用故障转移。")
            return

        available_ids = {self._get_provider_id(provider) for provider in providers}
        if self._provider_order:
            missing_ids = [
                pid for pid in self._provider_order if pid not in available_ids
            ]
            if missing_ids:
                self._log_failover(
                    f"配置的提供商未找到，将跳过: {', '.join(missing_ids)}"
                )

        for provider in providers:
            text_wrapped = getattr(provider, "_llm_failover_text_wrapped", False)
            stream_wrapped = getattr(provider, "_llm_failover_stream_wrapped", False)

            if not hasattr(provider, "_llm_failover_original_text_chat"):
                provider._llm_failover_original_text_chat = provider.text_chat

            if not text_wrapped:
                # 仅替换普通聊天方法，保留原始实现以便切回或调试。
                async def wrapper(p_self, *args, **kwargs):
                    return await self._execute_with_failover(p_self, *args, **kwargs)

                provider.text_chat = types.MethodType(wrapper, provider)
                provider._llm_failover_text_wrapped = True

            if hasattr(provider, "text_chat_stream") and not stream_wrapped:
                if not hasattr(
                    provider, "_llm_failover_original_text_chat_stream"
                ):
                    provider._llm_failover_original_text_chat_stream = (
                        provider.text_chat_stream
                    )

                # 为流式聊天单独包装，避免流途中异常时无法切换。
                async def stream_wrapper(p_self, *args, **kwargs):
                    async for chunk in self._execute_stream_with_failover(
                        p_self, *args, **kwargs
                    ):
                        yield chunk

                provider.text_chat_stream = types.MethodType(
                    stream_wrapper, provider
                )
                provider._llm_failover_stream_wrapped = True

            if getattr(provider, "_llm_failover_text_wrapped", False) or getattr(
                provider, "_llm_failover_stream_wrapped", False
            ):
                provider._llm_failover_installed = True

        preferred_primary = None
        if self._provider_order:
            id_map = {self._get_provider_id(p): p for p in providers}
            for pid in self._provider_order:
                if pid in id_map:
                    preferred_primary = id_map[pid]
                    break
        if not preferred_primary and providers:
            preferred_primary = providers[0]

        ordered_for_log = self._ordered_providers(providers, preferred_primary)
        order_desc = ", ".join(
            self._get_provider_id(provider) for provider in ordered_for_log
        )
        self._log_failover(f"已启用故障转移，当前提供商顺序: {order_desc}")

    def _log_failover_result(
        self, provider_id: str, response: Any | None, errors: list
    ):
        preview = self._extract_response_text(response)[:120] if response else ""
        if errors:
            summary = ", ".join(
                f"{pid}: {type(exc).__name__} {exc}" for pid, exc in errors
            )
            self._log_failover(
                f"提供商 {provider_id} 成功返回。此前失败: {summary}"
            )
        else:
            self._log_failover(f"提供商 {provider_id} 成功返回。")
        if preview:
            self._log_failover(f"提供商 {provider_id} 响应预览: {preview}")

    def _iter_fallback_providers(self, primary):
        providers = [
            provider
            for provider in self.context.get_all_providers()
            if provider.meta().provider_type == ProviderType.CHAT_COMPLETION
        ]
        ordered = self._ordered_providers(providers, primary)
        if ordered:
            return ordered
        if primary:
            return [primary]
        return []

    def _get_prompt_preview(self, args: tuple, kwargs: dict) -> str:
        if args:
            return str(args[0])[:80]
        if "prompt" in kwargs:
            return str(kwargs["prompt"])[:80]
        return ""

    def _should_failover(self, exc: Exception) -> bool:
        code = getattr(exc, "status_code", None)
        if isinstance(code, int) and code in {
            401,
            402,
            403,
            408,
            409,
            429,
            500,
            502,
            503,
            504,
        }:
            return True
        message = str(exc).lower()
        keywords = [
            "rate limit",
            "too many requests",
            "429",
            "401",
            "invalid api key",
            "timeout",
            "timed out",
            "connection reset",
        ]
        return any(keyword in message for keyword in keywords)

    async def _execute_with_failover(self, primary, *args, **kwargs):
        order = self._iter_fallback_providers(primary)
        errors = []
        prompt_preview = self._get_prompt_preview(args, kwargs)

        for index, provider in enumerate(order):
            provider_id = self._get_provider_id(provider)
            original_call = getattr(
                provider, "_llm_failover_original_text_chat", provider.text_chat
            )

            self._log_failover(
                f"尝试调用提供商 {provider_id}，prompt: {prompt_preview or '[空]'}"
            )
            try:
                result = await original_call(*args, **kwargs)
                if isinstance(result, LLMResponse):
                    self._log_failover_result(provider_id, result, errors)
                else:
                    self._log_failover_result(provider_id, None, errors)
                return result
            except Exception as exc:
                if self._should_failover(exc) and index < len(order) - 1:
                    errors.append((provider_id, exc))
                    self._log_failover(
                        f"提供商 {provider_id} 异常: {type(exc).__name__} {exc}，尝试下一个提供商。"
                    )
                    continue
                self._log_failover(
                    f"提供商 {provider_id} 异常且无法继续故障转移: {type(exc).__name__} {exc}"
                )
                raise

        if errors:
            summary = "; ".join(
                f"{pid}: {type(exc).__name__} {exc}" for pid, exc in errors
            )
            self._log_failover(f"所有提供商均失败: {summary}")
            raise errors[-1][1]

        raise RuntimeError("没有可用的模型服务提供商")

    async def _execute_stream_with_failover(
        self, primary, *args, **kwargs
    ) -> AsyncGenerator[Any, None]:
        order = self._iter_fallback_providers(primary)
        errors = []
        prompt_preview = self._get_prompt_preview(args, kwargs)

        for index, provider in enumerate(order):
            provider_id = self._get_provider_id(provider)
            original_stream = getattr(
                provider,
                "_llm_failover_original_text_chat_stream",
                getattr(provider, "text_chat_stream", None),
            )
            original_call = getattr(
                provider,
                "_llm_failover_original_text_chat",
                getattr(provider, "text_chat", None),
            )

            if not original_stream and not original_call:
                errors.append((provider_id, RuntimeError("缺少聊天方法")))
                self._log_failover(
                    f"提供商 {provider_id} 未提供聊天接口，跳过。"
                )
                continue

            self._log_failover(
                f"尝试调用流式提供商 {provider_id}，prompt: {prompt_preview or '[空]'}"
            )
            first_piece = None
            emitted = False

            try:
                if original_stream:
                    async for chunk in original_stream(*args, **kwargs):
                        if first_piece is None:
                            first_piece = chunk
                        emitted = True
                        yield chunk
                else:
                    result = await original_call(*args, **kwargs)
                    first_piece = result
                    emitted = True
                    yield result

                self._log_failover_result(provider_id, first_piece, errors)
                return
            except Exception as exc:
                if emitted:
                    # 流式场景一旦已有输出，为避免混流不再切换，直接抛出。
                    self._log_failover(
                        f"提供商 {provider_id} 已输出部分内容，异常后停止切换: {type(exc).__name__} {exc}"
                    )
                    raise

                if self._should_failover(exc) and index < len(order) - 1:
                    errors.append((provider_id, exc))
                    self._log_failover(
                        f"提供商 {provider_id} 流式异常: {type(exc).__name__} {exc}，尝试下一个提供商。"
                    )
                    continue

                self._log_failover(
                    f"提供商 {provider_id} 流式异常且无法继续故障转移: {type(exc).__name__} {exc}"
                )
                raise

        if errors:
            summary = "; ".join(
                f"{pid}: {type(exc).__name__} {exc}" for pid, exc in errors
            )
            self._log_failover(f"所有提供商均失败: {summary}")
            raise errors[-1][1]

        raise RuntimeError("没有可用的模型服务提供商")

    def _extract_response_text(self, resp: Any | None) -> str:
        if not resp:
            return ""
        if isinstance(resp, LLMResponse):
            if resp.result_chain:
                try:
                    return resp.result_chain.get_plain_text()
                except Exception:
                    pass
            return resp.completion_text or ""
        if isinstance(resp, str):
            return resp
        result_chain = getattr(resp, "result_chain", None)
        if result_chain:
            try:
                return result_chain.get_plain_text()
            except Exception:
                pass
        completion_text = getattr(resp, "completion_text", None)
        if completion_text:
            try:
                return completion_text
            except Exception:
                return ""
        try:
            return str(resp)
        except Exception:
            return ""
