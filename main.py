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
    "1.0.2",
)
class LLMFailoverPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        """初始化故障转移插件。

        Args:
            context: AstrBot 上下文对象，提供框架核心组件访问能力。
            config: 插件配置对象，包含用户自定义的故障转移提供商顺序。
        """
        super().__init__(context)
        # 插件配置，包含用户定义的 provider_order 列表
        self.config = config
        # 日志文件路径
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
        """为所有聊天类提供商安装故障转移包装器。

        遍历所有 CHAT_COMPLETION 类型的提供商,替换其 text_chat 和
        text_chat_stream 方法为故障转移包装版本。已包装的提供商会跳过。
        """
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
            text_wrapped = getattr(provider, "_llm_failover_text_wrapped", False)
            stream_wrapped = getattr(provider, "_llm_failover_stream_wrapped", False)

            # 缓存原始 text_chat 方法,便于故障转移时调用
            if not hasattr(provider, "_llm_failover_original_text_chat"):
                provider._llm_failover_original_text_chat = provider.text_chat

            if not text_wrapped:
                # 替换普通聊天方法为故障转移包装器
                async def wrapper(p_self, *args, **kwargs):
                    return await self._execute_with_failover(p_self, *args, **kwargs)

                provider.text_chat = types.MethodType(wrapper, provider)
                provider._llm_failover_text_wrapped = True

            if hasattr(provider, "text_chat_stream") and not stream_wrapped:
                # 缓存原始流式聊天方法
                if not hasattr(
                    provider, "_llm_failover_original_text_chat_stream"
                ):
                    provider._llm_failover_original_text_chat_stream = (
                        provider.text_chat_stream
                    )

                # 替换流式聊天方法为故障转移包装器
                async def stream_wrapper(p_self, *args, **kwargs):
                    async for chunk in self._execute_stream_with_failover(
                        p_self, *args, **kwargs
                    ):
                        yield chunk

                provider.text_chat_stream = types.MethodType(
                    stream_wrapper, provider
                )
                provider._llm_failover_stream_wrapped = True

            # 标记提供商已安装故障转移
            if getattr(provider, "_llm_failover_text_wrapped", False) or getattr(
                provider, "_llm_failover_stream_wrapped", False
            ):
                provider._llm_failover_installed = True

        order_desc = ", ".join(
            provider.provider_config.get("id", "unknown") for provider in providers
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

    def _get_prompt_preview(self, args: tuple, kwargs: dict) -> str:
        if args:
            return str(args[0])[:80]
        if "prompt" in kwargs:
            return str(kwargs["prompt"])[:80]
        return ""

    def _should_failover(self, exc: Exception) -> bool:
        """判断异常是否应触发故障转移。

        Args:
            exc: 捕获的异常对象。

        Returns:
            True 表示应切换到下一个提供商,False 表示直接抛出异常。

        检查异常的 HTTP 状态码或错误消息关键词,识别常见的限流、
        认证失败、超时等瞬态错误。
        """
        code = getattr(exc, "status_code", None)
        # 检查 HTTP 状态码:401未授权、429限流、5xx服务器错误等
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
        # 检查错误消息关键词
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
        """执行带故障转移的 LLM 调用。

        Args:
            primary: 当前主提供商实例。
            *args, **kwargs: 传递给 text_chat 的原始参数。

        Returns:
            成功提供商的 LLMResponse 对象。

        Raises:
            最后一个失败的异常,或 RuntimeError(无可用提供商)。

        按配置或默认顺序依次调用提供商,遇到瞬态错误时自动切换,
        直到成功或耗尽所有候选提供商。
        """
        order = self._iter_fallback_providers(primary)
        errors = []
        prompt_preview = self._get_prompt_preview(args, kwargs)

        for index, provider in enumerate(order):
            provider_id = provider.provider_config.get("id", "unknown")
            # 获取该提供商的原始 text_chat 方法
            original_call = getattr(
                provider, "_llm_failover_original_text_chat", provider.text_chat
            )

            self._log_failover(
                f"尝试调用提供商 {provider_id}，prompt: {prompt_preview or '[空]'}"
            )
            try:
                result = await original_call(*args, **kwargs)
                # 成功返回,记录日志并返回结果
                if isinstance(result, LLMResponse):
                    self._log_failover_result(provider_id, result, errors)
                else:
                    self._log_failover_result(provider_id, None, errors)
                return result
            except Exception as exc:
                # 判断是否应该故障转移
                if self._should_failover(exc) and index < len(order) - 1:
                    errors.append((provider_id, exc))
                    self._log_failover(
                        f"提供商 {provider_id} 异常: {type(exc).__name__} {exc}，尝试下一个提供商。"
                    )
                    continue
                # 无法继续故障转移,抛出异常
                self._log_failover(
                    f"提供商 {provider_id} 异常且无法继续故障转移: {type(exc).__name__} {exc}"
                )
                raise

        # 所有提供商均失败
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
        """执行带故障转移的流式 LLM 调用。

        Args:
            primary: 当前主提供商实例。
            *args, **kwargs: 传递给 text_chat_stream 的原始参数。

        Yields:
            成功提供商的响应块(chunk)。

        Raises:
            最后一个失败的异常,或 RuntimeError(无可用提供商)。

        流式场景下,一旦已有输出则不再切换(避免混流),
        否则按配置或默认顺序依次尝试提供商。
        """
        order = self._iter_fallback_providers(primary)
        errors = []
        prompt_preview = self._get_prompt_preview(args, kwargs)

        for index, provider in enumerate(order):
            provider_id = provider.provider_config.get("id", "unknown")
            # 获取原始流式聊天方法或普通聊天方法作为降级
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
                    # 调用流式方法
                    async for chunk in original_stream(*args, **kwargs):
                        if first_piece is None:
                            first_piece = chunk
                        emitted = True
                        yield chunk
                else:
                    # 降级使用普通聊天方法
                    result = await original_call(*args, **kwargs)
                    first_piece = result
                    emitted = True
                    yield result

                # 流式输出成功完成
                self._log_failover_result(provider_id, first_piece, errors)
                return
            except Exception as exc:
                if emitted:
                    # 已有输出,为避免混流不再切换,直接抛出异常
                    self._log_failover(
                        f"提供商 {provider_id} 已输出部分内容，异常后停止切换: {type(exc).__name__} {exc}"
                    )
                    raise

                # 判断是否应该故障转移
                if self._should_failover(exc) and index < len(order) - 1:
                    errors.append((provider_id, exc))
                    self._log_failover(
                        f"提供商 {provider_id} 流式异常: {type(exc).__name__} {exc}，尝试下一个提供商。"
                    )
                    continue

                # 无法继续故障转移,抛出异常
                self._log_failover(
                    f"提供商 {provider_id} 流式异常且无法继续故障转移: {type(exc).__name__} {exc}"
                )
                raise

        # 所有提供商均失败
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
