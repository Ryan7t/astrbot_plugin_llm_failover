from __future__ import annotations

import types
from datetime import datetime
from pathlib import Path

from astrbot.api import logger as astr_logger
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
    def __init__(self, context: Context):
        super().__init__(context)
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

        for provider in providers:
            if getattr(provider, "_llm_failover_installed", False):
                continue
            if not hasattr(provider, "_llm_failover_original_text_chat"):
                provider._llm_failover_original_text_chat = provider.text_chat

            async def wrapper(p_self, *args, **kwargs):
                return await self._execute_with_failover(p_self, *args, **kwargs)

            provider.text_chat = types.MethodType(wrapper, provider)
            provider._llm_failover_installed = True

        order_desc = ", ".join(
            provider.provider_config.get("id", "unknown") for provider in providers
        )
        self._log_failover(f"已启用故障转移，当前提供商顺序: {order_desc}")

    def _log_failover_result(
        self, provider_id: str, response: LLMResponse | None, errors: list
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
        order = []
        if primary:
            order.append(primary)
        for provider in providers:
            if provider is primary:
                continue
            if provider not in order:
                order.append(provider)
        return order if order else [primary]

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
        prompt_preview = ""
        if args:
            prompt_preview = str(args[0])[:80]
        elif "prompt" in kwargs:
            prompt_preview = str(kwargs["prompt"])[:80]

        for index, provider in enumerate(order):
            provider_id = provider.provider_config.get("id", "unknown")
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

    def _extract_response_text(self, resp: LLMResponse | None) -> str:
        if not resp:
            return ""
        if resp.result_chain:
            try:
                return resp.result_chain.get_plain_text()
            except Exception:
                pass
        return resp.completion_text or ""
