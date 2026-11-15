"""探针结果模型、JSON 证据落盘与 Markdown 报告渲染。

- 结果文件是「时点证据」：带运行时间戳，不承诺代表上游当前状态；
- 凭据一律不落盘（环境只记布尔）；样本数据每 case 最多 3 根 bar。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from tapehub.domain.models import Bar

SAMPLE_BAR_LIMIT = 3


def _json_safe(value: Any) -> Any:
    """递归转 JSON 安全结构；未识别对象降级为字符串表示。

    ``dataclasses.asdict`` 会深拷贝全树，观测里一旦混入不可拷贝对象
    （如 SDK 内部句柄/asyncio Future）会在写证据时崩溃，丢掉整轮运行——
    因此这里只做浅层遍历 + 兜底字符串化，证据绝不因序列化失败而丢失。
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dt.datetime):
        return value.isoformat()
    # dataclass 保持结构化展开（与旧 asdict 行为一致，如 SeriesCompare）
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_safe({f.name: getattr(value, f.name) for f in dataclasses.fields(value)})
    return str(value)


@dataclass(slots=True)
class CaseResult:
    """单条探针 case 的执行结果。"""

    case_id: str
    kind: str
    verdict: str  # CaseVerdict.value
    reason: str
    dims: dict[str, str] = field(default_factory=dict)
    observation: dict[str, Any] = field(default_factory=dict)
    sample_bars: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: float | None = None


@dataclass(slots=True)
class RunResult:
    """一个 Provider 一次探针运行的完整结果。"""

    provider: str
    started_at: dt.datetime
    finished_at: dt.datetime
    environment: dict[str, Any] = field(default_factory=dict)
    call_budget: dict[str, int] = field(default_factory=dict)
    file_skip_reason: str | None = None
    notes: str = ""
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def has_failed(self) -> bool:
        return any(case.verdict == "failed" for case in self.cases)

    def verdict_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self.cases:
            counts[case.verdict] = counts.get(case.verdict, 0) + 1
        return counts


def sample_bars_of(bars: list[Bar], limit: int = SAMPLE_BAR_LIMIT) -> list[dict[str, Any]]:
    """样本 bar（首部最多 3 根），JSON 安全，供人工核验。"""
    return [json.loads(bar.model_dump_json()) for bar in bars[:limit]]


def to_json_dict(run: RunResult) -> dict[str, Any]:
    """浅层提取 + ``_json_safe`` 消毒；不做深拷贝，不可序列化对象降级为字符串。"""
    return _json_safe({f.name: getattr(run, f.name) for f in fields(run)})


def write_results(run: RunResult, results_dir: Path) -> tuple[Path, Path]:
    """写入 JSON 证据与 Markdown 报告，返回 (json_path, md_path)。

    ``default=str`` 是最后防线：万一观测里混入未转换的 datetime 等对象，
    宁可降级为字符串表示，也绝不让整轮探针的证据丢失。
    """
    provider_dir = results_dir / run.provider
    provider_dir.mkdir(parents=True, exist_ok=True)
    # 文件名时间戳按北京时间（+0800）标注；中国无夏令时，偏移恒定
    stamp = run.started_at.strftime("%Y%m%dT%H%M%S+0800")
    json_path = provider_dir / f"{stamp}.json"
    md_path = results_dir / f"report-{run.provider}-{stamp}.md"
    json_path.write_text(
        json.dumps(to_json_dict(run), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(run), encoding="utf-8")
    return json_path, md_path


def _phase_summary(probes: Any) -> str:
    """history 探测记录的阶段摘要（如 ``era×3 bisect×4 verify×1``）。

    旧结果文件的 probes 条目没有 ``phase`` 键（历史证据），缺失时显示 ``-``。
    """
    if not isinstance(probes, list):
        return "-"
    counts: dict[str, int] = {}
    for entry in probes:
        phase = entry.get("phase") if isinstance(entry, dict) else None
        if phase:
            counts[phase] = counts.get(phase, 0) + 1
    return " ".join(f"{name}×{count}" for name, count in counts.items()) or "-"


def render_markdown(run: RunResult) -> str:
    """人读汇总：已确认 / 未通过 / suspect+observe / history 回填建议 / skipped。"""
    lines: list[str] = []
    started = run.started_at.isoformat()
    lines.append(f"# tapehub Provider 探针报告 — {run.provider}")
    lines.append("")
    lines.append(f"- 运行时间（北京时间 +08:00）：{started}（时点证据，不代表上游当前状态）")
    lines.append(f"- 调用预算：{run.call_budget.get('used', 0)} / {run.call_budget.get('max', 0)}")
    env = run.environment
    lines.append(f"- 可选依赖安装：{env.get('extras', {}) or '（无要求）'}")
    lines.append(f"- 环境变量凭据已配置：{env.get('env', {}) or '（无要求）'}")
    if run.file_skip_reason:
        lines.append(f"- **整个文件被跳过**：{run.file_skip_reason}")
    if run.notes:
        lines.append(f"- 备注：{run.notes}")
    lines.append(f"- 判定统计：{run.verdict_counts()}")
    lines.append("")

    confirmed = [c for c in run.cases if c.verdict == "confirmed"]
    failed = [c for c in run.cases if c.verdict == "failed"]
    flagged = [c for c in run.cases if c.verdict in ("suspect", "observe")]
    skipped = [c for c in run.cases if c.verdict == "skipped"]
    histories = [c for c in run.cases if c.kind == "history_depth"]
    # 快照可用性单列小节（观测字段与 bars 不同口径），bars 表只收 K 线 case
    quote_confirmed = [c for c in confirmed if c.kind == "quote_availability"]
    bars_confirmed = [c for c in confirmed if c.kind != "quote_availability"]

    lines.append("## 已确认能力（confirmed）")
    lines.append("")
    if bars_confirmed:
        lines.append("| case | count | 首根 | 末根 | null 率>0 | 时延(ms) |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for case in bars_confirmed:
            obs = case.observation
            nulls = {
                k: v for k, v in (obs.get("null_rates") or {}).items() if v and v > 0
            }
            lines.append(
                f"| {case.case_id} | {obs.get('count')} | {obs.get('first_timestamp')} "
                f"| {obs.get('last_timestamp')} | {nulls or '—'} | {case.duration_ms} |"
            )
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("## 快照可用性（quotes，confirmed）")
    lines.append("")
    if quote_confirmed:
        lines.append("| case | last_price | bid1/ask1 | volume | amount | 时延(ms) |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for case in quote_confirmed:
            obs = case.observation
            lines.append(
                f"| {case.case_id} | {obs.get('last_price')} "
                f"| {obs.get('bid_price1')}/{obs.get('ask_price1')} "
                f"| {obs.get('volume')} | {obs.get('amount')} | {case.duration_ms} |"
            )
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("## 声明未通过（failed）")
    lines.append("")
    if failed:
        lines.append("| case | 原因 |")
        lines.append("| --- | --- |")
        for case in failed:
            lines.append(f"| {case.case_id} | {case.reason} |")
    else:
        lines.append("（无）")
    lines.append("")

    raws = [c for c in run.cases if c.kind == "raw"]
    conns = [c for c in run.cases if c.kind == "connection"]

    lines.append("## 连接探针（connection）")
    lines.append("")
    if conns:
        lines.append(
            "| case | 判定 | 实例 | created/首调/释放 | create_ms | first_call_ms | close_ms |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for case in conns:
            obs = case.observation
            t = obs.get("timings", [])
            creates = [r.get("create_ms") for r in t]
            firsts = [r.get("first_call_ms") for r in t]
            closes = [r.get("close_ms") for r in t]
            lines.append(
                f"| {case.case_id} | {case.verdict} | rounds×concurrent="
                f"{case.dims.get('rounds')}×{case.dims.get('concurrent')} "
                f"| {obs.get('created')}/{obs.get('first_call_ok')}/{obs.get('closed')} "
                f"| {creates} | {firsts} | {closes} |"
            )
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("## raw 探针（声明外探索 / 错误指纹）")
    lines.append("")
    if raws:
        lines.append("| case | 判定 | 首样本（截断） |")
        lines.append("| --- | --- | --- |")
        for case in raws:
            sample = case.observation.get("sample", [])
            first = str(sample[0])[:120] if sample else "—"
            lines.append(f"| {case.case_id} | {case.verdict} | {first} |")
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("## suspect / observe")
    lines.append("")
    if flagged:
        lines.append("| case | 判定 | 原因 / 证据 |")
        lines.append("| --- | --- | --- |")
        for case in flagged:
            extra = ""
            if case.kind == "history_depth":
                est = case.observation.get("earliest_estimate")
                extra = (
                    f"earliest={est} first_bar={case.observation.get('first_bar')} "
                    f"approx={case.observation.get('approx')}"
                )
            lines.append(f"| {case.case_id} | {case.verdict} | {case.reason} {extra} |")
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("## history.start 建议回填值（人工确认后再改 manifest）")
    lines.append("")
    if histories:
        lines.append(
            "| case | earliest_estimate | 首根 bar | approx | 粒度 | 探测次数 | complete | 阶段 |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for case in histories:
            obs = case.observation
            lines.append(
                f"| {case.case_id} | {obs.get('earliest_estimate')} | {obs.get('first_bar')} "
                f"| {obs.get('approx')} | {obs.get('snap')} | {obs.get('calls_used')} "
                f"| {obs.get('complete')} | {_phase_summary(obs.get('probes'))} |"
            )
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("## skipped")
    lines.append("")
    if skipped:
        lines.append("| case | 原因 |")
        lines.append("| --- | --- |")
        for case in skipped:
            lines.append(f"| {case.case_id} | {case.reason} |")
    else:
        lines.append("（无）")
    lines.append("")
    return "\n".join(lines)
