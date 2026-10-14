# 模块接口约定 v0.1

本文是待实现的完整项目接口草案。现有 FR-04 API 保持兼容；FR-01～03 的端点尚未实现。合并实现前由受影响负责人在 PR 确认字段，不要求先整体重构现有代码。

## 共同输入与版本

分析请求包含 `code`、`filename`、`function_name`、`spec`、`spec_source`（`manual` 或 `comments`）。公共输入层负责上传解码、AST 范围检查、选择说明来源。复用 `fr04/materials.py` 的能力时先检查实际函数签名，不在四个模块分别实现不同的提取规则。

规划新增 `analysis_id`，后台保存规范化后的代码与实际说明快照，并生成 `code_hash`、`spec_hash`（UTF-8 内容的 SHA-256）。FR-02/03 按 `analysis_id` 读取已有分析，不接受浏览器任意拼造的评分基础。改变代码、函数或说明必须生成新版本；下游结果携带原版本，不能混展示。哈希规则以统一实现为准，迁移时保持现有 FR-04 历史记录可读。

## FR-01 输出

建议 `POST /api/analyses` 创建分析，`GET /api/analyses/{id}` 读取结果。返回状态、版本、规则集版本、模型/提示词版本及 `findings`。每条发现至少包含：

| 字段 | 约定 |
| --- | --- |
| id | 当前分析内唯一的发现编号 |
| source | `rule` 或 `ai` |
| rule_id | 规则发现必须有规则编号；AI 推测可为 null |
| category | `correctness`、`maintainability`、`robustness` |
| severity | `info`、`warning`、`error` |
| line_start / line_end | 1 起算、必须在源码范围内；无法定位允许同时为 null |
| message / evidence | 原因及可核对的代码或规则证据 |
| status | `rule_detected` 或 `needs_review`，AI 初始必须待复核 |

规则检测不等于证明业务逻辑错误；页面需要显示来源。AI 输出未通过校验不能转成正式发现。

## FR-02 输出与初始评分提案

建议 `POST /api/analyses/{id}/assessment`，返回评分规则版本、分项权重、每条扣分、总分、解释及解释状态。只给分析已完成的版本评分。评分与模型解释分开存储，模型解释失败不影响已计算分数。

供 M0 审核的 v1 公式：三个分项各从 100 分开始，权重分别为 correctness 0.5、maintainability 0.3、robustness 0.2；确定性规则发现按 error 20、warning 10、info 0 扣分，同一 `rule_id + line_start + line_end` 去重，分项最低为 0；总分为加权和四舍五入到 1 位小数。规则表固定每条规则所属分项和等级，不能直接采用模型给出的等级扣分。只有通过范围检查、规则扫描完成的输入才可评分。

这是待组内确认的工程约定，不是行业认证指标；实现时用独立手算用例锁定公式。没有规则发现可得 100，但应说明“在当前规则集下未扣分”，不代表正确或安全。AI 疑似发现单列；未来纳入评分必须制定复核状态与公式升级规则。

## FR-03 输出

建议 `POST /api/analyses/{id}/suggestions`，请求指定 `finding_ids`。每条结果包含 `finding_id`、`reason`、`before`、`after`、`line_start`、`line_end`、`limitations`、源码/说明哈希以及模型/提示词版本。

`before` 必须对应快照中的原片段。`after` 是待人工评审的建议；结构或语法可检查，但检查通过不能标成“已修复”。FR-03 不执行或写回建议。版本不一致时拒绝套用，页面提示重新分析。

## FR-04 现有端点与集成

| 端点 | 现有用途 |
| --- | --- |
| `POST /api/source/inspect` | 解码上传文件、解析函数和注释；不执行源码 |
| `POST /api/generations` | 从代码签名及选定说明生成用例并保存新任务 |
| `GET /api/tasks` | 任务历史 |
| `GET /api/tasks/{id}` | 查看任务报告 |
| `POST /api/tasks/{id}/run` | 提交 `confirmed: true` 与 `case_version`，执行确认版本 |

现有生成参数包括 `code`、`spec`、`spec_source`、`filename`、`function_name`、`target_count`；准确请求/响应以 `app.py` 和 `tests/test_app.py` 为准。集成新增可选 `analysis_id` 时应校验其快照与生成输入一致，并保留原来独立使用 FR-04 的能力。完整生成请求不要直接改成 FR-01 端点，避免破坏历史功能。

## 失败与调试约定

新增模块统一返回可读错误与稳定 `error_code`；参数/范围错误用 400，任务不存在 404，输入版本冲突 409，模型上游失败 502，模型超时 504。执行状态与用例通过/失败分开：正常执行得到断言失败不是服务器故障。这里的错误码约定针对新增模块，不能误认为现有所有端点已经遵循。

模型失败不能替换成未标注的固定数据。日志不写 Authorization 或 API 密钥。用于评分、建议和测试的每份结果都要能回溯到同一份源代码及说明；所有状态都必须有失败出口，不能无限等待。
