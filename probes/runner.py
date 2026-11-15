"""探针执行器。

- availability / quote_availability / adjust_diff / history_depth /
  limit_truncation 走统一链路（adapter.fetch_bars / adapter.fetch_quote），
  框架不含任何 Provider 专属逻辑；
- raw 探针按声明式配置反射调用 SDK（延迟导入）；kwargs 支持 ``$ENV``
  环境变量引用、``$ctor``（嵌套轻量对象构造，如认证对象）与
  ``$deadline_s``（相对秒数 → unix epoch）；凭据值不落任何结果文件；
- connection 探针表达「创建 → 首次取数 → 释放」的客户端生命周期计时
  与并发共存验证（同样纯声明式，无 Provider 专属代码）；
- 全程遵守 pacing 节流与 ``max_calls`` 调用预算，预算耗尽即停止并如实记录。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import itertools
import json
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from probes import analysis
from probes.analysis import BudgetExhausted
from probes.loader import ChainCase, ConnectionPlan, ExpandedProbe, RawPlan, expand_probe
from probes.report import CaseResult, RunResult, sample_bars_of
from probes.schema import CaseVerdict, CtorSpec, PacingSpec, ProbeFile, RawCaseSpec
from tapehub.domain.errors import TapehubError, UpstreamError
from tapehub.domain.models import (
    CN_TIMEZONE,
    Bar,
    BarBatch,
    BarQuery,
    DataVariant,
    Instrument,
    QuoteQuery,
)
from tapehub.providers.registry import ProviderRecord, ProviderRegistry
from tapehub.settings import Settings

PROBE_PAGE_LIMIT = 5000
# limit_truncation 用超大 limit 抵消统一分页的切片干扰，
# 让上游/适配器自身的单次上限原样可见。
PROBE_MAX_QUERY_LIMIT = 100_000
STANDARD_FIELDS = ("open", "high", "low", "close", "volume", "amount")

_ENV_REF = re.compile(r"^\$([A-Z][A-Z0-9_]*)$")

logger = logging.getLogger(__name__)

# 子步骤错误消息在日志行内的展示截断（证据文件仍按 500 截断原样保留）
_STEP_ERROR_TEXT_LIMIT = 200


def _log_step(kind: str, case_id: str, detail: str, *, level: int = logging.INFO) -> None:
    """case 内子步骤日志行：每次真实上游调用一行（列式布局与进度行对齐）。

    只含序号/方法名/行数/耗时/异常类型与截断消息，绝不打印 kwargs 值
    （raw 探针 kwargs 经 ``$ENV`` 注入凭据）；异常文本与证据 observation
    同口径（上游错误消息，非凭据）。
    """
    logger.log(level, f"  [step     ] {kind:<18} {case_id} {detail}")


def _error_detail(error: dict[str, Any]) -> str:
    """错误 observation 的单行摘要（子步骤行用）。"""
    text = str(error.get("error_text", ""))[:_STEP_ERROR_TEXT_LIMIT]
    return f"{error.get('error_type')} {error.get('error_code')}: {text}"


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


# ``.env`` 回退查找的懒加载缓存；测试可 monkeypatch 此变量注入受控值
_settings_env: Settings | None = None


def _lookup_env(name: str) -> str | None:
    """环境变量查找：进程环境优先，``TAPEHUB_*`` 回退到 Settings（含 ``.env``）。

    pydantic-settings 读取 ``.env`` 不会导出到进程环境，而探针凭据的本地
    注入路径是 ``.env``（见 probes/README.md），因此对 ``TAPEHUB_*`` 前缀的
    引用做一次 Settings 回退；值绝不写入结果文件。
    """
    resolved = os.environ.get(name)
    if resolved:
        return resolved
    global _settings_env
    if name.startswith("TAPEHUB_"):
        if _settings_env is None:
            try:
                _settings_env = Settings()
            except Exception:  # noqa: BLE001 - 本地 .env 非法时退化为空查找
                _settings_env = Settings(_env_file=None)
        value = getattr(_settings_env, name[len("TAPEHUB_"):].lower(), None)
        if value:
            return str(value)
    return None


def check_requirements(probe: ProbeFile) -> tuple[dict[str, Any], str | None]:
    """检查可选依赖与环境变量；返回（环境布尔, 缺失原因或 None）。"""
    extras = {name: _module_available(name) for name in probe.requires.extras}
    env = {name: bool(_lookup_env(name)) for name in probe.requires.env}
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


def _print_progress(phase: str, kind: str, case_id: str, detail: str = "") -> None:
    """实时进度输出：每条 case 开始/完成立即刷新（后台运行可逐条观察）。

    走 logging 控制台 handler（stdout），每次 emit 后自动 flush——stdout
    重定向到文件时同样实时可见；卡住的 case 通过「最后一条 RUN 行」
    直接定位。"""
    suffix = f" {detail}" if detail else ""
    logger.info(f"  [{phase:<9}] {kind:<18} {case_id}{suffix}")


def _progress_detail(result: CaseResult, budget: CallBudget) -> str:
    """完成行的附加信息：调用数、耗时与预算水位。"""
    calls = (result.observation or {}).get("calls_made", 0)
    seconds = (result.duration_ms or 0) / 1000
    return f"({calls} 调, {seconds:.1f}s, 预算 {budget.used}/{budget.max_calls})"


async def run_provider_probe(
    probe: ProbeFile,
    registry: ProviderRegistry,
    settings: Settings,
    *,
    execute: bool = True,
) -> RunResult:
    """执行一个 Provider 的完整探针文件。``execute=False`` 为 dry-run。"""
    started_at = datetime.now(CN_TIMEZONE)
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
            cases += [_dry_run_result_connection(plan) for plan in expanded.connection_cases]
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
                _print_progress("RUN", case.kind, case.case_id)
                result = await _run_chain_case(ctx, case)
                cases.append(result)
                _print_progress(
                    result.verdict, case.kind, case.case_id,
                    _progress_detail(result, budget),
                )
            for plan in expanded.raw_cases:
                _print_progress("RUN", "raw", plan.spec.id)
                result = await _run_raw_case(ctx, plan)
                cases.append(result)
                _print_progress(
                    result.verdict, "raw", plan.spec.id,
                    _progress_detail(result, budget),
                )
            for plan in expanded.connection_cases:
                _print_progress("RUN", "connection", plan.spec.id)
                result = await _run_connection_case(ctx, plan)
                cases.append(result)
                _print_progress(
                    result.verdict, "connection", plan.spec.id,
                    _progress_detail(result, budget),
                )
            budget_used = budget.used
        else:
            cases = [_skipped_result(case, file_skip) for case in expanded.chain_cases]
            cases += [_skipped_result_raw(plan, file_skip) for plan in expanded.raw_cases]
            cases += [
                _skipped_result_connection(plan, file_skip)
                for plan in expanded.connection_cases
            ]

    return RunResult(
        provider=probe.probe,
        started_at=started_at,
        finished_at=datetime.now(CN_TIMEZONE),
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
    dims = {
        "asset_type": case.asset_type.value,
        "interval": case.interval.code if case.interval else "",
        "adjust": case.adjust.value if case.adjust else "",
        "variant": case.variant.value,
        "instrument": case.instrument,
    }
    if case.interval is None:
        # quotes 无周期/复权维度（manifest 显式声明 null），dims 不携带空键
        del dims["interval"], dims["adjust"]
    return dims


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
            _log_step(case.kind, case.case_id, "预算耗尽，中止后续调用", level=logging.DEBUG)
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
        _log_step(case.kind, case.case_id, "预算耗尽，中止后续调用", level=logging.DEBUG)
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
    if case.kind == "quote_availability":
        return await _run_quote_availability(ctx, case, base)
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


async def _run_quote_availability(ctx: RunContext, case: ChainCase, base: CaseResult) -> CaseResult:
    """快照可用性：经 adapter.fetch_quote 全链路（二级路由→上游→标准化）复验。

    判定（快照无窗口/行数语义，与 bars availability 不同口径）：

    - 适配器调用失败（含上游「无有效行情抛 UPSTREAM_ERROR」语义）→ failed；
    - 返回 Quote 但 last/bid1/ask1 全为 null → failed（无可用价格，捕捉
      订阅未就绪 NaN 置空、权限静默降级等上游异常形态）；
    - 否则 confirmed。volume/amount 的 null（如外汇无量）不判定，
      如实记录进 observation（缺失值 null 口径的漂移观察）。
    """
    started = time.perf_counter()
    if not await ctx.take_call():
        _log_step(case.kind, case.case_id, "预算耗尽，中止后续调用", level=logging.DEBUG)
        base.reason = "call budget exhausted"
        return base
    query = QuoteQuery(
        provider=ctx.record.name,
        asset_type=case.asset_type,
        instrument=Instrument.parse(case.instrument),
        variant=case.variant,
    )
    try:
        quote = await asyncio.wait_for(
            ctx.record.adapter.fetch_quote(query), ctx.pacing.timeout_s
        )
    except TapehubError as exc:
        base.duration_ms = round((time.perf_counter() - started) * 1000, 1)
        _log_step(
            case.kind, case.case_id,
            f"{exc.code.value}: {exc.message[:_STEP_ERROR_TEXT_LIMIT]}",
        )
        return _failed_from(
            base,
            {
                "error_code": exc.code.value,
                "error_type": type(exc).__name__,
                "error_text": exc.message,
            },
            base.duration_ms,
        )
    except TimeoutError:
        base.duration_ms = round((time.perf_counter() - started) * 1000, 1)
        return _failed_from(
            base,
            {
                "error_code": "TIMEOUT",
                "error_type": "TimeoutError",
                "error_text": "adapter call timed out",
            },
            base.duration_ms,
        )
    except Exception as exc:  # noqa: BLE001 - 探针必须记录未知异常而不是崩溃
        base.duration_ms = round((time.perf_counter() - started) * 1000, 1)
        return _failed_from(
            base,
            {
                "error_code": "UNEXPECTED",
                "error_type": type(exc).__name__,
                "error_text": str(exc)[:500],
            },
            base.duration_ms,
        )
    base.duration_ms = round((time.perf_counter() - started) * 1000, 1)
    _log_step(case.kind, case.case_id, f"last_price={quote.last_price}")
    base.observation = {
        "timestamp": quote.timestamp.isoformat(),
        "last_price": quote.last_price,
        "bid_price1": quote.bid_price1,
        "ask_price1": quote.ask_price1,
        "volume": quote.volume,
        "amount": quote.amount,
        "extensions_keys": sorted(quote.extensions) if quote.extensions else [],
        "received_at": quote.received_at.isoformat(),
    }
    if quote.last_price is None and quote.bid_price1 is None and quote.ask_price1 is None:
        base.verdict = CaseVerdict.FAILED.value
        base.reason = "quote carries no usable price (last/bid/ask all null)"
        return base
    base.verdict = CaseVerdict.CONFIRMED.value
    base.reason = "quote availability confirmed"
    return base


def _series_compare_dict(result: "analysis.SeriesCompare") -> dict[str, Any]:
    """SeriesCompare → JSON 安全字典（datetime 一律转 ISO 字符串）。"""
    return {
        "field": result.field,
        "compared": result.compared,
        "identical": result.identical,
        "max_abs_diff": result.max_abs_diff,
        "tolerance": result.tolerance,
        "first_divergence": (
            result.first_divergence.isoformat() if result.first_divergence else None
        ),
    }


async def _run_adjust_diff(ctx: RunContext, case: ChainCase, base: CaseResult) -> CaseResult:
    assert case.compared_adjust is not None
    fetch_baseline = _make_fetcher(ctx, case, adjust=case.adjust)
    fetch_compared = _make_fetcher(ctx, case, adjust=case.compared_adjust)

    async def _fetch_logged(
        fetch: analysis.WindowFetch, label: str, adjust_value: str
    ) -> list[Bar]:
        """单侧抓取 + 子步骤日志（成功记 bar 数，失败记错误摘要后原样上抛）。"""
        try:
            bars = await fetch(case.window_start, case.window_end)
        except BudgetExhausted:
            _log_step(
                case.kind, case.case_id, f"{label}({adjust_value}) → 预算耗尽",
                level=logging.DEBUG,
            )
            raise
        except TapehubError as exc:
            _log_step(
                case.kind, case.case_id,
                f"{label}({adjust_value}) → {exc.code.value}: "
                f"{exc.message[:_STEP_ERROR_TEXT_LIMIT]}",
            )
            raise
        _log_step(case.kind, case.case_id, f"{label}({adjust_value}) → {len(bars)} bars")
        return bars

    try:
        baseline = await _fetch_logged(fetch_baseline, "baseline", case.adjust.value)
        compared = await _fetch_logged(fetch_compared, "compared", case.compared_adjust.value)
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
        "compares": [_series_compare_dict(c) for c in compares],
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
    # 滚动保留的数据边界不可能超过当前时刻；target 窗口终点若写在未来，
    # 超出 now 的搜索区间对「窗口非空」谓词不单调（未来与边界前同样为空），
    # 会把二分诱入未来空窗而误报「无数据」——搜索终点裁剪到 now。
    latest = min(case.window_end, datetime.now(CN_TIMEZONE))
    try:
        if case.full_history:
            # 全量模式：上游无日期过滤（每次返回全量历史），单次调用直接读最早 bar
            result = await analysis.run_history_depth_full(
                fetch, earliest=case.window_start, latest=latest, snap=case.snap
            )
        else:
            # 二分模式：analysis 经 on_step 回传每轮探测条目（含 phase 阶段
            # 标签），此处转成子步骤日志行实时可见（预算水位读共享预算）
            step_no = 0

            def _on_step(entry: dict[str, Any]) -> None:
                nonlocal step_no
                step_no += 1
                _log_step(
                    case.kind, case.case_id,
                    f"{entry['phase']}#{step_no} "
                    f"[{entry['start']}, {entry['end']}] count={entry['count']} "
                    f"(预算 {ctx.budget.used}/{ctx.budget.max_calls})",
                )

            result = await analysis.run_history_depth(
                fetch,
                earliest=case.window_start,
                latest=latest,
                era_step_years=case.era_step_years,
                snap=case.snap,
                max_calls=case.expected_calls,
                on_step=_on_step,
            )
    except TapehubError as exc:
        return _failed_from(base, {"error_code": exc.code.value, "error_text": exc.message})
    base.verdict = CaseVerdict.OBSERVE.value
    base.observation = {
        "earliest_estimate": (
            result.earliest_estimate.isoformat() if result.earliest_estimate else None
        ),
        "first_bar": result.first_bar.isoformat() if result.first_bar else None,
        "approx": result.approx,
        "snap": result.snap,
        "calls_used": result.calls_used,
        "complete": result.complete,
        "probes": result.probes,
    }
    if result.earliest_estimate is None:
        base.reason = "no data found in search range"
    elif not result.complete:
        base.reason = "search stopped early: call budget exhausted"
    elif case.full_history:
        base.reason = "first bar exact (full-history call); start snapped to calendar boundary"
    else:
        base.reason = "calendar-boundary declaration (conservative upper bound; informational)"
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

    超出容差的不一致（数值差异、无重叠、预算中断导致样本不全）都按
    suspect 记录并在报告中给出分歧细节（差异幅度 + 首个分歧时间戳）；
    容差内的差异判 confirmed，但差异幅度始终如实记录（表示层精度差异，
    如跨源小数位）。
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
            _log_step(
                case.kind, case.case_id, f"variant={variant.value} → 预算耗尽",
                level=logging.DEBUG,
            )
            incomplete = True
            break
        if error is not None:
            if isinstance(error, dict):
                _log_step(
                    case.kind, case.case_id,
                    f"variant={variant.value} → {_error_detail(error)}",
                )
            return _failed_from(base, error)
        assert batch is not None
        series[variant.value] = batch.bars
        _log_step(case.kind, case.case_id, f"variant={variant.value} → {len(batch.bars)} bars")

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
                series[left_name], series[right_name], field_name,
                tolerance=case.tolerance,
            )
            entry = _series_compare_dict(result)
            entry["a"] = left_name
            entry["b"] = right_name
            comparisons.append(entry)
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
    within_tolerance = [c for c in comparisons if c["max_abs_diff"] > 0]
    if within_tolerance:
        max_diff = max(c["max_abs_diff"] for c in within_tolerance)
        base.reason = (
            f"all declared variants agree within tolerance {case.tolerance:g} "
            f"(recorded diffs: max {max_diff:.4g})"
        )
    else:
        base.reason = "all declared variants agree on identical queries"
    return base


# ---------------------------------------------------------------------------
# raw 探针
# ---------------------------------------------------------------------------


def _interpolate(value: Any, default_module: str | None = None) -> Any:
    """递归解析 kwargs 引用；引用缺失抛 MissingEnvError（值绝不回传）。

    - ``"$ENV_NAME"``：环境变量引用；
    - ``{"$ctor": {attr, module?, kwargs}}``：轻量 SDK 对象构造（如认证对象），
      ``module`` 缺省继承所在 case 的 module；仅用于本地构造，不做网络调用；
    - ``{"$deadline_s": 秒数}``：相对当前时刻的秒数 → unix epoch 秒
      （供 wait/deadline 类上游参数使用）。
    """
    if isinstance(value, str):
        match = _ENV_REF.match(value)
        if match is not None:
            name = match.group(1)
            resolved = _lookup_env(name)
            if not resolved:
                raise MissingEnvError(name)
            return resolved
        return value
    if isinstance(value, dict):
        if set(value.keys()) == {"$ctor"}:
            spec = CtorSpec.model_validate(value["$ctor"])
            module = importlib.import_module(spec.module or default_module or "")
            constructor = _resolve_attr(module, spec.attr)
            return constructor(**_interpolate(spec.kwargs, default_module))
        if set(value.keys()) == {"$deadline_s"} and isinstance(
            value["$deadline_s"], (int, float)
        ):
            return time.time() + float(value["$deadline_s"])
        return {key: _interpolate(item, default_module) for key, item in value.items()}
    if isinstance(value, list):
        return [_interpolate(item, default_module) for item in value]
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
    # 表形结果（pandas DataFrame/Series，如 tushare 全系接口返回 DataFrame）：
    # 框架不 import pandas（保持 Provider 无关、可选依赖隔离），按 shape
    # 鸭子类型识别，shape[0] 即行数
    shape = getattr(result, "shape", None)
    if isinstance(shape, tuple) and shape and isinstance(shape[0], int):
        return shape[0]
    return None


def _sample_of(result: Any, limit: int = 3) -> list[Any]:
    if isinstance(result, (list, tuple)):
        return [_jsonable(item) for item in list(result)[:limit]]
    if result is None:
        return []
    return [_jsonable(result)]


def _raw_verdict(
    spec: RawCaseSpec,
    *,
    error: BaseException | None,
    rows: int | None,
    fields_ok: bool | None = None,
) -> tuple[CaseVerdict, str]:
    if error is not None:
        matched = (
            spec.expect.status == "error"
            and spec.expect.error_contains is not None
            and spec.expect.error_contains in str(error)
        )
    elif spec.expect.status == "ok" and spec.expect.min_rows is not None:
        matched = rows is not None and rows >= spec.expect.min_rows
    elif spec.expect.status == "ok" and spec.expect.min_fields:
        matched = fields_ok is True
    else:
        matched = False
    if not spec.assert_:
        return CaseVerdict.OBSERVE, f"observe-only: expectation matched={matched}"
    if matched:
        return CaseVerdict.CONFIRMED, "expectation matched"
    return CaseVerdict.FAILED, (
        f"expectation not matched (status={spec.expect.status}, "
        f"error={type(error).__name__ if error else None}, rows={rows}, "
        f"fields_ok={fields_ok})"
    )


def _fields_present(result: Any, fields: list[str]) -> bool:
    """mapping 形结果（如快照对象）是否包含全部期望字段名。"""
    if not isinstance(result, Mapping):
        return False
    return all(field_name in result for field_name in fields)


async def _run_raw_case(ctx: RunContext, plan: RawPlan) -> CaseResult:
    spec = plan.spec
    base = CaseResult(
        case_id=spec.id,
        kind="raw",
        verdict=CaseVerdict.SKIPPED.value,
        reason="",
        dims={"module": spec.module, "purpose": spec.purpose},
    )

    async def _execute() -> tuple[CaseResult, Any]:
        try:
            client_kwargs = (
                _interpolate(spec.client.kwargs, spec.module) if spec.client else {}
            )
            call_kwargs = [_interpolate(call.kwargs, spec.module) for call in spec.call]
        except MissingEnvError as exc:
            base.reason = f"env missing: {exc.name}"
            return base, None
        except Exception as exc:  # noqa: BLE001 - 单条 case 配置错误不中止整轮运行
            base.reason = f"kwargs resolve failed: {exc}"
            return base, None

        try:
            module = importlib.import_module(spec.module)
        except ModuleNotFoundError:
            base.reason = f"module not installed: {spec.module}"
            return base, None

        target: Any = None
        error: BaseException | None = None
        calls_made = 0
        if spec.client is not None:
            if not await ctx.take_call():
                _log_step("raw", spec.id, "预算耗尽，中止后续调用", level=logging.DEBUG)
                base.reason = "call budget exhausted"
                return base, None
            calls_made += 1
            try:
                constructor = _resolve_attr(module, spec.client.attr)
                target = await asyncio.wait_for(
                    asyncio.to_thread(constructor, **client_kwargs), ctx.pacing.timeout_s
                )
                _log_step("raw", spec.id, f"client {spec.client.attr} → ok")
            except Exception as exc:  # noqa: BLE001 - 构造失败按期望匹配处理
                _log_step(
                    "raw", spec.id,
                    f"client {spec.client.attr} → {type(exc).__name__}: "
                    f"{str(exc)[:_STEP_ERROR_TEXT_LIMIT]}",
                )
                error = exc

        owner: Any = target
        result: Any = target
        if error is None:
            for call, kwargs in zip(spec.call, call_kwargs, strict=True):
                if not await ctx.take_call():
                    _log_step("raw", spec.id, "预算耗尽，中止后续调用", level=logging.DEBUG)
                    base.reason = "call budget exhausted"
                    base.observation = {"calls_made": calls_made}
                    return base, target
                calls_made += 1
                call_label = call.method or call.module_func or ""
                try:
                    root = owner if call.target == "client" else result
                    # method 支持「资源.方法」点链（如 quotes.get）：
                    # 逐段 getattr 解析到最终可调用对象，单段行为不变
                    func = (
                        _resolve_attr(root, call.method)
                        if call.method
                        else _resolve_attr(module, call.module_func or "")
                    )
                    result = await asyncio.wait_for(
                        asyncio.to_thread(func, **kwargs), ctx.pacing.timeout_s
                    )
                    rows = _count_rows(result)
                    detail = f"{rows} rows" if rows is not None else "ok"
                    _log_step("raw", spec.id, f"call#{calls_made} {call_label} → {detail}")
                except Exception as exc:  # noqa: BLE001 - 探针必须记录异常而不是崩溃
                    _log_step(
                        "raw", spec.id,
                        f"call#{calls_made} {call_label} → "
                        f"{type(exc).__name__}: {str(exc)[:_STEP_ERROR_TEXT_LIMIT]}",
                    )
                    error = exc
                    break

        rows = None if error is not None else _count_rows(result)
        fields_ok = (
            None if error is not None else _fields_present(result, spec.expect.min_fields)
        )
        verdict, reason = _raw_verdict(spec, error=error, rows=rows, fields_ok=fields_ok)
        base.verdict = verdict.value
        base.reason = reason
        base.observation = {
            "calls_made": calls_made,
            "rows": rows,
            "error": (
                {"type": type(error).__name__, "text": str(error)[:500]}
                if error is not None
                else None
            ),
            "sample": [] if error is not None else _sample_of(result),
        }
        return base, target

    base, client = await _execute()
    # 客户端释放（声明了 close 且客户端构造成功时）：与 connection 探针同款
    # 「显式释放」语义，防止第三方 SDK 连接/asyncgen 跨段泄漏；close 是本地
    # 释放动作，不消耗调用预算；失败如实记录，不影响 case 判定
    if spec.close is not None and client is not None:
        try:
            close_kwargs = _interpolate(spec.close.kwargs, spec.module)
            closer = _resolve_attr(client, spec.close.method or "")
            await asyncio.wait_for(
                asyncio.to_thread(closer, **close_kwargs), ctx.pacing.timeout_s
            )
            _log_step("raw", spec.id, "close → ok")
            base.observation = {**base.observation, "closed": True}
        except Exception as exc:  # noqa: BLE001 - 释放失败如实记录
            _log_step(
                "raw", spec.id,
                f"close → {type(exc).__name__}: {str(exc)[:_STEP_ERROR_TEXT_LIMIT]}",
            )
            base.observation = {
                **base.observation,
                "closed": False,
                "close_error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }
    return base


class _BudgetAbort(Exception):
    """连接探针内部信号：调用预算耗尽，中止当前 case。"""


async def _run_connection_case(ctx: RunContext, plan: ConnectionPlan) -> CaseResult:
    """连接探针：每轮并发创建客户端 → 各自首次取数 → 逐个释放，全程计时。"""
    spec = plan.spec
    total = spec.rounds * spec.concurrent
    base = CaseResult(
        case_id=spec.id,
        kind="connection",
        verdict=CaseVerdict.SKIPPED.value,
        reason="",
        dims={
            "module": spec.module,
            "purpose": spec.purpose,
            "rounds": str(spec.rounds),
            "concurrent": str(spec.concurrent),
        },
    )
    try:
        client_kwargs = _interpolate(spec.client.kwargs, spec.module)
        first_kwargs = _interpolate(spec.first_call.kwargs, spec.module)
        close_kwargs = _interpolate(spec.close.kwargs, spec.module)
    except MissingEnvError as exc:
        base.reason = f"env missing: {exc.name}"
        return base
    except Exception as exc:  # noqa: BLE001 - 单条 case 配置错误不中止整轮运行
        base.reason = f"kwargs resolve failed: {exc}"
        return base

    try:
        module = importlib.import_module(spec.module)
    except ModuleNotFoundError:
        base.reason = f"module not installed: {spec.module}"
        return base

    timeout = ctx.pacing.timeout_s
    timings: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    created = first_ok = closed = 0

    async def _spawn(round_index: int, instance_index: int) -> dict[str, Any]:
        """创建单个客户端并完成首次取数，返回计时观测（含未释放的 client）。"""
        if not await ctx.take_call():
            raise _BudgetAbort()
        t0 = time.perf_counter()
        constructor = _resolve_attr(module, spec.client.attr)
        try:
            client = await asyncio.wait_for(
                asyncio.to_thread(constructor, **client_kwargs), timeout
            )
        except Exception as exc:  # noqa: BLE001 - 创建失败记子步骤后原样上抛
            _log_step(
                "connection", spec.id,
                f"round{round_index}#{instance_index} → create {type(exc).__name__}",
            )
            raise
        create_ms = round((time.perf_counter() - t0) * 1000, 1)
        if not await ctx.take_call():
            _log_step(
                "connection", spec.id, f"round{round_index}#{instance_index} → 预算耗尽",
                level=logging.DEBUG,
            )
            raise _BudgetAbort()
        t1 = time.perf_counter()
        func = getattr(client, spec.first_call.method or "")
        try:
            await asyncio.wait_for(asyncio.to_thread(func, **first_kwargs), timeout)
        except BaseException as exc:
            # 首调失败（含超时）也要尽力释放已创建的客户端，防止连接泄漏；
            # 释放失败不掩盖原始异常
            _log_step(
                "connection", spec.id,
                f"round{round_index}#{instance_index} → first_call {type(exc).__name__}",
            )
            try:
                closer = getattr(client, spec.close.method or "")
                await asyncio.wait_for(
                    asyncio.to_thread(closer, **close_kwargs), timeout
                )
            except Exception:  # noqa: BLE001 - 尽力释放，失败不掩盖原始异常
                pass
            raise
        first_ms = round((time.perf_counter() - t1) * 1000, 1)
        _log_step(
            "connection", spec.id,
            f"round{round_index}#{instance_index} "
            f"created={create_ms}ms first_call={first_ms}ms",
        )
        return {
            "round": round_index,
            "instance": instance_index,
            "create_ms": create_ms,
            "first_call_ms": first_ms,
            "_client": client,
        }

    for round_index in range(1, spec.rounds + 1):
        try:
            spawned = await asyncio.gather(
                *[_spawn(round_index, i) for i in range(1, spec.concurrent + 1)],
                return_exceptions=True,
            )
        except _BudgetAbort:
            base.reason = "call budget exhausted"
            base.observation = {"timings": timings[:24], "errors": errors[:6]}
            return base
        for item in spawned:
            if isinstance(item, BaseException):
                if isinstance(item, _BudgetAbort):
                    base.reason = "call budget exhausted"
                    base.observation = {"timings": timings[:24], "errors": errors[:6]}
                    return base
                errors.append({"type": type(item).__name__, "text": str(item)[:300]})
                continue
            client = item.pop("_client")
            created += 1
            try:
                if not await ctx.take_call():
                    raise _BudgetAbort()
                t2 = time.perf_counter()
                closer = getattr(client, spec.close.method or "")
                await asyncio.wait_for(
                    asyncio.to_thread(closer, **close_kwargs), timeout
                )
                item["close_ms"] = round((time.perf_counter() - t2) * 1000, 1)
                closed += 1
                _log_step(
                    "connection", spec.id,
                    f"round{item['round']}#{item['instance']} closed={item['close_ms']}ms",
                )
            except Exception as exc:  # noqa: BLE001 - 释放失败如实记录
                _log_step(
                    "connection", spec.id,
                    f"round{item['round']}#{item['instance']} → close {type(exc).__name__}",
                )
                errors.append({"type": type(exc).__name__, "text": str(exc)[:300]})
            timings.append(item)

    first_ok = sum(1 for row in timings if row.get("first_call_ms") is not None)
    all_ok = (
        created == total and first_ok == total and closed == total and not errors
    )
    if not spec.assert_:
        base.verdict = CaseVerdict.OBSERVE.value
        base.reason = f"observe-only: created={created}/{total}, closed={closed}/{total}"
    elif all_ok:
        base.verdict = CaseVerdict.CONFIRMED.value
        base.reason = f"all {total} instance(s) created, first-called and closed"
    else:
        base.verdict = CaseVerdict.FAILED.value
        base.reason = (
            f"lifecycle incomplete: created={created}/{total}, "
            f"first_call={first_ok}/{total}, closed={closed}/{total}, "
            f"errors={len(errors)}"
        )
    base.observation = {
        "instances_expected": total,
        "created": created,
        "first_call_ok": first_ok,
        "closed": closed,
        "timings": timings[:24],
        "errors": errors[:6],
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


def _dry_run_result_connection(plan: ConnectionPlan) -> CaseResult:
    return CaseResult(
        case_id=plan.spec.id,
        kind="connection",
        verdict=CaseVerdict.SKIPPED.value,
        reason=f"dry-run (planned {plan.expected_calls} calls)",
        dims={
            "module": plan.spec.module,
            "purpose": plan.spec.purpose,
            "rounds": str(plan.spec.rounds),
            "concurrent": str(plan.spec.concurrent),
        },
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


def _skipped_result_connection(plan: ConnectionPlan, reason: str) -> CaseResult:
    return CaseResult(
        case_id=plan.spec.id,
        kind="connection",
        verdict=CaseVerdict.SKIPPED.value,
        reason=reason,
        dims={
            "module": plan.spec.module,
            "rounds": str(plan.spec.rounds),
            "concurrent": str(plan.spec.concurrent),
        },
    )
