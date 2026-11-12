"""探针段级隔离测试（离线）。

覆盖 ``probes/cli.py`` 的 ``_run_sections`` 修复：

- 每 Provider 段独立事件循环——前一段遗留的 pending 任务随本段循环消亡，
  绝不污染下一段（2026-11-12 tqsdk→akshare 跨段崩溃的回归防线）；
- 段间停机钩子：适配器的 ``shutdown_runtime``（lifespan 同款鸭子类型）
  在每段结束后被调用；
- 段级异常 containment：单段框架级崩溃计入退出码，不中断后续段。
"""

import asyncio
from pathlib import Path
from typing import ClassVar

import probes.cli as cli_module
from probes.cli import _run_sections
from probes.schema import ProbeFile

from tapehub.providers.manifest import load_manifest_file
from tapehub.providers.registry import ProviderRegistry, build_provider_record
from tests.conftest import make_settings
from tests.fixtures.providers.fake_provider import FakeBarProvider

FIXTURES = Path(__file__).parents[2] / "fixtures"


class _LeakRecordingProvider(FakeBarProvider):
    """在 fetch_bars 时制造循环泄漏任务并记录运行循环的假适配器。

    继承 FakeBarProvider 复用确定性数据生成；实例记录自己的运行循环与
    泄漏任务，供段级隔离断言。
    """

    instances: ClassVar[list["_LeakRecordingProvider"]] = []

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.loops: list[asyncio.AbstractEventLoop] = []
        self.leaked_tasks: list[asyncio.Task[None]] = []
        self.shutdown_calls = 0
        _LeakRecordingProvider.instances.append(self)

    async def fetch_bars(self, query):  # type: ignore[no-untyped-def]
        loop = asyncio.get_running_loop()
        self.loops.append(loop)
        # 模拟第三方 SDK 泄漏：段循环上遗留一个永远不完成的 pending 任务
        self.leaked_tasks.append(loop.create_task(asyncio.sleep(3600)))
        return await super().fetch_bars(query)

    def shutdown_runtime(self) -> None:
        self.shutdown_calls += 1


def _two_provider_registry() -> tuple[ProviderRegistry, object, object]:
    manifest = load_manifest_file(FIXTURES / "manifests" / "fake_provider.yaml")
    alpha = build_provider_record("alpha", manifest, _LeakRecordingProvider)
    beta = build_provider_record("beta", manifest, _LeakRecordingProvider)
    routing: dict[tuple[str, str, str], tuple[str, ...]] = {}
    for record in (alpha, beta):
        for cap in record.capabilities:
            for market in cap.markets:
                for asset_type in cap.asset_types:
                    routing[(cap.data_type, market, asset_type)] = (record.name,)
    return ProviderRegistry([alpha, beta], routing), alpha.adapter, beta.adapter


def _probe_for(provider: str, target_key: str) -> ProbeFile:
    """单 target 探针文件：alpha→stock、beta→etf（避免路由键重叠）。

    etf 的 PT1M 周期在 fake fixture 中无对应 operation（manifest 声明与
    操作表的既有缺口，与本测试无关），显式 skip 跳过。
    """
    skip = (
        [
            {
                "case": f"{target_key}-PT1M-none-eastmoney",
                "reason": "fixture 无该周期 operation",
            }
        ]
        if target_key != "stock"
        else []
    )
    data = {
        "probe": provider,
        "version": 1,
        "targets": {
            target_key: {
                "instrument": (
                    "CN-SSE-600000" if target_key == "stock" else "CN-SSE-510050"
                ),
                "window": {"start": "2025-01-01", "end": "2025-03-31"},
                "min_bars": 1,
            }
        },
        "chain": {"include_declared": True, "skip": skip},
    }
    return ProbeFile.model_validate(data)


def _run(probes: list[ProbeFile], registry: ProviderRegistry, tmp_path: Path) -> bool:
    return _run_sections(
        probes,
        registry,
        make_settings(max_limit=1000, catalog_version="test"),
        dry_run=False,
        results_dir=tmp_path,
    )


def test_sections_isolate_loops_and_shutdown(tmp_path: Path) -> None:
    """两段顺序运行：各自独立循环、泄漏任务被本段回收、停机钩子逐段调用。"""
    _LeakRecordingProvider.instances.clear()
    probes = [_probe_for("alpha", "stock"), _probe_for("beta", "etf")]
    registry, alpha, beta = _two_provider_registry()

    any_failed = _run(probes, registry, tmp_path)

    assert any_failed is False
    # 两段各自 fetch 已执行（alpha=stock P1D+PT1M 共 2 次；beta=etf P1D 1 次，
    # PT1M 被 skip）；段内 case 共享本段循环，段间循环彼此独立且已关闭
    assert len(alpha.loops) == 2 and len(beta.loops) == 1
    assert alpha.loops[0] is alpha.loops[1]  # 同段共享一个循环
    assert alpha.loops[0] is not beta.loops[0]  # 跨段循环隔离
    assert all(loop.is_closed() for loop in (*alpha.loops, *beta.loops))
    # 段内泄漏的 pending 任务随本段循环消亡（asyncio.run 的取消收尾）
    assert all(task.cancelled() for task in (*alpha.leaked_tasks, *beta.leaked_tasks))
    # 段间停机钩子逐段调用
    assert alpha.shutdown_calls == 1 and beta.shutdown_calls == 1
    # 两段报告均已写盘
    reports = list(tmp_path.glob("report-*.md"))
    assert {p.name.split("-")[1] for p in reports} == {"alpha", "beta"}


def test_section_crash_does_not_stop_next_section(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """单段框架级异常：计入退出码（True），后续段照常运行并写报告。"""
    probes = [_probe_for("alpha", "stock"), _probe_for("beta", "etf")]
    registry, _alpha, _beta = _two_provider_registry()
    ran: list[str] = []
    real = cli_module.run_provider_probe

    def fake_run_provider_probe(probe, registry, settings, *, execute=True):  # type: ignore[no-untyped-def]
        ran.append(probe.probe)
        if probe.probe == "alpha":
            raise RuntimeError("boom")
        return real(probe, registry, settings, execute=execute)

    monkeypatch.setattr(cli_module, "run_provider_probe", fake_run_provider_probe)

    any_failed = _run(probes, registry, tmp_path)

    assert any_failed is True
    assert ran == ["alpha", "beta"]  # 第二段未被第一段崩溃拖垮
    assert list(tmp_path.glob("report-beta-*.md"))
    assert not list(tmp_path.glob("report-alpha-*.md"))  # 崩溃段无报告


def test_dry_run_sections_skip_shutdown(tmp_path: Path) -> None:
    """dry-run 不执行 case 也不调停机钩子（运行时尚未启动，无可回收）。"""
    probes = [_probe_for("alpha", "stock")]
    registry, alpha, _beta = _two_provider_registry()

    any_failed = _run_sections(
        probes,
        registry,
        make_settings(max_limit=1000, catalog_version="test"),
        dry_run=True,
        results_dir=tmp_path,
    )

    assert any_failed is False
    assert alpha.loops == []  # dry-run 不执行 fetch
    assert alpha.shutdown_calls == 0
