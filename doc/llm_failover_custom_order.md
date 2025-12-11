# 自定义故障转移顺序实现记录

## 问题概述
- 现有故障转移顺序依赖调用入口的 primary Provider 在首位，无法满足“按人为配置的优先级完全重排首选提供商”的需求。
- 需要在插件层通过配置文件指定聊天 Provider 的尝试顺序，覆盖默认的 primary 优先策略，并兼容流式与非流式调用。

## 备选方案与取舍
- 方案 A：保持 primary 在首位，仅在失败后参考配置调整后续顺序，无法真正重排首选，放弃。
- 方案 B：新增插件配置 `provider_order`，在每次故障转移时按配置顺序完全重建 Provider 队列，primary 仅作为兜底，最终选用。
- 方案 C：在业务层手动切换 Provider 或修改全局默认 Provider，侵入性高且缺少插件级可视化配置，放弃。

## 实施步骤/关键改动
- 新增 `_conf_schema.json` 定义 `provider_order` 列表配置，支持在管理界面按优先级填写 Provider ID。
- `main.py` 接收 `config` 并保存 `_provider_order`，新增 `_normalize_provider_order`、`_get_provider_id`、`_ordered_providers`，按配置顺序构建故障转移队列（未配置的 Provider 自动排在末尾）。
- 安装阶段记录缺失的配置 ID 并按配置顺序输出启用日志；执行阶段（含流式）统一改用 `_ordered_providers` 和 `_get_provider_id` 生成调用顺序与日志。

## 优缺点与结论
- 优点：可视化配置即可决定首选 Provider，顺序统一作用于同步/流式调用；配置缺失项会提示且其余 Provider 仍可参与故障转移。
- 缺点：配置 ID 需与实际 Provider 保持一致，否则会被跳过；首选顺序与框架默认不一致时需提醒使用方。
- 结论：插件已支持通过配置完全重排故障转移顺序，满足定制化首选提供商的要求并保持原有自动切换能力。
