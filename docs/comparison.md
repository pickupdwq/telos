# Evaluation and comparison / 评测与对比

Compare two **frozen Telos runtimes**, using the same fixed tasks, executor
configuration, initial instructions, questionnaire, checks and per-trial budget.
This is an explicitly started experiment. Ordinary MCP tasks keep their existing
work → checks → same-thread closed-book exam → original-supervisor review →
repair/acceptance lifecycle. No new tools are added to their MCP catalog.

The first release provides a separate CLI entry point. The **current supervisor**
drives each experiment through the existing task tools in explicit manual mode;
there is no automatic grader, background optimization or change to ordinary
Desktop callbacks. Each trial is a separate process with its own runtime snapshot,
repository, state, cache and model conversation. Experiences are not retrieved
across trials or published into ordinary task state. No installation, global
model selection, Skill or source branch is replaced.

## Prepare an experiment

Use the installed `telos compare` command, or run from source:

```sh
python3 runtime/telos.py compare suite
```

The bundled suite has 20 **public development fixtures**: 12 bug repairs, four
small features and four source audits. Two audits inspect UI source (dialog keys
and responsive CSS). These do **not** test a running page, browser interaction,
screenshots, visual quality or real API behavior. They are initial engineering
checks, not representative evidence of overall agent capability or a secret
holdout. All bundled seeds and oracles are published in this repository.

Create a private specification outside a task repository. Paths below are
examples; point `config` to your existing key-free Telos configuration. Reuse
its credential references and active local endpoint. Never put a key in the
specification or a command argument.

```json
{
  "label": "Baseline versus candidate: first smoke comparison",
  "baseline": "/absolute/path/to/baseline/runtime",
  "candidate": "/absolute/path/to/candidate/runtime",
  "config": "/absolute/path/to/installed/telos/config.json",
  "executor_profile": "local",
  "case_ids": ["clamp-bounds", "audit-pagination"],
  "repeats": 1,
  "max_rounds": 2,
  "budget": {"max_requests": 16, "max_total_tokens": 160000},
  "upstream_revision": null
}
```

Both runtime directories must support the current manual closed-book workflow
and usage ledger (v3.5.0 or a compatible candidate). A repository root containing
`runtime/telos.py` is also accepted. Source trees may be under development: the
experiment copies and hashes their runtime files once. Changes to the originals
after initialization do not change the experiment. Existing experiment
directories are never overwritten.

These limits are illustrative, not an estimate of guaranteed completion cost.
Tool definitions and resumed conversation history count toward every request's
input tokens, including cached input. An 80,000-token pilot can stop even a
small audit before its exam. Choose the allowance before initializing, keep
budget-limited failures in the report, and never edit a running experiment's
frozen allowance or reopen its tasks merely to bypass the stop.

```sh
python3 runtime/telos.py compare init --spec /private/path/spec.json --run /private/path/new-run
python3 runtime/telos.py compare next --run /private/path/new-run
```

Initialization makes no inference requests. It freezes runtime/configuration
hashes, source commits, task contracts, order and budgets. Pairs run adjacently;
the first side alternates across cases and repetitions. Both sides resolve the
same model, endpoint and effort, but a rolling model alias or gateway route is
not an immutable upstream weight revision. Record a known `upstream_revision`
when available; it is an operator declaration, not independently verified.

## Run and review each trial

`start --live` explicitly authorizes one bounded trial and opens its isolated
task. It does not send a model request until `call telos_send`. Repeating `start`
returns the current trial. The experiment runs one trial at a time and prevents
another comparison from claiming its host-level slot until that trial finishes.

```sh
python3 runtime/telos.py compare start --run /private/path/new-run --live
printf '{}\n' | python3 runtime/telos.py compare call --run /private/path/new-run --tool telos_send
printf '{}\n' | python3 runtime/telos.py compare call --run /private/path/new-run --tool telos_status
```

Pass `{}` for the first send: the harness supplies identical frozen work
instructions. Calls cannot override the selected session or open extra tasks.
All remaining calls take the usual tool arguments through stdin, **without
`session_id`**. Wait for work to finish, then:

1. Inspect status and the actual submission. If a completion receipt is
   present, use `telos_receive` with its event ID and carry that ID into the
   next action. Manual-mode tasks do not send callbacks into the ordinary chat.
2. Call `telos_evaluate`. The selected runtime runs checks first, then resumes
   the same executor for a tool-free exam. Failed checks require public repair.
3. Call `telos_review` with `{"detail":"full"}`. Read the pinned source, answers,
   checks and evidence. The original supervisor supplies question scores,
   rationales and blockers through `telos_grade`; the harness never invents them.
4. If rejected, use `telos_repair` or `telos_send` with public symptoms and
   requirements. Never forward private answers or check code. Repair and exam
   calls share the original frozen budget. Review audit evidence and include
   `audit_review` when grading audit tasks, just as in the ordinary workflow.
5. Once accepted, finish the trial. The harness independently runs the frozen
   implementation outcome checks, or compares supervisor-confirmed audit defects
   against seeded defects. Dismissed findings and suggestions do not count as
   confirmed defects. A questionnaire pass alone cannot produce a passing outcome.

```sh
python3 runtime/telos.py compare finish --run /private/path/new-run
python3 runtime/telos.py compare next --run /private/path/new-run
python3 runtime/telos.py compare start --run /private/path/new-run --live
```

An unaccepted trial can be closed with `finish --reason "concrete failure"`.
It counts as a failure, not a success or silently discarded sample. Preserve
the infrastructure/model failure reason. A request must finish before its trial
can be closed; starting another experiment is not a way to reset a budget.

The supervisor can pause, resume or cancel:

```sh
python3 runtime/telos.py compare pause --run /private/path/new-run
python3 runtime/telos.py compare resume --run /private/path/new-run
python3 runtime/telos.py compare cancel --run /private/path/new-run
```

Cancellation is permanent. An in-flight provider request may finish; no next
stage starts automatically. Finish the cancelled active trial with a reason to
save its spend and release the host-level slot. Keep its directory until then.

Before paid actions, the harness checks ordinary task/project state and yields
if normal work is active. A normal task launched *after* that check is not
preempted or blocked by the comparison: finish the current provider request,
then pause the experiment. The first version does not promise an atomic
scheduler shared with older installed runtimes.

## Read results and costs

```sh
python3 runtime/telos.py compare report --run /private/path/new-run
```

This writes `report.json` and `report.md`. The reports contain:

- Frozen runtime/configuration identities, selected model and trial order.
- Planned/completed/passed/failed/cancelled/pending counts. Success rate uses
  **all planned trials** as its denominator; an incomplete run is not comparable.
- Paired outcome regressions and improvements, plus per-trial evidence paths.
- Request/token totals including failed requests, repairs and exams. Cost per
  success includes spend on failed trials. Missing usage stays **unknown**;
  observed partial totals never masquerade as exact savings.
- Trial wall time (including manual waiting/review) and separately measured
  provider latency, when available. These measure different things.
- Seeded code-audit true positives, false positives and missed defects, using
  supervisor-confirmed defects in accepted reports. Failed/unaccepted audits
  remain trial failures; `audit_oracles_scored` states the detection-count coverage.

Aggregates currently cover finished trials; inspect `telos_status` for active
trial usage. Original supervisor tokens and monetary charges are unavailable
and are not fabricated. The request ceiling is planned trial count times the
per-trial `max_requests`; runtime gates enforce each trial's allowance. Token
budgets retain the existing between-request semantics: one request can cross a
token limit, after which further requests stop. This is not a monetary hard cap.

Partial/cancelled comparisons are marked non-comparable. The report never
selects a winner or promotes a runtime automatically. Review quality and
regressions alongside cost; spending less because more tasks failed is not an
efficiency gain. Manual supervisor feedback is recorded and is not a blinded
evaluation. Use repeated, paired runs and independent private tasks before
claiming improvement. Passing this mode does not establish recursive
self-improvement.

## Custom fixed tasks

Replace `case_ids` with a `cases` array in the specification. Each case contains
`id`, `category`, `split` (`development`, `validation` or `holdout`), a `files`
mapping of relative paths to source text, `task`, `questions`, `checks`, and
explicit `allowed_paths` for implementation. Question/check fields are the
existing Telos schema. For offline audits, provide `audit` and
`expected_findings` (defective checklist IDs) instead of write paths. Network
checks, live targets and browser adapters are not supported in this initial
fixture runner. See `runtime/evaluation_suite.py` for working examples.

The evaluator must curate private holdouts separately and keep them out of
candidate prompts, task source and public version control. A `holdout` label
does not itself make a public task private or uncontaminated. No fixture,
reference answer or test code is fed back as a repair instruction.

## 中文说明

这是单独启动的“试验场”：同一批题分别交给两个 Telos 版本，继续由当前大总管
检查代码、考试、要求修复和验收。日常 MCP 工具、自动回传和正式任务流程保持原样。

实验只使用复制的运行时和独立任务目录；不切换全局模型，不覆盖安装，不自动合并，
不把实验经验写入日常经验库。`init`、`next`、`report` 不调用模型。开始某轮必须显式
使用 `start --live`，随后由大总管发送工作并完成验收。可暂停；取消后不能恢复。

内置 20 题都是公开开发样例，包含修复、实现和代码走查。页面相关题只检查源码，
不代表真实页面交互或截图走查已验证。报告保留失败和缺失数据，统计执行者工作、
修复、考试的用量；大总管用量和实际金额不在统计范围。它用于建立可重复的对比，
不会凭少量题目的成绩宣布某个版本“更聪明”或“已经实现 RSI”。
