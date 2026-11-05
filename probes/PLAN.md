# Provider 数据探针实施方案（PLAN）

状态：已批准，实施中。本文件是探针子系统的设计依据；实现完成后由 `probes/README.md`
承接运行规范，本文件保留设计动机与决策记录。

## 一、目标与定位

为 6 个内置 Provider 建立一套**离线诊断工具**，用真实上游调用实证核实能力目录中的
每一个声明（以及声明之外的边界），把 README 中大量「未核实 / 保持 null / 待核实」的
诚实边界变成**有据可查的时点证据**。

核心原则（与现有架构对齐）：

- **探针不是服务运行时的一部分**：独立目录、显式命令运行，绝不进入 pytest/CI
  （测试仍然全部 Mock、零网络）；
- **声明内探针走统一链路**（`adapter.fetch_bars`），框架完全通用、零 Provider 专属代码；
  **声明外探索走声明式原始调用**（YAML 里写 SDK 调用，框架反射执行）；
- **能力目录仍是唯一事实来源**：探针不自动改 manifest，只产出报告，回填由人工确认后提交；
- **凭据纪律不破例**：只从环境变量注入，结果文件只记录「凭据是否已配置」（布尔），
  绝不记录值；行情数据只存摘要 + 最多 3 根样本 bar。

## 二、目录结构

```text
probes/
  PLAN.md                # 本文件（设计决策）
  README.md              # 探针规范文档（文件格式、类型语义、运行方式、结果解读）
  __init__.py
  schema.py              # 探针文件 pydantic 模型（extra="forbid"，未知键拒绝）
  loader.py              # 加载 cases/*.yaml，与 manifest 矩阵合并展开
  runner.py              # 执行器：chain 探针（经适配器）+ raw 探针（SDK 反射）
  analysis.py            # history-depth 逐年回溯+二分、adjust-diff 对比、null 率统计
  report.py              # JSON 证据文件 + Markdown 报告生成
  cli.py                 # python -m probes.cli ...
  cases/                 # 规范化探针文件，每 Provider 一个，与 BUILTIN_PROVIDERS 同名
    akshare.yaml / tushare.yaml / tickflow.yaml
    alphafeed.yaml / tqsdk.yaml / twelvedata.yaml
  results/               # 运行产出（.gitignore 整目录）
tests/unit/probes/       # 框架自身单测（全部离线：Fake Provider + 既有 stub_*.py）
```

配套改动：`pyproject.toml` 的 `tool.ruff.src` 增加 `"probes"`；`.gitignore` 增加
`probes/results/`；无新增依赖（pydantic/pyyaml 已有，CLI 用 argparse）。

## 三、探针文件规范（核心）

每个 `cases/<provider>.yaml` 结构统一，由 `schema.py` 强校验（维度枚举直接复用
domain 的 `AssetType` / `Adjust` / `Interval.parse`，不另造一套）：

```yaml
probe: tushare                     # 必须等于 BUILTIN_PROVIDERS 中的名称
version: 1                         # 探针文件格式版本

requires:                          # 运行前提：缺失时整文件 skip 并写入报告，不报错
  extras: [tushare]                # pyproject 可选依赖组
  env: [TAPEHUB_TUSHARE_TOKEN]     # 必需环境变量

pacing:                            # 节流与预算（探针消耗真实配额，默认保守）
  call_interval_s: 1.0             # 相邻上游调用最小间隔
  timeout_s: 30                    # 单次调用超时
  max_calls: 120                   # 本文件调用预算，超出即中止

targets:                           # 每个 asset_type 的基准标的 + 默认窗口
  stock:
    instrument: CN-SSE-600000
    window: {start: 2025-01-01, end: 2025-12-31}

chain:                             # ── 统一链路探针（经 adapter.fetch_bars）──
  include_declared: true           # 自动从 manifest 展开全部声明组合，不重复抄写
  skip:                            # 显式跳过必须给原因（进报告）
    - {case: contract-P1D-none-eastmoney, reason: 合约已到期摘牌}
  adjust_diff:                     # 复权生效性探针配置
    targets: [stock]
    window: {start: 2024-01-01, end: 2024-12-31}   # 选含分红除权日的窗口
    pairs: [[none, qfq], [none, hfq]]
  history_depth:
    targets: [stock]
    era_step_years: 5              # 先按 5 年档回溯，连续 3 档空则二分
    earliest: 1990-01-01
    max_calls: 24
  limit_truncation:
    targets: [contract]
    interval: P1D                  # 在最小声明周期上探测单次上限
    max_range: P1Y
    documented_max: 2000           # 可选：与文档上限对照

raw:                               # ── 原始 SDK 探针（声明外探索 / 错误指纹）──
  - id: fut-dce-suffix             # 文件内唯一；chain 类 case ID 由维度自动生成
    purpose: 探测大商所期货后缀是否可用（声明外）
    module: tushare
    client: {attr: pro_api, kwargs: {token: "$TAPEHUB_TUSHARE_TOKEN"}}
    call:
      - {method: fut_daily, kwargs: {ts_code: "M2409.DCE", start_date: "20240801", end_date: "20240831"}}
    expect: {status: ok, min_rows: 1}       # status: ok | error（error 可配子串匹配）
    assert: true                            # false 表示仅观察（observe），不判定

notes: 周线/月线复权是否生效未在文档核实，本探针以 adjust_diff 实证。
```

**规范化要点**：

| 规则 | 说明 |
| --- | --- |
| 文件名 = provider 名 | loader 只认 `BUILTIN_PROVIDERS` 同名文件，缺失即报告缺文件 |
| case ID 规范 | chain 自动生成 `{asset_type}-{interval}-{adjust}-{variant}`；raw 自定义但全局唯一 |
| 声明内不抄写 | `include_declared: true` 从 manifest 矩阵展开，manifest 改了探针自动跟 |
| skip 必须带 reason | 防止静默漏探 |
| 维度合法性 | interval 用 `Interval.parse`、adjust/asset_type 用枚举校验，非法即加载失败 |
| 环境引用 | `$TAPEHUB_*` 只允许出现在 raw 的 kwargs 中，运行时从环境注入，结果不落值 |
| 分析配置带 targets | adjust_diff / history_depth / limit_truncation 各自限定作用的 asset_type，控制调用预算 |

## 四、探针类型语义

| 类型 | 走哪条链路 | 做什么 | 产出 |
| --- | --- | --- | --- |
| **availability** 可用性 | chain | 对 manifest 每个声明组合发一次查询 | count、首尾时间戳、逐字段 null 率、`partial`、warnings、时延；对照 min_bars / 字段期望给出判定 |
| **history-depth** 历史深度 | chain | 5 年档回溯 + 二分搜索最早可用窗口 | 每能力的 `history.start` 候选值（回填 manifest 用，粒度受窗口宽度限制，报告标注 approx） |
| **adjust-diff** 复权生效性 | chain | 同窗口 `none` vs `qfq`/`hfq` 对比 close 序列 | 序列完全一致 → `adjust_no_op_suspect`（证据标记，报告醒目，不定罪） |
| **limit-truncation** 单次上限 | chain | 最小周期 + 超长窗口，数 bar 数 | 实测上限 vs 文档上限（如 tqsdk 8000/10000 之争） |
| **raw** 原始探测 | SDK 反射 | 声明外能力（如 td 45min/2h/4h/8h、tushare 其它期货所后缀）、错误码指纹 | ok/error + 匹配子串 + 样本行 |

判定枚举统一为：`confirmed` / `failed` / `suspect` / `skipped` / `observe`。

## 五、各 Provider 探针文件要点

| Provider | 探针必须回答的问题 |
| --- | --- |
| **akshare** | 三源（eastmoney/sina/tencent）同标的同日 close 一致性；全部 `history: null` 的实际深度；指数腾讯 `amount` 列语义复核；期货连续 `RB0` 历史起点。无凭据但需防站点限流 |
| **tushare** | 周线/月线 qfq/hfq 是否实际生效（adjust-diff，当前最紧迫的存疑点）；`.SHF` 之外期货所后缀（.DCE/.CZCE/.INE…）raw 探测以扩能力；`fut_daily` 2000 条上限实测；积分不足/限频错误指纹（observe） |
| **tickflow** | 10000 根上限实测；限频/无效 Key 错误指纹（raw，observe）；分钟权限 403 行为 |
| **alphafeed** | 同 tickflow；raw 探测 `10m` 周期确实缺失（声明缺失的实证） |
| **tqsdk** | `data_length` 上限 **8000 vs 10000** 实测裁决（limit-truncation）；`KQ.i@` 加权指数覆盖；实例启动时延（pacing 放宽） |
| **twelvedata** | 未声明周期 `45min/2h/4h/8h` raw 探测（可用则考虑扩 manifest）；crypto volume 缺失率；免费档限频指纹（pacing ≥ 8s） |

## 六、结果与报告规范

- `probes/results/<provider>/<北京时间戳>.json`：完整证据（case 列表 + 观测 + 判定 +
  环境布尔），是「时点证据」而非永久事实；
- `probes/results/report-<时间戳>.md`：人读汇总，四张表——**已确认能力 /
  声明未通过（红） / 声明外可用 / history.start 建议回填值**，外加 `suspect` 汇总与
  `skip` 原因清单；
- 样本数据每 case 最多保留 3 根 bar（核验用），凭据一律布尔化。

## 七、运行方式与安全

```bash
uv run python -m probes.cli list                              # 查看展开后的 case 清单（零网络）
uv run python -m probes.cli run --provider tushare --dry-run  # 打印计划调用，不发真实请求
uv run python -m probes.cli run --provider tushare --yes      # 真跑（横幅提示消耗配额）
```

凭据经 pydantic-settings 自动从 `.env` 读取（复用 `Settings`），探针内不出现任何密钥字面量。

## 八、框架自身的测试（全部离线）

`tests/unit/probes/`：schema 校验（未知键/非法维度/重复 ID 拒绝）、manifest 矩阵展开、
adjust-diff 与 history-depth 算法（用合成 fetch 函数构造已知数据）、raw 执行器
（动态注入假 SDK 模块）、报告生成。CI 不变：`uv run pytest` 依旧零网络。

## 九、实施步骤

1. 框架骨架：`schema.py` + `loader.py`（manifest 展开）+ 对应单测；
2. chain 执行器：`runner.py` + `analysis.py` 三类分析 + Fake 单测；
3. raw 执行器：声明式 SDK 反射 + stub 单测；
4. 六份 `cases/*.yaml` 按第五节要点编写；
5. `report.py` + `cli.py` + `probes/README.md`；ruff / .gitignore / AGENTS.md 收尾；
6. 人工实跑与回填（不在本方案代码范围内）：真实凭据跑一轮 → 报告确认 → 人工修订
   manifest（history.start、tushare 周月线复权、twelvedata 周期、tqsdk 上限）与 README，
   单独提交。

## 十、变更记录

- **v2（实施中修订）**：应用户要求，分析探针不再与「声明顺序第一组维度」绑定——
  adjust_diff 按 (interval × variant × pair)、history_depth / limit_truncation 按
  (interval × variant) 在声明矩阵上全量展开（`intervals` 字段可显式收敛 interval
  以控制预算，variant 维度永不收敛）；新增 `variant_consistency` 探针类型，对同一
  (interval, adjust) 在跨能力聚合的全部声明 variant 下重放同一查询并两两对比，
  不一致判 suspect 并记录 max_abs_diff 与 first_divergence。
  实施过程中由离线测试发现并修复两个执行器缺陷：adjust_diff 对比序列未使用
  compared_adjust（实际抓了两次 baseline）；分析 fetch 闭包未兜底未识别 SDK 异常
  （直连适配器绕过了 BarQueryService 的统一包装，已在 fetcher 折算为 UpstreamError）。
  history_depth 的 `max_calls` 语义改为**每条 case** 的预算。

主要风险：上游配额消耗（靠 pacing + max_calls 预算控制）、结果时效性（报告带时间戳，
明示为时点证据）、tqsdk 实例慢（单独放宽节流）、东财 push2 对单 IP 频率敏感
（akshare 实测 2s 节流即触发限流，已放宽至 5s 并记录于 cases/akshare.yaml）。
