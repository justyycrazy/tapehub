"""探针文件 schema（pydantic v2）。

规范化约束：

- 所有模型 ``extra="forbid"``：未知键一律拒绝，防止拼写错误静默失效；
- 维度取值直接复用 domain 枚举与 ``Interval.parse``，不另造取值体系；
- 本模块只做静态校验，不做环境检查，也不发起任何网络访问。

探针文件格式版本见 ``PROBE_SCHEMA_VERSION``；不兼容变更必须升版本，
并在 ``probes/README.md`` 记录迁移说明。
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tapehub.domain.intervals import Interval
from tapehub.domain.models import Adjust, AssetType

PROBE_SCHEMA_VERSION = 1


class CaseVerdict(StrEnum):
    """探针判定：统一五种结论，语义见 probes/README.md。"""

    CONFIRMED = "confirmed"
    FAILED = "failed"
    SUSPECT = "suspect"
    SKIPPED = "skipped"
    OBSERVE = "observe"


class _Strict(BaseModel):
    """公共基类：禁止未知字段。"""

    model_config = ConfigDict(extra="forbid")


class RequiresSpec(_Strict):
    """运行前提：缺失时整个探针文件跳过并写入报告，不报错。"""

    extras: list[str] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)


class PacingSpec(_Strict):
    """节流与调用预算：探针消耗真实上游配额，默认保守。"""

    call_interval_s: float = Field(default=1.0, gt=0)
    timeout_s: float = Field(default=30.0, gt=0)
    max_calls: int = Field(default=120, ge=1)


class WindowSpec(_Strict):
    """闭区间时间窗口（按日期解释，北京时间零点起算）。"""

    start: dt.date
    end: dt.date

    @model_validator(mode="after")
    def _ordered(self) -> "WindowSpec":
        if self.start >= self.end:
            raise ValueError("window.start must be earlier than window.end")
        return self


class TargetSpec(_Strict):
    """单个 asset_type 的基准标的与默认窗口。"""

    instrument: str
    window: WindowSpec
    # 可用性判定期望：bars 少于 min_bars 判 failed；required_fields 全 null 判 failed
    min_bars: int | None = Field(default=None, ge=0)
    required_fields: list[str] = Field(default_factory=list)


class SkipSpec(_Strict):
    """显式跳过某条 case，必须给出原因（进报告，防止静默漏探）。"""

    case: str
    reason: str = Field(min_length=1)


class AdjustDiffSpec(_Strict):
    """复权生效性探针：同窗口对比 baseline 与 compared 的价格序列。

    按 (interval × variant × pair) 在声明矩阵上**全量展开**，
    用于核对同一参数在不同 variant 下的一致性；
    ``intervals`` 可显式收敛 interval 维度以控制预算（variant 维度永不收敛）。
    """

    targets: list[AssetType] = Field(min_length=1)
    window: WindowSpec
    pairs: list[tuple[Adjust, Adjust]] = Field(default_factory=lambda: [(Adjust.NONE, Adjust.QFQ)])
    fields: list[str] = Field(default_factory=lambda: ["close"])
    intervals: list[Interval] = Field(default_factory=list)


class HistoryDepthSpec(_Strict):
    """历史深度探针：纪元回溯 + 二分搜索最早可用窗口。

    按 (interval × variant) 全量展开；``max_calls`` 是**每条 case** 的预算。
    """

    targets: list[AssetType] = Field(min_length=1)
    earliest: dt.date
    era_step_years: int = Field(default=5, ge=1)
    max_calls: int = Field(default=24, ge=1)
    intervals: list[Interval] = Field(default_factory=list)
    # 二分窗口宽度即精度：earliest_estimate 误差约为该值（天）
    probe_span_days: int = Field(default=90, ge=1)


class VariantConsistencySpec(_Strict):
    """跨 variant 一致性探针：同一 (asset_type, interval, adjust) 的查询在全部
    声明 variant 下的序列应当一致；任何不一致必须记录并在报告中呈现。
    """

    targets: list[AssetType] = Field(min_length=1)
    window: WindowSpec
    fields: list[str] = Field(default_factory=lambda: ["close"])
    intervals: list[Interval] = Field(default_factory=list)


class LimitTruncationSpec(_Strict):
    """单次上限探针：最小声明周期 + 超长窗口，实测单次返回上限（按 variant 全量展开）。"""

    targets: list[AssetType] = Field(min_length=1)
    interval: Interval
    max_range: Interval
    # 文档声称的上限；不写则只观察不判定
    documented_max: int | None = Field(default=None, ge=1)


class ChainSpec(_Strict):
    """统一链路探针配置（经 adapter.fetch_bars）。"""

    include_declared: bool = True
    skip: list[SkipSpec] = Field(default_factory=list)
    adjust_diff: AdjustDiffSpec | None = None
    history_depth: HistoryDepthSpec | None = None
    limit_truncation: LimitTruncationSpec | None = None
    variant_consistency: VariantConsistencySpec | None = None


class ClientSpec(_Strict):
    """SDK 客户端构造：``<module>.attr(**kwargs)``。

    ``module`` 写在 case 顶层；kwargs 支持 ``$ENV`` 环境变量引用、
    ``$ctor``（嵌套 SDK 对象构造，如凭据对象）与 ``$deadline_s``
    （相对当前时刻的秒数 → unix epoch）三类引用，语义见 probes/README.md。
    """

    attr: str
    kwargs: dict[str, Any] = Field(default_factory=dict)


class CallSpec(_Strict):
    """单次 SDK 调用：首条作用于 client，后续条作用于上一条的结果（支持链式）。

    ``target`` 显式覆盖作用对象：``client`` 强制作用于本 case 自建的客户端，
    ``result`` 强制作用于上一条结果；缺省（None）保持既有链式语义。
    """

    method: str | None = None
    module_func: str | None = None
    kwargs: dict[str, Any] = Field(default_factory=dict)
    target: Literal["client", "result"] | None = None

    @model_validator(mode="after")
    def _one_target(self) -> "CallSpec":
        if bool(self.method) == bool(self.module_func):
            raise ValueError("call must specify exactly one of method / module_func")
        return self


class CtorSpec(_Strict):
    """``$ctor`` 嵌套构造引用：``<module>.attr(**kwargs)``。

    仅用于轻量的凭据/配置对象（如认证对象）；不做网络调用，
    连接类行为必须用 connection / call 表达。``module`` 缺省继承所在 case。
    """

    attr: str
    module: str | None = None
    kwargs: dict[str, Any] = Field(default_factory=dict)


class ExpectSpec(_Strict):
    """raw 探针期望。``error_contains`` 传空串表示任意错误均可。

    ``ok`` 期望至少给出一个验证点：``min_rows``（行数下限）或
    ``min_fields``（mapping 结果必须包含的字段名）。
    """

    status: Literal["ok", "error"] = "ok"
    min_rows: int | None = Field(default=None, ge=0)
    min_fields: list[str] = Field(default_factory=list)
    error_contains: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "ExpectSpec":
        if self.status == "error" and self.error_contains is None:
            raise ValueError("error expectation requires error_contains")
        if self.status == "ok" and self.min_rows is None and not self.min_fields:
            raise ValueError("ok expectation requires min_rows or min_fields")
        return self


class RawCaseSpec(_Strict):
    """声明式 raw 探针：按配置反射调用 SDK，不做任何 Provider 专属代码。"""

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9._-]*$")
    purpose: str = ""
    module: str
    client: ClientSpec | None = None
    call: list[CallSpec] = Field(min_length=1)
    expect: ExpectSpec
    # assert=false 表示仅观察（observe），不参与判定
    assert_: bool = Field(default=True, alias="assert")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ConnectionCaseSpec(_Strict):
    """连接探针：客户端「创建 → 首次取数 → 释放」的生命周期计时与共存验证。

    - ``rounds``：顺序轮数；每轮同时创建 ``concurrent`` 个客户端并各自取数；
    - ``first_call`` / ``close`` 必须是客户端方法（``method``）；
      ``close`` 必填——连接类探针必须显式声明释放方式，防止连接泄漏；
    - 判定：全部步骤无异常 → confirmed（``assert`` false 时 observe）；
      任一异常 → failed。观测值：每实例的 create/first_call/close 毫秒耗时。
    """

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9._-]*$")
    purpose: str = ""
    module: str
    client: ClientSpec
    first_call: CallSpec
    close: CallSpec
    rounds: int = Field(default=1, ge=1)
    concurrent: int = Field(default=1, ge=1)
    assert_: bool = Field(default=True, alias="assert")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _calls_are_client_methods(self) -> "ConnectionCaseSpec":
        for name, call in (("first_call", self.first_call), ("close", self.close)):
            if call.module_func is not None:
                raise ValueError(f"{name} must be a client method (method), not module_func")
            if call.target is not None:
                raise ValueError(f"{name} target is implicit (always the case's own client)")
        return self


class ProbeFile(_Strict):
    """探针文件顶层模型，与 ``cases/<provider>.yaml`` 一一对应。"""

    probe: str
    version: int
    requires: RequiresSpec = Field(default_factory=RequiresSpec)
    pacing: PacingSpec = Field(default_factory=PacingSpec)
    targets: dict[AssetType, TargetSpec] = Field(min_length=1)
    chain: ChainSpec | None = None
    raw: list[RawCaseSpec] = Field(default_factory=list)
    connection: list[ConnectionCaseSpec] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def _content(self) -> "ProbeFile":
        if self.version != PROBE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported probe file version {self.version!r}; "
                f"expected {PROBE_SCHEMA_VERSION}"
            )
        if self.chain is None and not self.raw and not self.connection:
            raise ValueError("probe file must define chain and/or raw and/or connection cases")
        raw_ids = [case.id for case in self.raw]
        if len(raw_ids) != len(set(raw_ids)):
            raise ValueError("duplicate raw case ids")
        conn_ids = [case.id for case in self.connection]
        if len(conn_ids) != len(set(conn_ids)):
            raise ValueError("duplicate connection case ids")
        overlap = set(raw_ids) & set(conn_ids)
        if overlap:
            raise ValueError(f"raw/connection case id overlap: {sorted(overlap)}")
        return self
