"""探针命令行入口。

用法::

    uv run python -m probes.cli list [--provider NAME]...      # 展开 case 清单（零网络）
    uv run python -m probes.cli run  [--provider NAME]... [--dry-run] [--yes]

真实运行会发起上游调用并消耗配额，必须显式 ``--yes``；
dry-run 只做展开与前提检查，不发任何网络请求。
退出码：0 正常（允许 skipped/observe），1 存在 failed，2 参数或前提问题。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from probes.loader import expand_builtin, load_probe_files
from probes.report import write_results
from probes.runner import run_provider_probe
from tapehub.providers.registry import build_default_registry
from tapehub.settings import Settings

DEFAULT_CASES_DIR = Path(__file__).resolve().parent / "cases"
DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="probes", description="tapehub Provider 数据探针")
    sub = parser.add_subparsers(dest="command", required=True)

    cases_dir = {"default": DEFAULT_CASES_DIR}

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--provider", action="append", default=[], help="只跑指定 Provider（可多次）"
        )
        p.add_argument("--cases-dir", type=Path, help="探针文件目录", **cases_dir)

    p_list = sub.add_parser("list", help="展开后的 case 清单（零网络）")
    add_common(p_list)

    p_run = sub.add_parser("run", help="执行探针（真实运行消耗上游配额）")
    add_common(p_run)
    p_run.add_argument("--dry-run", action="store_true", help="只打印计划调用，不发真实请求")
    p_run.add_argument("--yes", action="store_true", help="确认发起真实上游调用")
    p_run.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR, help="结果输出目录")
    return parser.parse_args(argv)


def _select(probes: list, names: list[str]) -> list:
    if not names:
        return probes
    by_name = {probe.probe: probe for probe in probes}
    unknown = [name for name in names if name not in by_name]
    if unknown:
        raise SystemExit(f"unknown provider(s): {unknown}")
    return [by_name[name] for name in names]


def cmd_list(args: argparse.Namespace) -> int:
    probes = _select(load_probe_files(args.cases_dir), args.provider)
    for probe in probes:
        expanded = expand_builtin(probe)
        print(f"=== {probe.probe}（pacing {probe.pacing.call_interval_s}s, "
              f"预算 {probe.pacing.max_calls} 次调用）===")
        for case in expanded.chain_cases:
            mark = (
                f"SKIP: {case.skip_reason}"
                if case.skip_reason
                else f"{case.expected_calls} call(s)"
            )
            print(
                f"  [chain] {case.case_id:<46} {case.kind:<16} "
                f"{case.instrument:<22} {mark}"
            )
        for plan in expanded.raw_cases:
            print(
                f"  [raw]   {plan.spec.id:<46} {'module=' + plan.spec.module:<22} "
                f"{plan.expected_calls} call(s)"
            )
        for plan in expanded.connection_cases:
            mark = (
                f"rounds={plan.spec.rounds} x concurrent={plan.spec.concurrent} "
                f"| {plan.expected_calls} call(s)"
            )
            print(f"  [conn]  {plan.spec.id:<46} {'module=' + plan.spec.module:<22} {mark}")
        planned = sum(c.expected_calls for c in expanded.chain_cases if not c.skip_reason)
        planned += sum(p.expected_calls for p in expanded.raw_cases)
        planned += sum(p.expected_calls for p in expanded.connection_cases)
        print(f"  合计计划调用：{planned}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    probes = _select(load_probe_files(args.cases_dir), args.provider)
    if not args.dry_run and not args.yes:
        print("探针将发起真实上游调用并消耗配额。确认请加 --yes；预演请加 --dry-run。")
        return 2

    settings = Settings()
    registry = build_default_registry(settings)

    async def _run_all() -> bool:
        any_failed = False
        for probe in probes:
            print(
                f"=== 运行探针：{probe.probe}（{'dry-run' if args.dry_run else '真实调用'}）===",
                flush=True,
            )
            result = await run_provider_probe(probe, registry, settings, execute=not args.dry_run)
            if not args.dry_run:
                json_path, md_path = write_results(result, args.results_dir)
                print(f"  JSON 证据：{json_path}", flush=True)
                print(f"  Markdown 报告：{md_path}", flush=True)
            counts = result.verdict_counts()
            print(f"  判定统计：{counts}", flush=True)
            if result.file_skip_reason:
                print(f"  文件级跳过：{result.file_skip_reason}", flush=True)
            for case in result.cases:
                if case.verdict == "failed":
                    print(f"  [failed] {case.case_id}: {case.reason}", flush=True)
                    any_failed = True
        return any_failed

    any_failed = asyncio.run(_run_all())
    return 1 if any_failed else 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "list":
        return cmd_list(args)
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
