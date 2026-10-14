# 最小接口约定

只约定够大家并行开发的字段，不先搭复杂架构。FR-01～03 以下接口是待开发约定；FR-04 已有实现保持不变。

## 共用输入

四个功能使用页面上的同一份 `code`（代码）、`filename`（文件名）、`function_name`（函数名）、`spec`（实际说明）。`spec_source` 为 `manual` 或 `comments`。复用现有上传和注释提取能力，用户改代码或说明时清空旧分析结果，重新生成，避免混用。

## FR-01：问题列表

建议 `POST /api/analyze`，接收共用输入，返回 `findings` 数组。每条至少有：

```json
{"id":"F1","line":5,"type":"correctness","severity":"warning","source":"ai","message":"免邮边界可能与说明不一致"}
```

`line` 从 1 开始，无法定位可为 null；`source` 是 `rule` 或 `ai`。AI 发现标为待核查，不把推测说成确定事实。FR-01 负责人先提供一个这样的固定返回样例给 FR-02/03 使用。

## FR-02：评分

建议 `POST /api/assess`，接收 `findings`，返回 `score`、`deductions`（扣分条目）和 `explanation`（说明）。

先采用简单公式：100 分起，只对去重后的规则发现扣分，error 扣 20、warning 扣 10、info 不扣，最低 0 分。AI 疑似问题只提示，不直接扣分；AI 解释分数，不生成分数。规则级别由固定规则确定。没有完成分析时显示“未评估”，不能默认满分。100 分只表示当前规则未扣分。

## FR-03：建议

建议 `POST /api/suggest`，接收共用输入和选中的 `finding`，返回 `finding_id`、`reason`、`before`、`after`。页面显示前后代码和理由即可，不自动覆盖源代码。核对 `before` 确实来自当前代码。

## FR-04：沿用已有接口

- `POST /api/source/inspect`：上传解析与注释提取。
- `POST /api/generations`：根据代码与所选说明生成用例。
- `GET /api/tasks/{id}`：查看结果。
- `POST /api/tasks/{id}/run`：提交 `confirmed: true` 和 `case_version` 后实际执行。

准确参数参考 `app.py` 和 `tests/test_app.py`。先复用当前页面的数据，不为联调重写已有执行器。保留现有用例版本、确认和真实结果记录。

## 失败处理

参数错误、AI 超时或响应无法解析时，接口返回可读错误，页面结束等待并允许重试。不要把失败伪装成成功，不在日志中输出 API 密钥。接口字段有改动，在群里告诉相关同学即可，不需要专门的审批流程。
