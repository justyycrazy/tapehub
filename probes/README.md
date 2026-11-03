# probes — Provider 数据探针规范

用真实上游调用实证核实能力目录声明（及声明之外的边界），把 README 中
「未核实 / 保持 null / 待核实」的诚实边界变成**有据可查的时点证据**。

设计动机与决策记录见 [PLAN.md](PLAN.md)。**探针不是服务运行时的一部分，
也不进入 CI 测试**：`uv run pytest` 保持零网络、零真实凭据。

## 目录结构

```text
probes/
  PLAN.md                # 设计决策（批准时的方案全文）
  README.md              # 本文件：文件格式、类型语义、运行方式、结果解读
  schema.py / loader.py / runner.py / analysis.py / report.py / cli.py
  cases/                 # 探针文件：每 Provider 一个，与 BUILTIN_PROVIDERS 同名
  results/               # 运行产出（.gitignore 整目录，不入库）
tests/unit/probes/       # 框架自身离线测试
```

## 运行方式

```bash
uv run python -m probes.cli list                              # 展开 case 清单（零网络）
uv run python -m probes.cli run --provider tushare --dry-run  # 打印计划调用，不发请求
uv run python -m probes.cli run --provider tushare --yes      # 真实运行（消耗上游配额）
uv run python -m probes.cli run --yes                         # 全部 Provider
```

真实运行必须显式 `--yes`；退出码：0 正常（允许 skipped/observe）、
1 存在 failed、2 参数或前提问题。凭据经 `.env` / 环境变量（前缀 `TAPEHUB_`）
注入，运行前确认 `requires.env` 列出的变量已配置。

## 探针文件格式

文件名 = Provider 名（`cases/<provider>.yaml`）。所有模型 `extra="forbid"`，
未知键一律拒绝；维度取值复用 domain 枚举与 ISO 8601 Duration。

```yaml
probe: tushare                     # 必须等于 BUILTIN_PROVIDERS 中的名称
version: 1                         # 探针文件格式版本（当前 1）

requires:                          # 运行前提；缺失时整文件 skip 并写入报告
  extras: [tushare]                # SDK 模块名（同时是 pyproject 可选依赖组名）
  env: [TAPEHUB_TUSHARE_TOKEN]     # 必需环境变量

pacing:                            # 节流与预算（探针消耗真实配额，默认保守）
  call_interval_s: 1.5             # 相邻上游调用最小间隔
  timeout_s: 30                    # 单次调用超时
  max_calls: 120                   # 本文件调用预算，超出即中止

targets:                           # 每个 asset_type 的基准标的 + 默认窗口
  stock:
    instrument: CN-SSE-600000
    window: {start: 2025-01-01, end: 2025-12-31}
    min_bars: 100                  # 可选：count 低于该值判 failed
    required_fields: [close]       # 可选：字段全 null 判 failed

chain:                             # ── 统一链路探针（经 adapter.fetch_bars）──
  include_declared: true           # 从 manifest 自动展开全部声明组合
  skip:                            # 显式跳过必须给原因（进报告）
    - {case: contract-P1D-none-eastmoney, reason: 合约已到期摘牌}
  adjust_diff:                     # 复权生效性：同窗口对比价格序列
    targets: [stock]
    window: {start: 2024-01-01, end: 2024-12-31}
    pairs: [[none, qfq], [none, hfq]]
    fields: [close]
  history_depth:                   # 历史深度：纪元回溯 + 二分
    targets: [stock]
    earliest: 1991-01-01
    era_step_years: 5
    max_calls: 24
  limit_truncation:                # 单次上限：最小周期 + 超长窗口
    targets: [contract]
    interval: P1D
    max_range: P1Y
    documented_max: 2000           # 可选：不写则只观察

raw:                               # ── 声明外探索 / 错误指纹（SDK 反射）──
  - id: fut-dce-suffix
    purpose: 探测大商所后缀（声明外）
    module: tushare                # SDK 根模块
    client: {attr: pro_api, kwargs: {token: "$TAPEHUB_TUSHARE_TOKEN"}}
    call:                          # 首条作用于 client，后续条作用于上一条结果（链式）
      - {method: fut_daily, kwargs: {ts_code: "M2409.DCE", ...}}
    expect: {status: ok, min_rows: 1}   # 或 {status: error, error_contains: "..."}（空串=任意错误）
    assert: true                   # false = 仅观察（observe），不判定

notes: 本探针要回答的问题与已知存疑点。
```

### 规范化规则

| 规则 | 说明 |
| --- | --- |
| 文件名 = Provider 名 | loader 只认 `BUILTIN_PROVIDERS` 同名文件，缺失/多余即报错 |
| case ID | chain 自动生成 `{asset_type}-{interval}-{adjust}-{variant}`；分析类为 `adjust-diff-<at>-<a>-vs-<b>`、`history-depth-<at>`、`limit-<at>-<interval>`；raw 自定义 `^[A-Za-z][A-Za-z0-9._-]*$` 且全局唯一 |
| 声明内不抄写 | `include_declared: true` 从 manifest 矩阵展开，manifest 改动探针自动跟随 |
| skip 必须带 reason | 防止静默漏探 |
| 环境引用 | `$TAPEHUB_*` 只允许出现在 raw 的 kwargs；运行时注入，值绝不落盘 |
| 分析配置带 targets | adjust_diff / history_depth / limit_truncation 各自限定 asset_type，控制预算 |
| 分析探针维度 | 每个目标 asset_type 取「声明顺序第一组维度」（interval/adjust/variant）生成一条 case |

## 探针类型语义

| 类型 | 链路 | 做什么 | 判定 |
| --- | --- | --- | --- |
| availability | chain | 对每个声明组合查一次 | 0 bars / 低于 min_bars / required 字段全 null → failed；否则 confirmed |
| adjust_diff | chain | 同窗口 none vs qfq/hfq 对比 | 序列完全一致 → **suspect**（adjust_no_op_suspect）；有差异 → confirmed；无重叠 → suspect |
| history_depth | chain | 5 年档回溯 + 90 天窗口二分 | 恒为 observe；报告给出 `earliest_estimate`（**近似值**，粒度 = 窗口宽度） |
| limit_truncation | chain | 最小周期 + 超长窗口数 bar | 实测 = documented_max → confirmed；不等 → suspect；未设 → observe |
| raw | SDK 反射 | 声明外能力探测、错误指纹 | 期望匹配 → confirmed / 不匹配 → failed；`assert: false` → observe |

判定枚举：`confirmed` / `failed` / `suspect` / `skipped` / `observe`。

## 结果解读

- `results/<provider>/<时间戳>.json`：完整证据（case + 观测 + 判定 + 环境布尔）；
- `results/report-<provider>-<时间戳>.md`：人读汇总，含「已确认能力 /
  声明未通过 / suspect+observe / history.start 建议回填值 / skipped」五张表；
- 结果是**时点证据**，报告带时间戳，不代表上游当前状态；
- 样本数据每 case 最多 3 根 bar；凭据只记录「是否已配置」布尔。

**manifest 回填流程**：报告 → 人工确认 → 修订 manifest（`history.start`、
复权声明收敛、新增周期/后缀声明）与 README → 单独提交。探针永不自动改 manifest。

## 安全纪律

- Provider 凭据只经环境变量注入；探针文件与结果文件中禁止出现任何凭据字面量
  （错误指纹探针可使用明显的字面量假 Key，如 `probe-invalid-key-not-a-credential`）；
- pacing + max_calls 预算必须保守设置；探针消耗的是真实上游配额；
- 日志与结果不记录凭据值、完整行情响应。

## 新增 Provider 时

1. `cases/<name>.yaml` 按 Provider 要核实的问题编写（参考现有六份文件）；
2. 新增分析探针时先在 `tests/unit/probes/` 补离线测试（合成 fetch 闭包）；
3. 框架代码保持 Provider 无关：Provider 专属事实只允许出现在 `cases/*.yaml`。
