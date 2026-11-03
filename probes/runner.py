"""探针执行器。

- availability / adjust_diff / history_depth / limit_truncation 走统一链路
  （adapter.fetch_bars），框架不含任何 Provider 专属逻辑；
- raw 探针按声明式配置反射调用 SDK（延迟导入），仅支持标量 kwargs 与
  ``$ENV`` 环境变量引用，凭据值不落任何结果文件；
- 全程遵守 pacing 节流与 ``max_calls`` 调用预算，预算耗尽即停止并如实记录。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import itertools
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from probes import analysis
from probes.analysis import BudgetExhausted
from probes.loader import ChainCase, ExpandedProbe, RawPlan, expand_probe
from probes.report import CaseResult, RunResult, sample_bars_of
from probes.schema import CaseVerdict, PacingSpec, ProbeFile, RawCaseSpec
from tapehub.domain.errors import TapehubError, UpstreamError
from tapehub.domain.models import Bar, BarBatch, BarQuery, DataVariant, Instrument
from tapehub.providers.registry import ProviderRecord, ProviderRegistry
from tapehub.settings import Settings

PROBE_PAGE_LIMIT = 5000
# limit_truncation 用超大 limit 抵消统一分页的切片干扰，
# 让上游/适配器自身的单次上限原样可见。
PROBE_MAX_QUERY_LIMIT = 100_000
STANDARD_FIELDS = ("open", "high", "low", "close", "volume", "amount")

_ENV_REF = re.compile(r"^\$([A-Z][A-Z0-9_]*)$")


class MissingEnvError(Exception):
    """raw 探针引用的环境变量未配置。"""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


@dataclass(slots=True)
class CallBudget:
    """跨整个探针文件共享的调用预算。"""

    max_calls: int
    used: int = 0

    def try_take(self) -> bool:
        if self.used >= self.max_calls:
            return False
        self.used += 1
        return True


@dataclass(slots=True)
class RunContext:
    """单 Provider 探针运行的共享状态。"""

    probe: ProbeFile
    record: ProviderRecord
    settings: Settings
    pacing: PacingSpec
    budget: CallBudget

    async def take_call(self) -> bool:
        """领取一次调用预算；除首次调用外按 pacing 等待节流。"""
        if not self.budget.try_take():
            return False
        if self.budget.used > 1:
            await asyncio.sleep(self.pacing.call_interval_s)
        return True


def check_requirements(probe: ProbeFile) -> tuple[dict[str, Any], str | None]:
    """检查可选依赖与环境变量；返回（环境布尔, 缺失原因或 None）。"""
    extras = {name: _module_available(name) for name in probe.requires.extras}
    env = {name: bool(os.environ.get(name)) for name in probe.requires.env}
    missing = [name for name, ok in extras.items() if not ok]
    missing += [name for name, ok in env.items() if not ok]
    environment: dict[str, Any] = {"extras": extras, "env": env}
    reason = f"requirements missing: {', '.join(missing)}" if missing else None
    return environment, reason


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


async def run_provider_probe(
    probe: ProbeFile,
    registry: ProviderRegistry,
    settings: Settings,
    *,
    execute: bool = True,
) -> RunResult:
    """执行一个 Provider 的完整探针文件。``execute=False`` 为 dry-run。"""
    started_at = datetime.now(UTC)
    environment, req_reason = check_requirements(probe)

    record = registry.get(probe.probe)
    file_skip = req_reason
    if record is None:
        file_skip = "provider is not registered"
    elif record.adapter is None:
        file_skip = f"adapter unavailable: {record.load_error or 'unknown load error'}"

    expanded: ExpandedProbe | None = None
    if record is not None:
        expanded = expand_probe(probe, record.manifest)

    cases: list[CaseResult] = []
    budget_used = 0
    if expanded is not None:
        if not execute:
            cases = [_dry_run_result(case) for case in expanded.chain_cases]
            cases += [_dry_run_result_raw(plan) for plan in expanded.raw_cases]
        elif file_skip is None:
            assert record is not None  # file_skip 为 None 时 record 必然存在
            budget = CallBudget(probe.pacing.max_calls)
            ctx = RunContext(
                probe=probe,
                record=record,
                settings=settings,
                pacing=probe.pacing,
                budget=budget,
            )
            for case in expanded.chain_cases:
                cases.append(await _run_chain_case(ctx, case))
            for plan in expanded.raw_cases:
                cases.append(await _run_raw_case(ctx, plan))
            budget_used = budget.used
        else:
            cases = [_skipped_result(case, file_skip) for case in expanded.chain_cases]
            cases += [_skipped_result_raw(plan, file_skip) for plan in expanded.raw_cases]

    return RunResult(
        provider=probe.probe,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        environment=environment,
        call_budget={"max": probe.pacing.max_calls, "used": budget_used},
        file_skip_reason=file_skip,
        notes=probe.notes,
        cases=cases,
    )


# ---------------------------------------------------------------------------
# chain 探针
# ---------------------------------------------------------------------------


def _dims_of(case: ChainCase) -> dict[str, str]:
    return {
        "asset_type": case.asset_type.value,
        "interval": case.interval.code,
        "adjust": case.adjust.value,
        "variant": case.variant.value,
        "instrument": case.instrument,
    }


def _build_query(
    ctx: RunContext,
    case: ChainCase,
    start: Any,
    end: Any,
    *,
    limit: int,
    variant: DataVariant | None = None,
    adjust: Any = None,
) -> BarQuery:
    """构造统一查询；``variant`` / ``adjust`` 覆盖 case 默认值（一致性、复权对比探针使用）。"""
    return BarQuery(
        provider=ctx.record.name,
        asset_type=case.asset_type,
        instrument=Instrument.parse(case.instrument),
        interval=case.interval,
        start=start,
        end=end,
        adjust=adjust if adjust is not None else case.adjust,
        variant=variant if variant is not None else case.variant,
        offset=0,
        limit=limit,
    )


def _make_fetcher(
    ctx: RunContext,
    case: ChainCase,
    *,
    adjust: Any = None,
    variant: DataVariant | None = None,
) -> analysis.WindowFetch:
    """分析探针共用的窗口查询闭包（含预算与节流，异常原样上抛）。

    ``adjust`` / ``variant`` 覆盖 case 维度：adjust_diff 用它分别抓取
    baseline 与 compared 两套序列。
    """

    async def fetch(start: datetime, end: datetime) -> list[Bar]:
        if not await ctx.take_call():
            raise BudgetExhausted()
        query = _build_query(
            ctx, case, start, end, limit=PROBE_PAGE_LIMIT, variant=variant, adjust=adjust
        )
        try:
            batch = await asyncio.wait_for(
                ctx.record.adapter.fetch_bars(query), ctx.pacing.timeout_s
            )
        except (TapehubError, BudgetExhausted):
            raise
        except Exception as exc:  # noqa: BLE001 - 未识别异常折算为 UpstreamError
            # 探针直连适配器，绕过了 BarQueryService 的统一包装；
            # SDK 未识别异常（如 akshare Push2RateLimitError）在此折算，
            # 保证分析层只需处理 TapehubError / BudgetExhausted。
            raise UpstreamError(
                f"unexpected SDK error: {type(exc).__name__}: {exc}"[:500],
                details={"provider": ctx.record.name},
            ) from exc
        return batch.bars

    return fetch


def _skip_budget() -> CaseResult:
    return CaseResult(
        case_id="", kind="", verdict=CaseVerdict.SKIPPED.value, reason="call budget exhausted"
    )


async def _fetch_query(ctx: RunContext, query: BarQuery) -> tuple[BarBatch | None, Any]:
    """单次链路调用（预算由调用方领取）：返回 (BarBatch, None) 或 (None, 错误描述)。"""
    try:
        return (
            await asyncio.wait_for(ctx.record.adapter.fetch_bars(query), ctx.pacing.timeout_s),
            None,
        )
    except TapehubError as exc:
        return None, {
            "error_code": exc.code.value,
            "error_type": type(exc).__name__,
            "error_text": exc.message,
        }
    except TimeoutError:
        return None, {
            "error_code": "TIMEOUT",
            "error_type": "TimeoutError",
            "error_text": "adapter call timed out",
        }
    except Exception as exc:  # noqa: BLE001 - 探针必须记录未知异常而不是崩溃
        return None, {
            "error_code": "UNEXPECTED",
            "error_type": type(exc).__name__,
            "error_text": str(exc)[:500],
        }


async def _fetch_safe(
    ctx: RunContext,
    case: ChainCase,
    start: datetime,
    end: datetime,
    *,
    limit: int,
    variant: DataVariant | None = None,
) -> tuple[BarBatch | None, Any]:
    """领取预算后单次链路调用（variant 可覆盖，供一致性探针使用）。"""
    if not await ctx.take_call():
        return None, BudgetExhausted()
    query = _build_query(ctx, case, start, end, limit=limit, variant=variant)
    return await _fetch_query(ctx, query)


async def _run_chain_case(ctx: RunContext, case: ChainCase) -> CaseResult:
    base = CaseResult(
        case_id=case.case_id, kind=case.kind, verdict=CaseVerdict.SKIPPED.value, reason="",
        dims=_dims_of(case),
    )
    if case.skip_reason:
        base.reason = case.skip_reason
        return base
    if case.kind == "availability":
        return await _run_availability(ctx, case, base)
    if case.kind == "adjust_diff":
        return await _run_adjust_diff(ctx, case, base)
    if case.kind == "history_depth":
        return await _run_history_depth(ctx, case, base)
    if case.kind == "limit_truncation":
        return await _run_limit_truncation(ctx, case, base)
    if case.kind == "variant_consistency":
        return await _run_variant_consistency(ctx, case, base)
    base.reason = f"unknown case kind: {case.kind}"
    return base


def _failed_from(base: CaseResult, error: Any, duration_ms: float | None = None) -> CaseResult:
    base.verdict = CaseVerdict.FAILED.value
    base.observation = {"error": error}
    base.duration_ms = duration_ms
    code = error.get("error_code") if isinstance(error, dict) else error
    base.reason = f"adapter call failed: {code}"
    return base


async def _run_availability(ctx: RunContext, case: ChainCase, base: CaseResult) -> CaseResult:
    started = time.perf_counter()
    batch, error = await _fetch_safe(
        ctx, case, case.window_start, case.window_end,
        limit=min(PROBE_PAGE_LIMIT, ctx.settings.max_limit),
    )
    base.duration_ms = round((time.perf_counter() - started) * 1000, 1)
    if error is not None:
        if isinstance(error, BudgetExhausted):
            base.reason = "call budget exhausted"
            return base
        return _failed_from(base, error, base.duration_ms)
    assert batch is not None
    bars = batch.bars
    rates = analysis.null_rates(bars, list(STANDARD_FIELDS))
    base.observation = {
        "count": len(bars),
        "first_timestamp": bars[0].timestamp.isoformat() if bars else None,
        "last_timestamp": bars[-1].timestamp.isoformat() if bars else None,
        "partial": batch.partial,
        "null_rates": rates,
        "page_limit": min(PROBE_PAGE_LIMIT, ctx.settings.max_limit),
        "declared_history_start": (
            case.declared_history_start.isoformat() if case.declared_history_start else None
        ),
    }
    base.sample_bars = sample_bars_of(bars)
    if not bars:
        base.verdict = CaseVerdict.FAILED.value
        base.reason = "0 bars in declared capability window"
        return base
    if case.min_bars is not None and len(bars) < case.min_bars:
        base.verdict = CaseVerdict.FAILED.value
        base.reason = f"count {len(bars)} < min_bars {case.min_bars}"
        return base
    all_null = [name for name in case.required_fields if rates.get(name) == 1.0]
    if all_null:
        base.verdict = CaseVerdict.FAILED.value
        base.reason = f"required fields null in all bars: {all_null}"
        return base
    base.verdict = CaseVerdict.CONFIRMED.value
    base.reason = "availability confirmed"
    return base


async def _run_adjust_diff(ctx: RunContext, case: ChainCase, base: CaseResult) -> CaseResult:
    assert case.compared_adjust is not None
    fetch_baseline = _make_fetcher(ctx, case, adjust=case.adjust)
    fetch_compared = _make_fetcher(ctx, case, adjust=case.compared_adjust)
    try:
        baseline = await fetch_baseline(case.window_start, case.window_end)
        compared = await fetch_compared(case.window_start, case.window_end)
    except TapehubError as exc:
        return _failed_from(base, {"error_code": exc.code.value, "error_text": exc.message})
    except BudgetExhausted:
        base.reason = "call budget exhausted"
        return base
    fields = case.compare_fields or ("close",)
    compares = [
        analysis.compare_price_series(baseline, compared, field_name) for field_name in fields
    ]
    base.observation = {
        "baseline_adjust": case.adjust.value,
        "compared_adjust": case.compared_adjust.value,
        "baseline_count": len(baseline),
        "compared_count": len(compared),
        "compares": [asdict(c) for c in compares],
    }
    if any(c.compared == 0 for c in compares):
        base.verdict = CaseVerdict.SUSPECT.value
        base.reason = "no overlapping non-null timestamps to compare"
        return base
    if all(c.identical for c in compares):
        base.verdict = CaseVerdict.SUSPECT.value
        base.reason = "adjust_no_op_suspect: adjusted series identical to unadjusted"
        return base
    base.verdict = CaseVerdict.CONFIRMED.value
    base.reason = "adjust takes effect (series differ)"
    return base


async def _run_history_depth(ctx: RunContext, case: ChainCase, base: CaseResult) -> CaseResult:
    fetch = _make_fetcher(ctx, case)
    try:
        result = await analysis.run_history_depth(
            fetch,
            earliest=case.window_start,
            latest=case.window_end,
            max_calls=case.expected_calls,
        )
    except TapehubError as exc:
        return _failed_from(base, {"error_code": exc.code.value, "error_text": exc.message})
    base.verdict = CaseVerdict.OBSERVE.value
    base.observation = {
        "earliest_estimate": (
            result.earliest_estimate.isoformat() if result.earliest_estimate else None
        ),
        "approx": result.approx,
        "granularity_days": result.granularity_days,
        "calls_used": result.calls_used,
        "complete": result.complete,
        "probes": result.probes,
    }
    if result.earliest_estimate is None:
        base.reason = "no data found in search range"
    elif not result.complete:
        base.reason = "search stopped early: call budget exhausted"
    else:
        base.reason = "earliest window estimate (informational)"
    return base


async def _run_limit_truncation(ctx: RunContext, case: ChainCase, base: CaseResult) -> CaseResult:
    started = time.perf_counter()
    batch, error = await _fetch_safe(
        ctx, case, case.window_start, case.window_end, limit=PROBE_MAX_QUERY_LIMIT
    )
    base.duration_ms = round((time.perf_counter() - started) * 1000, 1)
    if error is not None:
        if isinstance(error, BudgetExhausted):
            base.reason = "call budget exhausted"
            return base
        return _failed_from(base, error, base.duration_ms)
    assert batch is not None
    base.observation = {
        "count": len(batch.bars),
        "partial": batch.partial,
        "window_start": case.window_start.isoformat(),
        "window_end": case.window_end.isoformat(),
        "documented_max": case.documented_max,
    }
    base.sample_bars = sample_bars_of(batch.bars)
    if case.documented_max is None:
        base.verdict = CaseVerdict.OBSERVE.value
        base.reason = "no documented max to compare (informational)"
    elif len(batch.bars) == case.documented_max:
        base.verdict = CaseVerdict.CONFIRMED.value
        base.reason = f"measured count matches documented max {case.documented_max}"
    else:
        base.verdict = CaseVerdict.SUSPECT.value
        base.reason = (
            f"measured single-call count {len(batch.bars)} differs from "
            f"documented max {case.documented_max}"
        )
    return base


async def _run_variant_consistency(
    ctx: RunContext, case: ChainCase, base: CaseResult
) -> CaseResult:
    """跨 variant 一致性：同一查询在全部声明 variant 下的序列必须一致。

    任何不一致（数值差异、无重叠、预算中断导致样本不全）都按 suspect
    记录并在报告中给出分歧细节（差异幅度 + 首个分歧时间戳）。
    """
    assert case.variants, "variant_consistency case must carry declared variants"
    fields = case.compare_fields or ("close",)
    series: dict[str, list[Bar]] = {}
    incomplete = False
    for variant in case.variants:
        batch, error = await _fetch_safe(
            ctx,
            case,
            case.window_start,
            case.window_end,
            limit=min(PROBE_PAGE_LIMIT, ctx.settings.max_limit),
            variant=variant,
        )
        if isinstance(error, BudgetExhausted):
            incomplete = True
            break
        if error is not None:
            return _failed_from(base, error)
        assert batch is not None
        series[variant.value] = batch.bars

    if len(series) < 2:
        base.reason = (
            "call budget exhausted before two variants were fetched"
            if incomplete
            else "fewer than two variants returned data"
        )
        return base

    comparisons: list[dict[str, Any]] = []
    names = list(series)
    for left_name, right_name in itertools.combinations(names, 2):
        for field_name in fields:
            result = analysis.compare_price_series(
                series[left_name], series[right_name], field_name
            )
            comparisons.append(
                {
                    "a": left_name,
                    "b": right_name,
                    "field": result.field,
                    "compared": result.compared,
                    "identical": result.identical,
                    "max_abs_diff": result.max_abs_diff,
                    "first_divergence": (
                        result.first_divergence.isoformat() if result.first_divergence else None
                    ),
                }
            )
    per_variant = {
        name: {
            "count": len(bars),
            "first": bars[0].timestamp.isoformat() if bars else None,
            "last": bars[-1].timestamp.isoformat() if bars else None,
        }
        for name, bars in series.items()
    }
    base.observation = {
        "variants": per_variant,
        "fields": list(fields),
        "comparisons": comparisons,
        "incomplete": incomplete,
    }

    mismatched = [c for c in comparisons if not c["identical"]]
    no_overlap = [c for c in comparisons if c["compared"] == 0]
    if mismatched or no_overlap or incomplete:
        base.verdict = CaseVerdict.SUSPECT.value
        details: list[str] = []
        for item in mismatched:
            details.append(
                f"{item['a']} vs {item['b']} [{item['field']}] "
                f"max_diff={item['max_abs_diff']:.4g} first_at={item['first_divergence']}"
            )
        if no_overlap:
            details.append("some pairs have no overlapping non-null timestamps")
        if incomplete:
            details.append("budget exhausted before all variants were fetched")
        base.reason = "cross-variant inconsistency: " + "; ".join(details)
        return base

    base.verdict = CaseVerdict.CONFIRMED.value
    base.reason = "all declared variants agree on identical queries"
    return base


# ---------------------------------------------------------------------------
# raw 探针
# ---------------------------------------------------------------------------


def _interpolate(value: Any) -> Any:
    """递归解析 ``$ENV`` 引用；引用缺失抛 MissingEnvError（值绝不回传）。"""
    if isinstance(value, str):
        match = _ENV_REF.match(value)
        if match is not None:
            name = match.group(1)
            resolved = os.environ.get(name)
            if not resolved:
                raise MissingEnvError(name)
            return resolved
        return value
    if isinstance(value, dict):
        return {key: _interpolate(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_interpolate(item) for item in value]
    return value


def _resolve_attr(root: object, attr_path: str) -> Any:
    obj: Any = root
    for part in attr_path.split("."):
        obj = getattr(obj, part)
    return obj


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, default=str)
        return value
    except (TypeError, ValueError):
        return repr(value)[:500]


def _count_rows(result: Any) -> int | None:
    if isinstance(result, (list, tuple, dict)):
        return len(result)
    return None


def _sample_of(result: Any, limit: int = 3) -> list[Any]:
    if isinstance(result, (list, tuple)):
        return [_jsonable(item) for item in list(result)[:limit]]
    if result is None:
        return []
    return [_jsonable(result)]


def _raw_verdict(
    spec: RawCaseSpec, *, error: BaseException | None, rows: int | None
) -> tuple[CaseVerdict, str]:
    if error is not None:
        matched = (
            spec.expect.status == "error"
            and spec.expect.error_contains is not None
            and spec.expect.error_contains in str(error)
        )
    else:
        matched = spec.expect.status == "ok" and rows is not None and rows >= (
            spec.expect.min_rows or 0
        )
    if not spec.assert_:
        return CaseVerdict.OBSERVE, f"observe-only: expectation matched={matched}"
    if matched:
        return CaseVerdict.CONFIRMED, "expectation matched"
    return CaseVerdict.FAILED, (
        f"expectation not matched (status={spec.expect.status}, "
        f"error={type(error).__name__ if error else None}, rows={rows})"
    )


async def _run_raw_case(ctx: RunContext, plan: RawPlan) -> CaseResult:
    spec = plan.spec
    base = CaseResult(
        case_id=spec.id,
        kind="raw",
        verdict=CaseVerdict.SKIPPED.value,
        reason="",
        dims={"module": spec.module, "purpose": spec.purpose},
    )
    try:
        client_kwargs = _interpolate(spec.client.kwargs) if spec.client else {}
        call_kwargs = [_interpolate(call.kwargs) for call in spec.call]
    except MissingEnvError as exc:
        base.reason = f"env missing: {exc.name}"
        return base

    try:
        module = importlib.import_module(spec.module)
    except ModuleNotFoundError:
        base.reason = f"module not installed: {spec.module}"
        return base

    target: Any = None
    error: BaseException | None = None
    calls_made = 0
    if spec.client is not None:
        if not await ctx.take_call():
            base.reason = "call budget exhausted"
            return base
        calls_made += 1
        try:
            constructor = _resolve_attr(module, spec.client.attr)
            target = await asyncio.wait_for(
                asyncio.to_thread(constructor, **client_kwargs), ctx.pacing.timeout_s
            )
        except Exception as exc:  # noqa: BLE001 - 构造失败按期望匹配处理
            error = exc

    result: Any = target
    if error is None:
        for call, kwargs in zip(spec.call, call_kwargs, strict=True):
            if not await ctx.take_call():
                base.reason = "call budget exhausted"
                base.observation = {"calls_made": calls_made}
                return base
            calls_made += 1
            try:
                func = (
                    getattr(result, call.method)
                    if call.method
                    else _resolve_attr(module, call.module_func or "")
                )
                result = await asyncio.wait_for(
                    asyncio.to_thread(func, **kwargs), ctx.pacing.timeout_s
                )
            except Exception as exc:  # noqa: BLE001 - 探针必须记录异常而不是崩溃
                error = exc
                break

    rows = None if error is not None else _count_rows(result)
    verdict, reason = _raw_verdict(spec, error=error, rows=rows)
    base.verdict = verdict.value
    base.reason = reason
    base.observation = {
        "calls_made": calls_made,
        "rows": rows,
        "error": (
            {"type": type(error).__name__, "text": str(error)[:500]} if error is not None else None
        ),
        "sample": [] if error is not None else _sample_of(result),
    }
    return base


# ---------------------------------------------------------------------------
# dry-run / 跳过结果
# ---------------------------------------------------------------------------


def _dry_run_result(case: ChainCase) -> CaseResult:
    return CaseResult(
        case_id=case.case_id,
        kind=case.kind,
        verdict=CaseVerdict.SKIPPED.value,
        reason=f"dry-run (planned {case.expected_calls} calls)"
        + (f"; would skip: {case.skip_reason}" if case.skip_reason else ""),
        dims=_dims_of(case),
    )


def _dry_run_result_raw(plan: RawPlan) -> CaseResult:
    return CaseResult(
        case_id=plan.spec.id,
        kind="raw",
        verdict=CaseVerdict.SKIPPED.value,
        reason=f"dry-run (planned {plan.expected_calls} calls)",
        dims={"module": plan.spec.module, "purpose": plan.spec.purpose},
    )


def _skipped_result(case: ChainCase, reason: str) -> CaseResult:
    return CaseResult(
        case_id=case.case_id,
        kind=case.kind,
        verdict=CaseVerdict.SKIPPED.value,
        reason=reason,
        dims=_dims_of(case),
    )


def _skipped_result_raw(plan: RawPlan, reason: str) -> CaseResult:
    return CaseResult(
        case_id=plan.spec.id,
        kind="raw",
        verdict=CaseVerdict.SKIPPED.value,
        reason=reason,
        dims={"module": plan.spec.module},
    )
