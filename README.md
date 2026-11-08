# herder-team-loop-cli

通过 Herdr 运行可按 workspace 配置角色的 agent 团队，由统一 CLI 管理需求、派工、证据、Bug 分诊和有界修复循环。

- CLI 命令名：`herdr-team-loop-cli`。
- GitHub 仓库名：`herder-team-loop-cli`，按项目发起者指定的拼写保留。
- 当前阶段：**设计评审，尚未实现 CLI**。本文及设计文档中的新命令均为拟议接口，暂不可执行。

## 目标体验

在 Herdr 中向任意 agent 说明：

> 使用 herdr-team-loop-cli 初始化当前 workspace，列出本机可发现的 agent，让我指定团队角色。

角色按 workspace 独立设置。某个 workspace 可以由 Droid 协调，另一个可以由 Codex 或其他已适配的 agent 协调。发起初始化的 agent 不会自动成为协调者。

初始化后，显式创建或选择需求，启动该需求的一次执行。所有团队派工、状态、交接、Bug 决策和报告提交都经过 CLI；具体 agent 仍负责分析、开发、审查及测试。

## 评审入口

[需求、架构与使用流程设计](docs/design.md) 包含：

1. workspace → requirement → run 的隔离和定位规则。
2. agent 被动发现、初始化和角色配置。
3. SQLite、Markdown 和 JSON 的职责及统一存储布局。
4. CLI 核心、Herdr 适配层和 agent 的边界。
5. Bug 分诊、用户决策、修复、审查、SC 版本确认与复测。
6. 派工恢复、并发约束、迭代边界和验收条件。

## 开发边界

先评审设计，再开始实现。现有 `herdr-team-loop` Skill 和已安装的模板保持原状，后续通过显式迁移接入 CLI；不会让新旧两套状态同时调度同一个任务。

本项目不保存业务 workspace 的实际运行数据、账号凭据或测试证据，不自动调用厂商模型 API，也不新增遥测。
