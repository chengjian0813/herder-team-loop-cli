---
name: herdr-team-loop-cli
description: 使用 herdr-team-loop-cli 初始化 workspace 团队、创建需求、推进开发或测试循环，并处理 Bug 决策、报告验收与交接。适用于用户明确要求该 CLI 团队协作的任务。
---

# Herdr team loop CLI

团队状态、派工、决定、报告和交接统一调用 `herdr-team-loop-cli`。业务编辑、项目测试、浏览器和部署使用项目已有工具，遵守用户授权及 workspace 规则。

## 初始化与开始

- 执行 `init --workspace <实际目录> --json`。返回 `needs_configuration` 时展示角色问题；已有明确映射就直接提交 `--roles-file`。发起 agent 不自动成为协调者。
- 用 `requirement list/create/show` 明确归属。标题、分支和“最近任务”不能替代确认。正文记录目标、范围、验收和来源。
- `run start` 显式传入 requirement、mode、`--repo`、environment 和基线。测试默认 report-only；开发/修复需已有 code_fix 授权及来源，部署另需 deploy 授权。文件只能记录许可，不能创造许可。
- 读取 `protocol --json` 和相关 `--help` 获取接口；保存真实返回 ID，不绕过 CLI 改状态。

## 执行与验收

- 发起 agent 用 `run watch --run ID --after CURSOR --timeout 30 --json` 持续跟进。待决定项展示具体问题、选项、建议与影响，收到明确答复后 `decision resolve` 保存答复和来源。
- 协调者沿用启动消息的 `--data-dir`、`--actor`，先 `assignment claim/context`，再 `run next` 推进。worker 由 `run dispatch` 创建，不直接 hc 派工。
- worker 仅执行绑定角色与范围。用 `report submit --file` 回报；fail 列出实际失败的已登记 Bug，pass 列出**实际复测通过的 Bug ID**，保留未覆盖项。提交后不重做副作用。
- 协调者先 `report show` 核对实际证据，再 `report accept --rationale`。SC 测试前 `run environment --evidence-file` 记录实际部署、源码映射、配置、对象和时间。送达、进程完成、自报 pass 均非业务验收。

## Bug 和决定

- 根因不明：NEEDS_INVESTIGATION，`run dispatch --investigate` 只读调查。
- 预期明确、根因有证据且已授权：AUTO_ELIGIBLE，限定 Bug 修复。
- 业务语义、兼容、范围或数据处理有实质取舍：NEEDS_DECISION，提供具体方案及推荐理由。解法数量不是判断依据。
- 独立 Bug 可继续，不得绕过共享资源冲突。仅无关输入变化时 `bug reassess` 保存适用性证据，复用仍有效的决定；实质方案变化重新 triage。历史 Bug 先核对 applicability。

## 中断与交接

- 投递未知先 `assignment reconcile/observe`，核对原终端；人工确认用 `confirm-delivery --evidence`。查不到终端不等于从未执行，不盲目重发。
- 仅原终端明确 `first_message.sent=false` 且解决对话后处于 `idle/done` 时，由主控或人工用 `assignment deliver --assignment ID` 补投原任务一次；响应不明禁止重试。
- 额度或工具故障先保存 checkpoint，必要时 `assignment terminate`；核对在途结果后 `run handoff --operations-checked`。终止进程不等于远端操作回滚。
- 主控失联先核对旧主控停止调度，再 `run takeover`，保留已有 worker。
- 补测/输入变更使用 `run resume --revalidate` 连同 checkpoint、操作核对和旧 owner 检查；不新建 run 绕过预算。
- 无后台 daemon 自动完成业务判断，协调会话中断后须恢复或接管。不将夹具 smoke 宣称为真实 agent 或 SC E2E 通过。
