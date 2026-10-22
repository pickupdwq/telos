# 文档索引 / Documentation Index

本页按使用场景索引 `docs/` 下的文档和数据。返回项目首页：[English](../README.md) · [中文](../README.zh-CN.md)。文档索引只帮助查找资料，不改变运行时行为或实验结论。

## 快速开始与配置 / Quick start & configuration

- [安装与快速开始（English）](../README.md#quick-start)：英文安装、CLI、MCP 注册、凭据和首次任务步骤。
- [安装与快速开始（中文）](../README.zh-CN.md#快速开始)：对应的中文安装与首次任务说明。
- [配置指南 / Configuration](configuration.md)：安装后的配置、凭据与服务商、预算、耗时和结构化错误，以及观察面板配置。
- [本地执行者 / Local executors](local-executors.md)：保存和切换回环地址入口，选择 Responses 或 Chat Completions，并了解协议边界。

## 架构 / Architecture

- [架构说明 / Architecture](architecture.md)：大总管、运行时和执行者的角色，执行协议、持久事件、模块划分、信任边界、项目与经验复用；含中英文说明和图示。

## 大总管、工具、走查与观察 / Supervision, tools, audits & observer

- [大总管 Skill / Supervisor Skill](skills.md)：Skill 的适用范围、支持的流程，以及安装和升级方式。
- [MCP 工具 / MCP tools](tools.md)：面向原大总管的 25 个工具、用途和必填参数。
- [工具结构定义 / Tool schemas](tools.schema.json)：上述工具的机器可读字段结构。
- [系统走查指南 / System audits](audits.md)：冻结检查范围和证据要求、接口与浏览器探针、走查验收及修复衔接。
- [执行者观察面板 / Live executor observer](watch.md)：只读观察面板、tmux 配置、记录内容和验证方式。

## 测试与对比 / Testing & comparison

- [测试指南 / Testing](testing.md)：源码回归、精确选择与可选并行验证、安装冒烟测试、Skill 验证、可选浏览器检查及 CI。
- [评测与对比 / Evaluation and comparison](comparison.md)：用固定任务和预算比较两个冻结的 Telos 运行时，以及结果和成本解读。
- [失败诊断保留 / Relay diagnostics](rsi-r002-diagnostics.md)：预算关闭时保留有界、脱敏的 relay 诊断，不改变重试权限。
- [异常来源脱敏 / Failure-origin redaction](rsi-r003-origin-redaction.md)：先脱敏再截断，保留预算、线程和超时语义。
- [耗时统计正确性 / Duration correctness](rsi-r004-durations.md)：整数精度、异常时间戳及 unknown 的统一处理。

## 历史 RSI 报告与 JSON 证据 / Historical RSI reports & JSON evidence

> 本节是历史记录，不是当前活动、当前任务进展或后续轮次的验收状态。

- [20261020 二十轮改进实验报告 / 20-round campaign report](rsi-campaign-20261020.md)：2026-10-20 至 21 日的历史实验、逐轮结果、最终匹配对照、复用情况和证据边界。
- [20261020 二十轮实验数据 / 20-round campaign data](rsi-campaign-20261020.json)：同一历史活动的轮次、最终对照、用量账目和限制。
- [20261021 最终交付摘要 / Final delivery summary](rsi-results-20261021.json)：上述活动的公开最终摘要、验收提交、验证与源证据哈希；文件明确将其范围标为历史记录。
- [2026-10-22 RSI 收尾交付 / RSI closeout](reports/2026-10-22-rsi60-closeout.md)：前期 Mac 修复、R001–R011 全部轮次、收尾纳入范围、验证、复现和回退；未完成 60 轮，无已确认效率收益。
- [2026-10-22 RSI 公开数据 / Public closeout data](reports/2026-10-22-rsi60-closeout.json)：台账生成计数、原轮次判定、对照样本、证据哈希与组合回归摘要，不包含私有验收材料。
- [未纳入运行时的候选 / Archived candidates](reports/rsi60-20261022-candidates/README.md)：R005、R010 可复原补丁及其证据限制；只归档，不默认启用。

## 图像资源 / Image assets

- [图片资源目录 / Assets directory](assets/)：项目横幅，以及英文和中文的架构图、执行时序图的 PNG/SVG 版本。

## 贡献与安全 / Contributing & security

- [贡献指南 / Contributing](../CONTRIBUTING.md)：开发流程、必须保持的不变量及中文说明。
- [安全说明 / Security](../SECURITY.md)：漏洞报告方式、运行边界及中文说明。
