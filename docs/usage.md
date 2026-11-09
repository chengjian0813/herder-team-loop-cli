# v0.1 使用指南

示例中的路径、ID、授权和证据必须替换为实际值。`--data-dir`、`--workspace`、`--actor`、`--json` 可置于命令前后。机器调用使用 JSON。

## 响应和初始化

响应 envelope 包含 schema_version、status 和 data 或结构化 error。退出码：0 成功，3 需要配置/输入/决定，2 校验或执行错误，130 中断。已保存的待决定方案可能返回 3，先查看 data 和真实 ID，不重放创建。

`init` 不启动团队。非 TTY 或 JSON 模式缺角色返回 questions，人工 TTY 可交互选择。重复 init 不覆盖已有映射，修改通过 `roles set`：

```bash
herdr-team-loop-cli roles set --workspace /actual/workspace \
  --role tester --agent cursor --fallback kimi --json
```

`agents register --profile-file profile.json` 显式补充 agent：`{"id":"my-agent","command":["/absolute/agent","arg"]}`。command 是单行参数数组，不支持多行 shell 程序。Codex 自动带 `--no-daemon`。角色配置在 run 开始时冻结，修改只影响新 run。

CLI 按启动命令哈希生成 Herdr 薄模板，不覆盖用户模板；稳定角色和本次上下文由 CLI 生成。旧 Skill/模板不自动迁移。

## 需求与授权

`requirement create --title ... --scope-file ... [--source URL]` 创建需求；list/show/revise 查询或修订。同一 source 在同 workspace 内唯一，标题不是身份；无全局“当前需求”。

模式：develop 为计划→开发→审查→环境→测试→验收；e2e（别名 test）从环境确认开始；review 仅完成独立审查。默认基线 master，不存在时显式指定实际基线。

授权文件只记录已有许可：

```json
{"source":"本次用户明确答复的来源和范围","code_fix":true,"deploy":false}
```

```bash
herdr-team-loop-cli run start --workspace /actual/workspace \
  --requirement "$REQ_ID" --mode develop --repo /actual/repo \
  --repair-policy guarded --authorization-file authorization.json \
  --max-rounds 3 --active-minutes 120 --no-progress-limit 2 --json
```

report-only 禁止修 Bug，guarded 允许明确且已授权的方案，confirm-every-fix 每项方案需决定。`--resource NAME` 可重复，用于共享 SC 项目、账号和数据集。worker 串行；repo/测试资源锁保留到 run 完成或经核对停止。

CLI 默认启动配置的协调者。`--attach-terminal` 用于已存在且与配置匹配的终端。后台终端不承诺自动成为 App 可见 Tab；交互终端可用 `run attach`，JSON 模式仅返回连接信息。

## 调度和报告

发起 agent 保存 run ID 与 event cursor，`run watch --after CURSOR --timeout 30` 持续跟进。pending_decisions 返回待决定项；发起 agent 展示给用户，收到明确答复后 `decision resolve --decision ID --option ID --response-file answer.md --source ...`，再继续观察。

协调者使用启动消息绑定的 actor/data-dir。worker claim 后才执行；run next 返回阶段提示，派工用 run dispatch。协调者实际读取 report show 的证据后，才用 report accept --rationale；最终 run finish --summary 核对所有门禁。

`run plan --plan-file` 允许简短的目标范围说明，不强制额外设计文档。worker 的 context 返回冻结输入、需求正文、方案及报告模板：

```json
{
  "result":"pass",
  "summary":"实际结果",
  "coverage":["实际核对的用例"],
  "not_covered":[],
  "evidence":["实际命令、结果摘要或可核对路径"],
  "input_fingerprint":"复制 assignment 的 digest",
  "bug_ids":["实际复测通过的 Bug ID"]
}
```

result 支持 pass/fail/blocked；fail 的 bug_ids 是实际失败问题，pass 是实际验证通过的问题。被分配不等于已验证。外部环境/额度故障用 blocked 和 checkpoint，不伪造业务 Bug。

```bash
herdr-team-loop-cli --actor "$ASG_ID" report submit --assignment "$ASG_ID" --file report.json --json
herdr-team-loop-cli --actor "$PM_ID" report accept --report "$REPORT_ID" \
  --rationale "实际核对的证据、覆盖和结论" --json
```

## 环境证据

SC 等非 local 环境，`run environment --evidence-file` 至少提供：

```json
{
  "environment":"sc","input_fingerprint":"当前 run 的 digest",
  "verified_at":"实际核对时间","evidence":["实际平台查询及结果"],
  "project":"实际项目","config_version":"实际配置标识",
  "test_objects":["实际测试对象"],
  "applications":[{
    "name":"实际应用","version":"实际版本",
    "source_mapping":"证明版本包含本次代码的映射",
    "deployment_evidence":"实际部署结果"
  }],
  "deployment_performed":false
}
```

local 仅需 environment、input_fingerprint、verified_at、evidence。deployment_performed=true 需要部署授权。CLI 校验关联和完整性，不执行公司部署工具，也不凭文件声明证明远端事实。

## Bug 与决定

`bug create --run ID --bug-file bug.json` 登记 title/evidence/expected/actual；stable_key 可防止同 run 重复创建。协调者通过 bug triage 提交分类和方案：

```json
{
  "summary":"具体方案","evidence":"复现与根因证据",
  "authorization_reference":"已有授权引用",
  "impact":"业务影响、修改范围及依赖","validation_plan":"验证路径",
  "options":[
    {"id":"fix","description":"方案和代价","action":"fix"},
    {"id":"reject","description":"不实施此方案","action":"reject"}
  ],
  "recommended":"fix"
}
```

NEEDS_INVESTIGATION 需先调查；AUTO_ELIGIBLE 表示预期明确且已授权；NEEDS_DECISION 表示实质取舍。需要决定时 options/recommended 必填。action 支持 fix/reject/defer/accept-risk；拒绝和延期仍阻止当作修复通过，接受风险与验证通过分开记录。决定默认 requirement 范围，decision_scope=run 限定本轮。

```bash
herdr-team-loop-cli --actor "$PM_ID" bug triage --run "$RUN_ID" --bug "$BUG_ID" \
  --classification NEEDS_DECISION --proposal-file proposal.json --reason "具体取舍" --json
herdr-team-loop-cli --actor "$PM_ID" run dispatch --run "$RUN_ID" --bug "$BUG_ID" --json
```

未知根因用 run dispatch --investigate。仅无关输入变化且原方案仍完全适用时，`bug reassess --run ID --bug ID --evidence ...` 保存复核依据、保留原方案与决定；实质变化需新 triage。复核不刷新测试证据。新 run 用 bug applicability --applicable/--exclude --reason 核对历史问题；排除必需项还要真实用户答复。

## 恢复与停止

- 投递不明：assignment reconcile 找原终端，assignment observe 读取输出；人工核对已送达后 confirm-delivery --evidence。原 Herdr 终端的领取可自动证明接收；不自动重发业务任务。
- 已知未送达：原终端明确 `first_message.sent=false` 且已解决信任或权限对话、状态为 `idle/done` 后，由主控或人工执行 `assignment deliver --assignment ID`，向同一终端补投原任务一次；发送意图先持久化，响应不明则保持 UNKNOWN，禁止盲目重试。
- 未上报状态：只有明确 `status=null` 时，发起方核对原终端已进入空输入提示、信任/权限对话已结束，再用 `assignment deliver --assignment ID --ready-evidence-file PATH --ready-turn N` 记录证据并绑定当前 turn。不能带 assignment actor，不能用它绕过 working/blocked 或未知投递；证据不能代替真实观察。
- 交接：assignment block --reason --checkpoint-file 保存断点，必要时 assignment terminate --checkpoint-file 结束本工具 worker 并保持 BLOCKED。核对在途结果后 run handoff --checkpoint-file --operations-checked 使用候补。
- 无需交接：确认旧终端结束后 assignment retire --reason --checkpoint-file --operations-checked。查不到终端不等于已停止。
- 主控接管：run takeover --old-pm-stopped --checkpoint-file --reason 更新 epoch，保留已有 worker，旧主控不能继续推进。
- 补测/输入变化：run resume --revalidate --checkpoint-file --operations-checked --old-owner-stopped。未变输入复用有效审查/环境；代码变化则重新验证。方案适用性单独核对。
- 停工：run stop --reason 停止新派工；--terminate 结束本 run 管理的终端，人工附着终端和正在执行停止命令的协调终端除外。--operations-checked 记录副作用核对；未核对时保持 STOPPING，不能再推进 DONE。
- 等决定：没有 worker、没有可继续独立修复且有 pending decision 才可 run pause 停计活跃预算。绝对 deadline 不暂停，resume 不重置预算和轮次。
- 预算扩大：有明确授权后，由发起方（不带 `--actor`）执行 `run extend-budget --run ID --active-minutes TOTAL --reason TEXT --authorization-source TEXT`。TOTAL 是新的总分钟数；命令记录旧/新上限、已用时间、原因和真实授权来源，不重置已用时间、轮次或阶段，不修改绝对 deadline，也不自动派工。assignment 不能为自己扩大预算；来源文本本身不能创造授权。

## 存储和当前限制

workspace export 导出该 workspace 的记录与已核验正文；storage backup --destination NEW_DIR 用 SQLite backup API 创建一致性备份并复制引用 artifact。导出不自动恢复外部终端。

v0.1 不包含旧 loop-state 自动导入、跨机器运行恢复、后台通知、任意 DAG 或自动业务部署。同一 OS 用户下的 actor/决定来源是可审计协议，不是安全认证边界。
