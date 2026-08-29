# SLO 与错误预算（医疗 Agent）

本文件定义服务的**服务等级目标（SLO）**与**错误预算**口径，是 `alert_rules.yml`
中告警阈值与 `grafana/dashboards/medical-agent.json` 面板的依据。
指标命名空间统一为 `medical_agent_`，由 `src/metrics.py` 暴露。

## 一、SLO 清单

| 编号 | 维度       | SLO 目标                              | 测量指标 / PromQL 思路                                              | 窗口    |
| ---- | ---------- | ------------------------------------- | ------------------------------------------------------------------ | ------- |
| SLO1 | 可用性     | 对话接口 5xx 占比 < 0.5%             | `1 - rate(http_requests{status=~"5.."})/rate(http_requests)`       | 滚动 30d |
| SLO2 | 延迟-体感  | 首字节（首 token）p95 < 2s           | `histogram_quantile(0.95, rate(chat_first_token_seconds_bucket))`  | 滚动 30d |
| SLO3 | 延迟-模型  | LLM 调用 p95 < 2s                    | `histogram_quantile(0.95, rate(llm_duration_seconds_bucket))`      | 滚动 30d |
| SLO4 | 运维-审批  | 待审批积压 ≤ 20 单                    | `approvals_pending`                                                | 瞬时    |
| SLO5 | 韧性       | 安全降级率 < 1%（fallback/对话）     | `rate(llm_fallbacks)/rate(chat_turns)`                             | 滚动 30d |

## 二、错误预算

错误预算 = (1 - SLO 目标) × 总请求数。以 SLO1 为例，30 天窗口内允许
`0.5% × 总对话轮次` 次 5xx。告警 `MedicalAgentHighErrorRate` 在 5xx 占比连续
5 分钟超过 0.5% 时触发——相当于快速消耗错误预算的早期信号，而非等到月末才复盘。

各 SLO 的错误预算消耗速率可在 Grafana 新增「错误预算燃烧率（burn rate）」面板，
用多窗口多燃速（MWMB）法做快速耗尽预警（本仓库面板未内置，按需扩展）。

## 三、告警分级

- **critical**：直接威胁安全或可用性的事件
  - `MedicalAgentHighErrorRate`（SLO1 破窗）
  - `MedicalAgentSafetyGateSpike`（Tier-0 硬闸命中突增，疑似攻击）
- **warning**：性能退化或依赖异常，需介入但不立即宕机
  - `MedicalAgentFirstTokenSLO` / `MedicalAgentLLMLatencySLO`（SLO2/3）
  - `MedicalAgentApprovalBacklog`（SLO4）
  - `MedicalAgentBreakerOpen` / `MedicalAgentChatTimeoutSpike` / `MedicalAgentLLMFallbackSpike`
- **info**：运维主动操作的可观测信号
  - `MedicalAgentKillSwitchActive`

## 四、数据可信度说明

- 延迟类直方图基于真实埋点（`CHAT_FIRST_TOKEN` / `LLM_DURATION` 等），
  由 `src/gateway.py`、`src/agents.py` 在请求/调用路径上打点。
- 成本类指标（`llm_tokens_total` / `llm_cost_usd_total`）为**估算**：
  fake / 本地模型不计费；真实 API 按 `config.LLM_PRICING` 公开报价估算，
  用于容量规划，不等同财务结算。真实 token 数优先取 langchain `usage_metadata`。
- 所有数字来自运行期实测，非静态估算；压测基线见 `bench/压测基线报告.html`。
