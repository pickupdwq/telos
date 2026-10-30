# 岗位与公司经验本 / Roles and shared lessons

Telos 3.12 adds reusable executor jobs and explicitly reviewed cross-repository
lessons. The original supervisor still chooses assignments and owns acceptance.
No automatic extra worker or model request is introduced.

**岗位怎么使用**

大总管先调用 `telos_roles`，查看设计师 `designer`、程序员 `developer`、测试人员
`tester`，以及本机自定义岗位。创建任务或项目子任务时传 `role_profile`；讨论参与者
同时保留 `role`，说明此次具体负责什么。小任务只安排需要的岗位。

```json
{
  "role_profile": "developer",
  "experience_tags": ["numeric-input", "python"]
}
```

这两个字段可以加入现有 `telos_open` 参数或项目 `tasks` 条目。原有任务目标、
私有问卷、检查、路径范围等仍需提供。岗位自带公开职责、方法、技能需求、交付要求
和审核重点；不会自动授予网络、文件或浏览器权限，也不代表能力已达到“资深”。

**经验怎么共享**

旧版每个仓库的笔记原样保留。A 项目修复并验收通过后，大总管判断哪些教训通用，
再通过 `telos_experience_publish` 发布摘要。项目子任务还需等待项目整体验收。

```json
{
  "experience_id": "<accepted source experience ID>",
  "lesson": {
    "problem": "连续点击保存产生重复数据。",
    "cause": "相同操作被当成多个独立请求处理。",
    "resolution": "根据业务要求处理重复请求，并验证连续点击及重试。",
    "applicability": "同一次操作只能产生一次业务结果的保存流程。",
    "limitations": "允许重复创建的业务不能直接照搬；具体实现仍需本项目验证。",
    "required_tags": ["duplicate-submit"],
    "roles": ["designer", "developer", "tester"]
  },
  "rationale": "已审核原修复证据，摘要不包含项目专属业务规定。"
}
```

例子只展示参数形状，不能替代实际证据。未确认的原因填写未知。原大总管身份、
源码与验收证据、项目验收状态都会检查，候选失败记录不能直接发布。

B 的新任务声明 `experience_tags=["duplicate-submit"]` 后可读取这条经验。
所有 `required_tags` 都匹配才入选；岗位帮助排序，不把经验锁在一个部门里。
仓库经验优先，项目与共享内容合计最多三条、6000 字符。摘要的适用条件与限制
不会被截断丢弃，`experience_refs` 记录实际注入的 ID 和版本/内容哈希。

原有 `telos_experience_search(repo=...)` 没有 `tags` 时仍只搜本仓库。
传 `tags` 可补充共享摘要，`include_shared=false` 可只看项目经验。
跨项目返回值不包含源仓库路径、任务正文或私有验收材料。

这本公司经验本保存在同一 Telos 状态目录中，默认是
`~/.local/state/telos/experience/shared/`。换执行者或模型不会清空它。
本版本不做跨电脑同步，也不向正在工作的任务实时推送新经验。

**配置与兼容**

运行时 `config.json` 可以添加以下字段，升级会保留原配置和模型偏好：

```json
{
  "roles_enabled": true,
  "shared_experience_enabled": true,
  "role_profiles": {
    "developer": {
      "version": 2,
      "executor_profile": "bailian_deepseek"
    }
  }
}
```

内置岗位可以局部覆盖；自定义岗位需提供 `name`、正整数 `version`、
`responsibilities`、`workflow`、`skills`、`deliverables`、`review_focus`，
后五项为有界字符串数组。`executor_profile` 可选，未配置时沿用原执行者默认。
显式指定模型/provider/profile 优先于岗位默认。修改岗位时递增版本。

岗位内容和模型绑定在创建时冻结，排队期间也不会改变。旧任务不必补岗位字段，
旧经验不重写、不自动升级为共享经验。关闭 `roles_enabled` 可继续按旧方式派工；
关闭 `shared_experience_enabled` 停止共享发布与读取；`experience_enabled=false`
仍会关闭所有自动经验注入。显式查询经验用于人工审查，不受自动注入开关影响。

`telos_experience_forget` 可禁用项目或共享记录。源证据失效或被禁用后，不再支持
派生共享经验；若同一摘要有另一份有效审核来源，仍可使用。重复发布不会恢复
禁用条目。历史任务中的快照保留，便于查清当时参考了什么。

**English summary**

Use `telos_roles` and `role_profile` for frozen public job descriptions. Built-ins
are designer/developer/tester; explicit model choices override role defaults.
Publish only evidence-backed generalizations with `telos_experience_publish`
after task and, if applicable, project acceptance. Other repositories receive
reviewed summaries only when all applicability tags match. Original records,
private checks, ownership, budgets, concurrency and grading gates remain intact.
Shared entries deduplicate identical normalized lessons, retain reviewed source
receipts, and stop being retrieved when all supporting sources become invalid.
The runtime does not train models, synchronize machines, or update running tasks.
