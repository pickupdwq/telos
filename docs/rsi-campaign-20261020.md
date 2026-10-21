# Telos 20 轮改进实验与最终匹配迁移记录

记录日期：2026-10-20。基线为 `5e6a917135f9a71ad4cb7d84a65bde8aa3beceb1`，最终测量候选为 `281757c98911553c0fd53d68969848bd79b00dce`。本记录覆盖 20 个不同的实验轮次：第 1 至 19 轮为开发与机制验证，第 20 轮为原先冻结的 16 次匹配迁移比较。17 轮被接受，第 10、16 轮被拒绝，第 20 轮结论为不确定（inconclusive）。本文件只记录已有事实和边界，不重新执行运行时测试，也不构成项目验收。

结论先说：已有证据支持“在监督下对系统和工作流进行了可测量的改进”，包括若干组件微基准、可选的原生验证并行路径和确定性的边界处理；没有证据支持自主 RSI、加速增长的通用智能、模型能力提升、通用代理能力提升或端到端交付加速。最终 16 次比较规模很小，由未盲的原始监督者人工审查，不是广谱能力基准。候选侧的请求数和 token 总数较低，但真实 provider 时间和包含监督者等待的 trial wall time 更高；这不是统一的效率增益。

## 口径与证据边界

- 组件微基准：例如第 3 轮的游标定位、第 12 轮的 SSE 扫描和第 15 轮的可选原生验证。它们只测量所指明的合成或本地路径，不包含网络、模型、监督者等待和完整交付时间。
- 可选原生验证速度：第 15 轮在同一 clean pin 上以相反顺序比较 serial 与 jobs4；这是验证阶段的可选路径，默认仍为 serial，不能外推为模型速度或总体交付加速。
- 真实 provider 时间：第 20 轮两侧分别记录模型请求耗时，包含修复和考试请求；它不是纯模型速度，也不是墙钟时间。
- 总交付墙钟：第 20 轮的 trial wall time 包含原始监督者的审查、等待和上下文续接。它与 provider 时间、组件基准不能合并成同一个指标。
- 实际匹配任务结果：第 20 轮完成 16 次真实匹配 trial，两侧最终均为 8/8 passed；首次提交接受数不同，且发生了修复与范围清理。该结果是描述性工程证据，不是统计显著性或通用能力结论。
- 测试数量、断言数量、诊断通过和 96 至 100 的合同分数只说明对应冻结检查通过，不说明通用智能、开发效率或模型能力。失败的尝试、修复、超时和考试尝试都保留在计数中，不拆成额外轮次。

## 20 轮实验

| 轮次 | 假设 | 实际观察 | 决定 | 实测增益或限制 | 下一轮原因 |
| --- | --- | --- | --- | --- | --- |
| 1 | 在来源校验前过滤和排序可用经验，可减少来源读取同时保留被选记录。 | 120 条记录工作负载中，来源调用从基线 120/120/120 降至候选 3/3/5，ID 完全一致，候选检查为 0；host 10/10 通过，0 skip，3.837s；`source_valid` 未变。 | 接受 | 证明的是操作次数减少，不是端到端延迟或模型能力；探针使用 stub。一次无效嵌套 sandbox 设置被取消并纠正，先前 27 请求和 774544 tokens 全部计入。 | 实际回调恢复仍需读取私有 courier 状态，因为项目状态未暴露事件 ID。 |
| 2 | 在状态中暴露精确项目 courier 身份，可避免回调恢复读取私有状态文件。 | full/summary 均暴露 `return_event` 的 id/status；独立探针覆盖 2 种视图的 20 个 phase/status 组合及 3 个缺失身份案例并通过；host 原生 project+compact 44/44 通过，0 skip，12.019s；authority/receive 未改。 | 接受 | 确定性恢复字段得到验证，但没有测量传输可靠性或通用延迟。sandbox 内原生套件出现 2 failures/4 errors，耗时 100.368s；独立 pinned host 44/44 说明兼容性。两次 closed-book 响应尝试按一个实验计入。 | 已测得约 90ms 的日志尾部定位目标；同时 100s 的无效 sandbox 尝试要求更严格的测试范围说明和后续验证工具。 |
| 3 | 用有界 C 级换行搜索替代 Python 逐字节扫描，可降低 observer 尾部定位延迟且不改变字节游标语义。 | 39 个 host observer 原生测试 0 skip/error/failure，14.484s；独立 54 个语义断言通过；相反顺序配对把中位数 89.863ms 降至 2.216ms、89.869ms 降至 2.215ms，分别 40.548x 和 40.564x。 | 接受 | 这是合成温缓存 observer 游标负载的局部增益；分片分配仍受 64KiB 输入块约束，并非零分配；14 次 work 请求与上一轮 31 次是不同任务，不能当过程提速。安装控制器未改。 | 把聚焦/全量测试选择和计时报告做成后续轮次可复用能力，回应约 100s sandbox 失败及反复手工构造回归的问题。 |
| 4 | 提供 import-safe、allowlist 化、带精确 ID、显式范围和逐测试结果/计时的回归 CLI，可使后续实验可复现且不丢失默认原生覆盖。 | pinned host 全量 418/418 通过，0 failure/error/skip，156.161s；独立 ID 比较保留原有 395 个测试并精确匹配 418 个声明 ID；私有 6-outcome/selection 探针和 15 个 pure 测试通过；一次超时后需同一 session 的 3 请求续接。 | 接受 | 建立了可复用的全量和聚焦选择路径，但全量 156.161s 与基线相近，不声称全量提速；聚焦计时范围更窄；超时和续接的消耗全部保留。安装控制器未改，最终实时比较仍未做。 | 第 2、3 轮都出现格式重试；一次合成实时请求已确认 Responses JSON-object 兼容，因此第 5 轮处理该摩擦并实际使用第 4 轮 CLI。 |
| 5 | 在已验证的 Responses 路由使用 native JSON Object，并给出明确 schema 修复说明，可减少可避免的格式重试；安全的逐尝试诊断能让剩余失败可测。 | scoped native 格式配置、4 个确定性重试场景、5 个 strict-parser 否定案例和隐私守卫通过；host 46/46 原生测试通过，22.680s，ID 独立导出且原选择测试全在；worker 和 host 都实际使用第 4 轮 CLI。尚无实测真实重试率收益。 | 接受 | 格式集成和安全诊断路径得到验证；单次合成请求不能估计重试率下降；安装控制器仍是基线，因此该轮考试不能归因于候选格式；46 个测试是选择范围，不是全量套件；6 个新增 pure 测试包含较紧的 prompt 文本耦合。 | 第 4 轮截止事件和已检查的 once-only 启动指引，促使在保留真实 deadline 与 exam reserve 的前提下公开时间和进一步请求提示。 |
| 6 | 公开新的时间和进一步 work 请求提示，可在不改变 deadline 与 exam reserve 的情况下改善 closeout。 | 在一次超时和一次被拒的原生回归后，修复的 pinned 候选 host 168/168 通过，0 failure/error/skip，73.006s；独立 relay fixture 证明 allowance 为 2/1/0、3 work+2 exam 请求，并排除 exams/compact/unspecified/localChat；真实实时完成/延迟效果仍未测。 | 接受 | 确定性 allowance 语义得到证明；安装控制器全程为基线，超时和考试数不能证明提示效度。41 次 work 请求后 admission timeout，又用 10 次同 session 请求完成；首次 167 测试运行有 1 个原会计测试错误，首交被拒 89/100，修复后 168 通过；新增解析和公开提示有每请求开销。 | 六项累积改动需要真实候选运行时验证；先运行独立冻结的四 trial 开发 pilot，再选择第 7 轮，最终 holdout 保持未用。 |
| 7 | 抑制没有监督者动作、仅显示 running 的新鲜唤醒，同时保留所有未完成和可执行的 courier 行为。 | pinned native 83/83 通过，0 failure/error/skip，15.139s；独立 97 断言覆盖 24 个被抑制的新鲜状态、27 个保留的可执行状态和 6 个既有 courier 状态；第 40 次 work 请求完成，无超时或修复 turn。 | 接受 | 行为边界得到验证；历史 7/23 个空通知只是动机，不估计端到端节省，也没有 live callback 延迟或可靠性增益。fixture 路径错误和多次相对路径编辑失败在同一 work turn 内纠正，40 次请求均计入；100/100 只是冻结合同满足。 | 第 6、7 轮因模块选择会带入 native 测试而直接选 unittest class；需要精确 class/test 选择，让聚焦验证继续可测量并保留默认全量覆盖。 |
| 8 | 精确 class/method 选择可让后续 worker 复用计时验证器，而不会误跑相邻 native 测试，同时保留默认全量覆盖。 | 独立 44 断言通过；worker 运行 21 个聚焦测试并列出 6 个前轮案例；host 447/447 native 测试通过，164.010s；installer 21/21 通过；原有 395 个 native 测试全保留，0 failure/error/skip。 | 接受 | 精确选择路径可复用，但选择覆盖较窄，不声称默认全量提速或模型质量。前 32 次 work 请求触发 admission timeout，同 session 再用 3 次完成；closed-book 在未安装候选的基线控制器下需 2 请求；100/100 只是合同满足。 | 三个已完成的真实候选 pilot exam 在完整 receipt 中保留第 5 轮格式元数据，但 compact summary 遗漏，迫使读取原始证据；需用已接受 runner 恢复安全 typed marker。 |
| 9 | 在 compact summary 中保留安全可选格式 marker，可让监督者验证实际考试协议和解析状态而无需额外读取原始 receipt。 | 独立 88 断言通过；host 41/41 compact/closed_book/verify_all 测试通过，0 failure/error/skip；两个 summary 路由对全部 3 个真实的前期候选 pilot exam 调用保留 `format_valid=true`、`format_error=null` 和对齐的 `json_object` 模式，source 未变。 | 接受 | 消除已观察的 summary 信息缺口；不是重试率或端到端延迟下降。重放使用已完成模型调用，没有新的 live trial。安装控制器仍为基线，候选 compact 代码仅为审查证据显式执行；100/100 仅合同满足。 | 第 8 轮 37 请求记录含 591107.863ms 的请求耗时，但 UsageLedger 快照和 project usage summary 都不汇总；需增加诚实的延迟覆盖，避免后续另读原始 ledger。 |
| 10 | 在 task/project usage 中暴露有限的已观察请求耗时总和及明确的 missing/in-flight 覆盖，同时不改变预算会计。 | 初版候选通过 161 个 host 回归并重放 299 个真实请求，但审查发现显式的 child incomplete marker 可能被变成 complete；初始私有 legacy fixture 又有已确认的随机 ID 排序缺陷，证据和成本保留。修正 fixture 后仅剩预算内，续接在 19 次 work 请求中因反复 malformed patch 耗尽，只改了 `usage.py`，所需 project 改动/测试缺失；公开 CLI 以 exit 2 失败。没有最终 pin 或 exam，全部第 10 轮代码排除。 | 拒绝 | 没有接受的能力或速度/质量增益。监督者 fixture 缺陷和全部 60 次 work 请求、2040773 tokens 均计入；纠正 fixture 不是额外实验。只剩 9 个接受轮次留在 source，安装控制器仍为基线；最终 continuation 无 closed-book exam，历史候选不保留。 | 转向在已接受第 9 轮上复现的另一个 relay admission 诊断实验；采用失败续接的公开工具调用格式建议，不重启已耗尽的时间实验，也不保留其未接受代码。 |
| 11 | 按 origin 限定 admission 诊断，可把已确认的 no-send deadline/closing 拒绝与协议失败区分开而不改变 enforcement。 | 两项冻结检查含 85 断言通过；worker 用 exact CLI 跑 30 pure；host 95/95 通过，32.489s；5 个真实 loopback 场景/7 请求保留状态、0 reservation/上游调用与清理。 | 接受 | 只证明诊断分类，不证明延迟、重试或任务失败下降。前 20 请求触达 deadline，4 请求同 session 完成；25 请求/1109445 tokens 计入。 | 已接受 parser 在基线测量中处理 2MiB terminal 花 230.911ms、丢弃 oversized data 花 95.215ms，需测试批量扫描。 |
| 12 | 批量扫描可去除 SSE usage 解析的逐字节解释器开销，同时保持流式与有界保留语义。 | 初版因 LF/CR 空行回归 4.2–4.5x 和公开 provenance 缺口被拒；同 session 修复后，1003 场景/349538 断言、29 pure、host 471 native+21 installer 全过，原有 395+21 保留。每序 9 样本：large 61.887x/61.177x、oversized 257.968x/253.762x、small 3.319x/3.313x。 | 接受 | 合成 parser 测量不含网络、模型或监督者，不能证明整体提速；66 请求/4625292 tokens 保留。 | 第 10 轮续接因 malformed patch 耗尽预算；改善初始/续接 prompt 的 patch 格式指导，保留权限边界。 |
| 13 | 在初始和续接 prompt 中提供紧凑 patch 格式和有界 fallback 建议，可补上指导缺口而不改变工具 enforcement。 | 11 routes/230 断言、20 pure、host 80/80（22.535s）通过；source review 确认 advisory-only 边界。开发期间仍有 5 次真实 patch 格式诊断拒绝，最终改用 scoped editing。 | 接受 | 建议投递与边界正确，但没有硬重试上限，也没有实测减少失败编辑或 live 速度/质量增益；21 请求/719815 tokens 计入。 | 临时 reader/lookup fixture 复现截断 JSON、非对象 JSON 或 hash 完好但缺失 repo 的记录问题，需隔离单个损坏记录。 |
| 14 | 隔离单个不可读或 malformed 经验记录，可防止一个损坏可选文件阻塞有效检索和新 work 设置。 | 三个 lookup 失败解决；39 场景/225 断言、11 pure（一次早先命令失败后）、native 86/86（25.425s）通过，原 ID 保留。 | 接受 | 只证明确定性损坏记录韧性，未测 live 成功率或端到端提速；范围限于只读 lookup/context；29 请求/1103930 tokens 计入。 | 全量原生验证约 160s，需测试 opt-in、模块进程隔离的并行路径并保留默认 serial、精确覆盖和可信失败报告。 |
| 15 | 有界新进程模块并行可降低全量原生验证时间，同时保持 serial 兼容、完整覆盖和可信失败报告。 | 同一 clean pin 四次全量均执行 499 测试（含 395 原 ID），顺序一致、0 failure/error/skip；164.5857s serial 对 49.9265s jobs4（3.2966x），166.9085s 对 50.1606s（3.3275x）；installer 21/21、26 pure、17 场景/116 断言和 9 补充案例通过。 | 接受 | 仅证明单主机验证阶段的可选加速，不是总体估计或模型/交付提速；默认仍 serial；35 请求/1668323 tokens 含首次 21 请求/633604 token 超时与修复。 | 已复现 Reader.read 跨 EOF poll 丢失 oversized physical-line discard 状态、可能误发附加 JSON 后缀；修复该游标边界并复用并行验证器。 |
| 16 | 跨 EOF poll 持久化每个 reader 的 oversized physical-line discard，可阻止附加 JSON 后缀成为伪事件。 | executor 只改 activity.py/test_watch.py，27 测试 exact parallel 在命令/patch 错误后通过；停止候选独立通过 50 场景/1373 断言。但 45 work 请求耗尽前无完成 turn、无 pin 或 exam，全部改动排除。 | 拒绝 | 缺陷仍留在继承 source；诊断通过不是 clean-pin host gate 或考试验收；无预算重置、放宽 gate 或额外重试轮。 | 在 R15 上继续已复现的 experience source-commit/project binding 缺陷；后续不得假设 ReaderOverflowTests 或修复存在。 |
| 17 | 将复用经验绑定到当前 acceptance commit/packet 与可信 source-project membership，可阻止陈旧或改标签记录绕过项目 gate。 | 22 场景/37 断言与 39 场景/225 断言通过；worker 用 jobs2 跑 33 测试；jobs4 native 122 测试含 65 原 ID，13.264s，0 failure/error/skip，AST 相同；closed-book 98，计 2 次格式尝试。 | 接受 | 验证确定性 source/project 边界，不防御对可信验收状态的重写；13.264s 无新 serial 对照；36 请求/1528141 tokens 计入；未推断模型或 live 质量增益。 | 真实 watch 复现 malformed timestamp 崩溃及 null 时间冒充当前时间；在排除 R16 代码的前提下修复人类显示/列表边界。 |
| 18 | 在人类 observer 边界隔离 malformed event/lifecycle timestamp，可保持 history、tail/follow 和排序可用而不虚构时间或改原始状态。 | 首次冻结检查因 huge-integer lifecycle 排序列拒，另发现有限 out-of-range float；修复后 41 场景/145 断言、2 对照/36 断言通过；jobs2 保留 3 条命令记录含 1 失败；jobs4 143 测试含 90 原 ID，15.058s，0 failure/error/skip；closed-book 98，计 2 次尝试。 | 接受 | curses frame 与 native 测试不是截图式视觉审计；timestamp 转换仍有平台差异；15.058s 无新 serial 对照；31 请求/1189311 tokens 计入。 | promotion 仍读取无关记忆并重写陈旧/外来记录；用精确 canonical accepted-child 记录处理该独立写路径。 |
| 19 | 只提升为 accepted children 重建的当前 canonical 记录，可消除无关读取/失败和陈旧/外来同 session 重写。 | 15 场景/221 断言通过，300 次无关读取变 0、只读 2 个目标；22/37 与 39/225 继续通过；jobs2 跑 38 测试；jobs4 130 测试含 65 原 ID，13.376s，0 failure/error/skip；仅 promote_project 变化，4 个测试类 AST 相同；closed-book 97。 | 接受 | 机制级减少不等于整体交付延迟或 live 质量增益；首次 work turn 32 请求后超时；44 请求/2362356 tokens 计入；不声称跨 child 原子提升。 | 冻结累积运行时，完成原有 16 次匹配 held-out trial，分别测量成功率、成本、provider 延迟和总 wall time。 |
| 20 | 累积已接受 Telos 改动可在原先 withheld 的匹配案例上改善真实委派任务的效率和质量，超出既有组件和验证机制。 | 16 次 trial（4 private 案例 x 2 repeats x 2 sides）完成，同一 executor/预算。最终均 8/8；首次提交接受基线 7/8、候选 6/8；请求 85→73（-14.12%），tokens 1263538→827156（-34.54%），provider 1272.794→1317.912s（+3.54%），wall 1763.288→2333.199s（+32.32%）。 | 不确定 | token 节省集中于一次基线审计，候选在 4/8 对 token 更多，配对中位比率 1.0495；provider/wall 反而更高。四案例、未盲审查和未固定远端 revision 不支持通用能力或因果结论；比较禁用经验复用。候选还需 1 次功能修复和 1 次范围清理，基线 1 次范围清理；parser 事后诊断另有候选 r1 same-chunk 恢复回归。 | 20 个不同实验已足够；冻结 17 个接受改进并排除 R10/R16 代码，不做针对已暴露比较的调参；最终 native/installer/project 验收作为独立交付。 |

## 最终比较与实际结果

实际 overridden private case 为 `private-stable-dependency-layers`（feature）、`private-atomic-idempotent-transfers`（bug_repair）、`private-incremental-record-parser`（feature）和 `private-path-boundary-audit`（code_audit），每例 2 repeats、两侧共 16 trial。harness 关于 public built-in fixtures 的警告是通用警告，不描述这些 overridden private cases；原始冻结案例未向优化 executor 披露。

- 任务结果：两侧最终均 8/8 passed；首次提交接受为基线 7/8、候选 6/8。候选 atomic r1 因额外 integer-only 账户余额限制首交 85 分被拒，同预算修复后通过；候选 stable r2 因生成字节码范围清理需修复；基线 atomic r2 也需范围清理。path audit r1 候选另有监督者 schema 修正，无额外 worker 请求。
- 请求与 token：85→73，差 -12（-14.12%）；1263538→827156，差 -436382（-34.54%）。请求在 5/8 对更低、1/8 相等、2/8 更高；token 在 4/8 对更低、4/8 更高，中位比率 1.0495。path audit r1 单对 token 差为 -498633，其余配对合计增加 62251，故总 token 下降不是统一改进。
- 时间：provider 1272.794→1317.912s，差 +45.118s（+3.54%）；trial wall 1763.288→2333.199s，差 +569.911s（+32.32%）。wall 包含人工/监督审查、等待和续接，不是纯模型速度；provider 也包含修复和考试请求。
- 审计：每侧 2 个已评分审计，true_positive 6、false_positive 0、false_negative 0；每次审计均找出全部 3 个植入缺陷。该计数只覆盖已评分且接受的审计报告，不能当作通用审计能力。
- 事后 parser 诊断：两侧候选均拒绝 NaN/Infinity/-Infinity，两侧基线均接受；候选 r1 在同一 chunk 坏行后丢失一个有效完整记录。同一 chunk 的错误恢复语义未被冻结任务规定；这些原始事后观察不引入新的 pass/fail 要求，也不改变冻结评分、source、预算或结果，并保留该项恢复回归。
- fractional-balance 诊断在首次候选源码审查发现未声明 integer-only 限制后加入，随后对称应用于全部 4 个 transfer trials；原 frozen oracle、预算和 rubric 未改，但该诊断未预注册。人工审查未盲，结论仅为描述性工程证据。

## 实际复用与固定边界

- 第 4 轮的 import-safe、精确 ID、范围和计时 CLI 被第 5、6、7 轮实际使用；第 8 轮 exact class/method 选择被第 9、11、12、13、14、15、16、17、18、19 轮在 worker 或 host 验证中实际运行。第 15 轮 jobs2/jobs4 模块并行被第 16、17、18、19 轮实际使用；第 20 轮全部 8 个候选 trial 实际运行累积已接受运行时。R10 初版和 R16 也调用过工具，但未成为接受提交。
- 第 1 轮 lazy provenance、第 14 轮损坏记录隔离、第 17 轮 source identity 和第 19 轮 canonical promotion 被保留并结合；第 10、16 轮未接受代码明确排除，第 16 轮之后不假设 Reader overflow 修复存在。
- 固定部分：原始 supervisor、安装的 supervisory controller 和模型权重均未更新；比较使用同一 Bailian `deepseek-v4.1-flash-1017`、reasoning effort `max` 和预算。provider alias 没有不可变 revision，远端路由/负载可能漂移，因此配对不等于完全同一上游权重。
- 达成层级是监督下的系统/工作流改进：可测的组件级减少、可选验证并行、确定性错误隔离和更可复用的验证工具。没有展示自主 RSI 或加速增长的通用智能，也不是模型自我改进、通用 agent 能力或所有任务效率承诺。

## 账目与交付限制

- 第 1 至 19 轮：718 请求、33338731 tokens；包含 setup failures、被拒实验、全部修复和考试。
- 第 20 轮 transfer：158 请求、2090694 tokens；包含全部 16 trial 的初失败、修复和考试（work 141、exam 17）。这是独立组，不与 1–19 轮合并成已完成总成本。
- 第 6 轮后 development pilot：独立 4 trial（每侧 2 trial）；基线 24 请求、445351 tokens、provider 535.847s、wall 881.591s；候选 18 请求、187959 tokens、provider 280.193s、wall 636.139s；两侧 2/2，通过各一次功能修复，未测出质量 uplift。它是开发证据，不是最终 holdout 或因果结论。
- 准备探针：第 5 轮前 `bailian-json-mode-probe.json` 仅 1 请求、138 tokens，只验证 Responses HTTP200/completed 和 `text.format json_object` 回显，不构成能力或速度实验。
- 报告编写开销：独立交付开销，只有在本 artifact 被接受后才能最终结算并另行提供；这里不虚构已完成总成本，也不把该开销塞入实验 totals。原始 supervisor 用量与 executor token 分开报告，缺少可靠货币换算，故不给出货币金额。
- 本文件是文档记录，不是新的运行时实验；未运行运行时套件，未进行 main 合并、push、install 或 deploy。运行时必须保持与已测量候选 `281757c98911553c0fd53d68969848bd79b00dce` 字节一致。最终集成 native tests、installer gate 和 project acceptance 有独立交付证据，尚需验证；本报告本身不能证明它们通过或构成最终验收。
