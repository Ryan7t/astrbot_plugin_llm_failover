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
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config  # 插件配置
        self._log_path = Path(__file__).resolve().parent / "llm_failover.log"
        self._log("LLM Failover 插件初始化，准备安装故障转移包装。")
        self._install_provider_failover()

    @filter.on_astrbot_loaded()
    async def on_bot_loaded(self):
        """AstrBot 启动完成后再次尝试安装故障转移，确保新加载的 Provider 被包装。"""
        self._log("AstrBot 已加载，刷新 Provider 故障转移包装。")
        self._install_provider_failover()

    def _log(self, message: str):
        """写入本地日志文件（llm_failover.log）。

        Args:
            message: 日志消息
        """
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{timestamp}] {message}\n")
        except Exception:
            pass  # 忽略日志写入失败，避免影响主流程

    def _log_failover(self, message: str):
        """同时写入 AstrBot 全局日志和本地日志文件。

        Args:
            message: 日志消息
        """
        astr_logger.info(f"[llm_failover][LLM] {message}")
        self._log(f"[LLM切换] {message}")

    def _install_provider_failover(self):
        """为所有聊天类 Provider 安装故障转移包装。"""
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

            # 缓存原始 text_chat 方法
            if not hasattr(provider, "_llm_failover_original_text_chat"):
                provider._llm_failover_original_text_chat = provider.text_chat

            # 仅对尚未包装的 Provider 进行普通聊天包装
            if not text_wrapped:

                async def wrapper(p_self, *args, **kwargs):
                    return await self._execute_with_failover(p_self, *args, **kwargs)

                provider.text_chat = types.MethodType(wrapper, provider)
                provider._llm_failover_text_wrapped = True

            # 如果存在流式聊天方法且尚未包装，则进行流式包装
            if hasattr(provider, "text_chat_stream") and not stream_wrapped:
                if not hasattr(
                    provider, "_llm_failover_original_text_chat_stream"
                ):
                    provider._llm_failover_original_text_chat_stream = (
                        provider.text_chat_stream
                    )

                async def stream_wrapper(p_self, *args, **kwargs):
                    async for chunk in self._execute_stream_with_failover(
                        p_self, *args, **kwargs
                    ):
                        yield chunk

                provider.text_chat_stream = types.MethodType(
                    stream_wrapper, provider
                )
                provider._llm_failover_stream_wrapped = True

            # 标记 Provider 已安装故障转移
            if getattr(provider, "_llm_failover_text_wrapped", False) or getattr(
                provider, "_llm_failover_stream_wrapped", False
            ):
                provider._llm_failover_installed = True

        # 读取用户配置的顺序，记录日志
        configured_order = self.config.get("provider_order", [])
        if configured_order:
            order_desc = ", ".join(configured_order)
            self._log_failover(f"已启用故障转移，用户配置顺序: {order_desc}")
        else:
            order_desc = ", ".join(
                provider.provider_config.get("id", "unknown") for provider in providers
            )
            self._log_failover(
                f"已启用故障转移，未配置 provider_order，当前提供商顺序: {order_desc}"
            )

    def _log_failover_result(
        self, provider_id: str, response: Any | None, errors: list
    ):
        """记录故障转移成功的结果日志。

        Args:
            provider_id: 成功返回的 Provider ID
            response: 响应对象
            errors: 此前失败的 Provider 和异常列表
        """
        # 提取响应预览（最多 120 字符）
        preview = self._extract_response_text(response)[:120] if response else ""
        if errors:
            # 汇总此前失败的 Provider 信息
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
        """根据用户配置的 provider_order 生成故障转移顺序。

        Args:
            primary: 当前正在使用的 Provider 实例

        Returns:
            按配置顺序排列的 Provider 列表，若配置为空则将 primary 放在首位
        """
        # 获取所有聊天类 Provider
        providers = [
            provider
            for provider in self.context.get_all_providers()
            if provider.meta().provider_type == ProviderType.CHAT_COMPLETION
        ]
        # 构建 ID -> Provider 的映射
        provider_map = {
            provider.provider_config.get("id", ""): provider for provider in providers
        }
        # 读取用户配置的顺序
        configured_order = self.config.get("provider_order", [])

        order = []
        if configured_order:
            # 完全按照用户配置的顺序重排
            for provider_id in configured_order:
                if provider_id in provider_map:
                    order.append(provider_map[provider_id])
            # 追加未在配置中的 Provider（以防配置过时）
            for provider in providers:
                if provider not in order:
                    order.append(provider)
        else:
            # 配置为空时，保留原行为：primary 在首位，其余不确定
            if primary:
                order.append(primary)
            for provider in providers:
                if provider not in order:
                    order.append(provider)

        return order if order else [primary]

    def _get_prompt_preview(self, args: tuple, kwargs: dict) -> str:
        """从请求参数中提取 prompt 预览，用于日志记录。

        Args:
            args: 位置参数元组
            kwargs: 关键字参数字典

        Returns:
            最多 80 字符的 prompt 预览字符串
        """
        if args:
            return str(args[0])[:80]
        if "prompt" in kwargs:
            return str(kwargs["prompt"])[:80]
        return ""

    def _should_failover(self, exc: Exception) -> bool:
        """判断异常是否应触发故障转移。

        Args:
            exc: 捕获的异常对象

        Returns:
            True 表示应切换到下一个 Provider，False 表示直接抛出异常
        """
        # 检查 HTTP 状态码
        code = getattr(exc, "status_code", None)
        if isinstance(code, int) and code in {
            401,  # 未授权
            402,  # 需要付款
            403,  # 禁止访问
            408,  # 请求超时
            409,  # 冲突
            429,  # 请求过多（限流）
            500,  # 服务器内部错误
            502,  # 网关错误
            503,  # 服务不可用
            504,  # 网关超时
        }:
            return True
        # 检查错误消息中的关键词
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
        """执行带故障转移的普通聊天请求。

        Args:
            primary: 当前正在使用的 Provider 实例
            *args: 传递给 text_chat 的位置参数
            **kwargs: 传递给 text_chat 的关键字参数

        Returns:
            LLMResponse 或原始响应对象

        Raises:
            Exception: 当所有 Provider 均失败时，抛出最后一个异常
        """
        # 获取故障转移顺序
        order = self._iter_fallback_providers(primary)
        errors = []  # 记录所有失败的 Provider 和异常
        prompt_preview = self._get_prompt_preview(args, kwargs)

        for index, provider in enumerate(order):
            provider_id = provider.provider_config.get("id", "unknown")
            # 获取原始的 text_chat 方法（未被包装的版本）
            original_call = getattr(
                provider, "_llm_failover_original_text_chat", provider.text_chat
            )

            self._log_failover(
                f"尝试调用提供商 {provider_id}，prompt: {prompt_preview or '[空]'}"
            )
            try:
                # 调用原始方法
                result = await original_call(*args, **kwargs)
                if isinstance(result, LLMResponse):
                    self._log_failover_result(provider_id, result, errors)
                else:
                    self._log_failover_result(provider_id, None, errors)
                return result
            except Exception as exc:
                # 判断是否应该切换到下一个 Provider
                if self._should_failover(exc) and index < len(order) - 1:
                    errors.append((provider_id, exc))
                    self._log_failover(
                        f"提供商 {provider_id} 异常: {type(exc).__name__} {exc}，尝试下一个提供商。"
                    )
                    continue
                # 不应该故障转移或已经是最后一个 Provider，直接抛出异常
                self._log_failover(
                    f"提供商 {provider_id} 异常且无法继续故障转移: {type(exc).__name__} {exc}"
                )
                raise

        # 所有 Provider 均失败，记录汇总日志并抛出最后一个异常
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
        """执行带故障转移的流式聊天请求。

        流式场景的特殊处理：一旦已有输出（emitted=True），则不再切换 Provider，
        避免混流导致响应错乱。

        Args:
            primary: 当前正在使用的 Provider 实例
            *args: 传递给 text_chat_stream 的位置参数
            **kwargs: 传递给 text_chat_stream 的关键字参数

        Yields:
            流式响应的 chunk 对象

        Raises:
            Exception: 当所有 Provider 均失败时，抛出最后一个异常
        """
        # 获取故障转移顺序
        order = self._iter_fallback_providers(primary)
        errors = []  # 记录所有失败的 Provider 和异常
        prompt_preview = self._get_prompt_preview(args, kwargs)

        for index, provider in enumerate(order):
            provider_id = provider.provider_config.get("id", "unknown")
            # 获取原始的流式聊天方法
            original_stream = getattr(
                provider,
                "_llm_failover_original_text_chat_stream",
                getattr(provider, "text_chat_stream", None),
            )
            # 获取原始的普通聊天方法（作为降级方案）
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
            first_piece = None  # 记录第一个 chunk 用于日志
            emitted = False  # 标记是否已有输出

            try:
                if original_stream:
                    # 优先使用流式方法
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

                self._log_failover_result(provider_id, first_piece, errors)
                return
            except Exception as exc:
                if emitted:
                    # 流式场景一旦已有输出，为避免混流不再切换，直接抛出
                    self._log_failover(
                        f"提供商 {provider_id} 已输出部分内容，异常后停止切换: {type(exc).__name__} {exc}"
                    )
                    raise

                # 判断是否应该切换到下一个 Provider
                if self._should_failover(exc) and index < len(order) - 1:
                    errors.append((provider_id, exc))
                    self._log_failover(
                        f"提供商 {provider_id} 流式异常: {type(exc).__name__} {exc}，尝试下一个提供商。"
                    )
                    continue

                # 不应该故障转移或已经是最后一个 Provider，直接抛出异常
                self._log_failover(
                    f"提供商 {provider_id} 流式异常且无法继续故障转移: {type(exc).__name__} {exc}"
                )
                raise

        # 所有 Provider 均失败，记录汇总日志并抛出最后一个异常
        if errors:
            summary = "; ".join(
                f"{pid}: {type(exc).__name__} {exc}" for pid, exc in errors
            )
            self._log_failover(f"所有提供商均失败: {summary}")
            raise errors[-1][1]

        raise RuntimeError("没有可用的模型服务提供商")

    def _extract_response_text(self, resp: Any | None) -> str:
        """从响应对象中提取纯文本内容，用于日志预览。

        Args:
            resp: LLMResponse 或其他响应对象

        Returns:
            提取出的纯文本字符串，失败则返回空字符串
        """
        if not resp:
            return ""
        # 优先处理 LLMResponse 类型
        if isinstance(resp, LLMResponse):
            if resp.result_chain:
                try:
                    return resp.result_chain.get_plain_text()
                except Exception:
                    pass
            return resp.completion_text or ""
        # 直接返回字符串类型
        if isinstance(resp, str):
            return resp
        # 尝试从属性中提取
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
        # 降级为字符串转换
        try:
            return str(resp)
        except Exception:
            return ""
