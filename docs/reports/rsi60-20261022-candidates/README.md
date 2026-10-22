# 隔离候选归档 / Isolated candidate archive

这些补丁仅保存研究成果，不属于交付分支的运行时代码。没有启动后续付费验证，也不授权复活预算未知的旧任务。

| 补丁 | 原固定候选 | 内容 | 为什么未纳入 |
| --- | --- | --- | --- |
| [R005.patch](R005.patch) | `960d79bfab7f8402adf27d26254132da9da50e5e` | 实现阶段的连贯执行包提示 | 首组完整耗时增加 13.86%，Token 减少 4.78%；重复测量失败，用量未知，未证明净收益 |
| [R010.patch](R010.patch) | `468e5a6d4f3eafb722d9e0ef4ae1c10e1f2d0c12` | 默认关闭的 `evaluate_on_completion`，成功交付后衔接正常检查/考试，绝不自动评分 | 611 项原生测试及独立竞态检查曾通过，但真实对照基线失败，没有完整配对、重复确认或未见任务验收 |

## 精确复原范围

两个补丁都以已发布的 **main / v3.7.8** 提交 `0c462765f6784b573b2ee1a0adfbd7223e06fb51` 为起点，包含相应候选所需的前序运行时改动。不要叠加，不要直接应用到本交付分支。

补丁完整复原原固定候选的 `runtime/` 树，以及相关的配置/MCP 文档。原提交中的个人路径、历史报告、任务状态及私有考试材料不在补丁中；这不是原提交的完整 Git 历史副本。候选提交号只作来源标识，不要求这些私有历史提交存在于远端。

在另一个干净的临时检出中，以 R010 为例：

```sh
git switch --detach 0c462765f6784b573b2ee1a0adfbd7223e06fb51
git apply --check /absolute/path/to/delivery/docs/reports/rsi60-20261022-candidates/R010.patch
git apply /absolute/path/to/delivery/docs/reports/rsi60-20261022-candidates/R010.patch
python3 -B runtime/verify_all.py /absolute/private-output/candidate-native.json --jobs 2
```

只在专用、干净的实验检出运行，勿操作日常安装或他人工作区。以上原生验证使用本地模拟模型，不消耗推理额度。真实上游验证需要另外核清稳定性、费用和预算边界。

收尾验证通过临时 Git 索引应用每份补丁，逐文件核对 blob，并确认整个 `runtime/` Git tree 与原候选完全一致；哈希及结果见[公开数据](../2026-10-22-rsi60-closeout.json)。补丁格式中的空白上下文行是必要数据，因此补丁单独做应用与树一致性校验，普通源码另做 `git diff --check`。

R010 的已知诊断限制：账目不可用时，自动评估标记仍归为 `budget_exhausted`，没有独立的 unavailable 分类；仍然禁止继续付费。该归档没有额外修复这一点。
