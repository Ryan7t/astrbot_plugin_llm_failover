from __future__ import annotations

import asyncio
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
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # 加载插件配置，支持用户通过 WebUI 自定义故障转移行为
        self.config = config
        self._log_path = Path(__file__).resolve().parent / "llm_failover.log"
        self._log("LLM Failover 插件初始化，准备安装故障转移包装。")
        self._install_provider_failover()

    @filter.on_astrbot_loaded()
    async def on_bot_loaded(self):
        """AstrBot 启动完成后再次尝试安装故障转移，确保新加载的 Provider 被包装。"""
        self._log("AstrBot 已加载，刷新 Provider 故障转移包装。")
        self._install_provider_failover()

    def _log(self, message: str):
        """写入本地日志文件，记录插件运行时信息"""
        # 仅在配置允许时写入本地日志文件
        if not self.config.get("log_enabled", True):
            return
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
        """为所有聊天类提供商安装故障转移包装器"""
        # 检查是否启用故障转移功能
        if not self.config.get("enabled", True):
            self._log_failover("故障转移功能已禁用，跳过安装。")
            return

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

            # 保存原始方法引用，用于后续调用
            if not hasattr(provider, "_llm_failover_original_text_chat"):
                provider._llm_failover_original_text_chat = provider.text_chat

            if not text_wrapped:
                # 为普通聊天方法添加故障转移包装，保留原始实现以便调试或切回
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

                # 为流式聊天方法添加故障转移包装，避免流途中异常时无法切换
                async def stream_wrapper(p_self, *args, **kwargs):
                    async for chunk in self._execute_stream_with_failover(
                        p_self, *args, **kwargs
                    ):
                        yield chunk

                provider.text_chat_stream = types.MethodType(
                    stream_wrapper, provider
                )
                provider._llm_failover_stream_wrapped = True

            # 标记该提供商已安装故障转移包装
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
        """
        构建故障转移的提供商顺序列表。

        优先级规则：
        1. 如果用户配置了 provider_order，完全按照该顺序排列，不再将 primary 置于首位
        2. 如果未配置 provider_order，则按原有逻辑：primary 优先，其他提供商按框架顺序排列

        Args:
            primary: 当前会话正在使用的提供商

        Returns:
            提供商顺序列表
        """
        providers = [
            provider
            for provider in self.context.get_all_providers()
            if provider.meta().provider_type == ProviderType.CHAT_COMPLETION
        ]

        # 获取用户配置的提供商顺序
        configured_order = self.config.get("provider_order", [])

        if configured_order:
            # 用户配置了自定义顺序，完全按照该顺序排列，不依赖框架的 primary
            order = []
            provider_map = {
                provider.provider_config.get("id", ""): provider
                for provider in providers
            }

            # 按照配置顺序添加提供商
            for provider_id in configured_order:
                if provider_id in provider_map:
                    order.append(provider_map[provider_id])

            # 将未在配置中的提供商追加到末尾
            for provider in providers:
                if provider not in order:
                    order.append(provider)

            self._log_failover(
                f"使用配置的提供商顺序: {', '.join(p.provider_config.get('id', 'unknown') for p in order)}"
            )
        else:
            # 未配置自定义顺序，使用默认逻辑：primary 优先
            order = []
            if primary:
                order.append(primary)
            for provider in providers:
                if provider is primary:
                    continue
                if provider not in order:
                    order.append(provider)

        return order if order else [primary] if primary else []

    def _get_prompt_preview(self, args: tuple, kwargs: dict) -> str:
        if args:
            return str(args[0])[:80]
        if "prompt" in kwargs:
            return str(kwargs["prompt"])[:80]
        return ""

    def _should_failover(self, exc: Exception) -> bool:
        """
        判断当前异常是否应触发故障转移。

        检查 HTTP 状态码和异常信息，识别限流、认证失败、超时等可恢复错误。

        Args:
            exc: 捕获的异常对象

        Returns:
            True 表示应该尝试下一个提供商，False 表示直接抛出异常
        """
        code = getattr(exc, "status_code", None)
        # 检查 HTTP 状态码，匹配常见的临时性错误
        if isinstance(code, int) and code in {
            401,  # 认证失败
            402,  # 需要付费
            403,  # 禁止访问
            408,  # 请求超时
            409,  # 冲突
            429,  # 速率限制
            500,  # 服务器内部错误
            502,  # 网关错误
            503,  # 服务不可用
            504,  # 网关超时
        }:
            return True

        # 检查异常信息中的关键词，识别常见的限流或网络问题
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
        """
        执行带故障转移的 LLM 调用（普通聊天模式）。

        按照配置的提供商顺序依次尝试，直到成功或全部失败。

        Args:
            primary: 当前会话正在使用的提供商
            *args, **kwargs: 传递给 text_chat 的参数

        Returns:
            成功的 LLM 响应结果

        Raises:
            最后一个提供商的异常或 RuntimeError
        """
        order = self._iter_fallback_providers(primary)
        errors = []
        prompt_preview = self._get_prompt_preview(args, kwargs)

        # 获取配置的重试延迟时间
        retry_delay = self.config.get("retry_delay", 0.0)

        for index, provider in enumerate(order):
            provider_id = provider.provider_config.get("id", "unknown")
            # 获取原始的 text_chat 方法，避免递归调用包装器
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
                # 判断是否应该继续尝试下一个提供商
                if self._should_failover(exc) and index < len(order) - 1:
                    errors.append((provider_id, exc))
                    self._log_failover(
                        f"提供商 {provider_id} 异常: {type(exc).__name__} {exc}，尝试下一个提供商。"
                    )
                    # 在切换到下一个提供商前等待指定时间
                    if retry_delay > 0:
                        await asyncio.sleep(retry_delay)
                    continue
                self._log_failover(
                    f"提供商 {provider_id} 异常且无法继续故障转移: {type(exc).__name__} {exc}"
                )
                raise

        # 所有提供商均失败，记录汇总日志并抛出最后一个异常
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
        """
        执行带故障转移的 LLM 调用（流式聊天模式）。

        流式场景下，一旦开始输出内容，为避免混流问题，不再进行故障转移。

        Args:
            primary: 当前会话正在使用的提供商
            *args, **kwargs: 传递给 text_chat_stream 的参数

        Yields:
            流式响应的各个数据块

        Raises:
            最后一个提供商的异常或 RuntimeError
        """
        order = self._iter_fallback_providers(primary)
        errors = []
        prompt_preview = self._get_prompt_preview(args, kwargs)

        # 获取配置的重试延迟时间
        retry_delay = self.config.get("retry_delay", 0.0)

        for index, provider in enumerate(order):
            provider_id = provider.provider_config.get("id", "unknown")
            # 优先使用原始的流式方法，若不存在则降级为普通方法
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
                    # 使用流式方法
                    async for chunk in original_stream(*args, **kwargs):
                        if first_piece is None:
                            first_piece = chunk
                        emitted = True
                        yield chunk
                else:
                    # 降级为普通方法
                    result = await original_call(*args, **kwargs)
                    first_piece = result
                    emitted = True
                    yield result

                self._log_failover_result(provider_id, first_piece, errors)
                return
            except Exception as exc:
                if emitted:
                    # 流式场景一旦已有输出，为避免混流不再切换，直接抛出异常
                    self._log_failover(
                        f"提供商 {provider_id} 已输出部分内容，异常后停止切换: {type(exc).__name__} {exc}"
                    )
                    raise

                # 判断是否应该继续尝试下一个提供商
                if self._should_failover(exc) and index < len(order) - 1:
                    errors.append((provider_id, exc))
                    self._log_failover(
                        f"提供商 {provider_id} 流式异常: {type(exc).__name__} {exc}，尝试下一个提供商。"
                    )
                    # 在切换到下一个提供商前等待指定时间
                    if retry_delay > 0:
                        await asyncio.sleep(retry_delay)
                    continue

                self._log_failover(
                    f"提供商 {provider_id} 流式异常且无法继续故障转移: {type(exc).__name__} {exc}"
                )
                raise

        # 所有提供商均失败，记录汇总日志并抛出最后一个异常
        if errors:
            summary = "; ".join(
                f"{pid}: {type(exc).__name__} {exc}" for pid, exc in errors
            )
            self._log_failover(f"所有提供商均失败: {summary}")
            raise errors[-1][1]

        raise RuntimeError("没有可用的模型服务提供商")

    def _extract_response_text(self, resp: Any | None) -> str:
        """
        从响应对象中提取纯文本内容，用于日志预览。

        支持多种响应格式：LLMResponse、字符串、带 result_chain 的对象等。

        Args:
            resp: LLM 响应对象

        Returns:
            提取的文本内容，失败时返回空字符串
        """
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
