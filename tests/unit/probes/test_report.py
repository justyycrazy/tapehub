"""探针报告渲染与落盘测试（离线）。"""

from datetime import datetime
from pathlib import Path

from probes.report import CaseResult, RunResult, render_markdown, write_results

from tapehub.domain.models import CN_TIMEZONE


def _run() -> RunResult:
    started = datetime(2026, 1, 1, tzinfo=CN_TIMEZONE)
    return RunResult(
        provider="fake-provider",
        started_at=started,
        finished_at=started,
        environment={"extras": {"some_sdk": True}, "env": {"TAPEHUB_X": True}},
        call_budget={"max": 120, "used": 42},
        notes="时点证据说明",
        cases=[
            CaseResult(
                case_id="stock-P1D-none-eastmoney",
                kind="availability",
                verdict="confirmed",
                reason="availability confirmed",
                dims={"asset_type": "stock"},
                observation={
                    "count": 55,
                    "first_timestamp": "2025-01-02T00:00:00+08:00",
                    "last_timestamp": "2025-03-31T00:00:00+08:00",
                    "null_rates": {"amount": 0.2},
                },
                duration_ms=12.3,
            ),
            CaseResult(
                case_id="stock-P1M-none-eastmoney",
                kind="availability",
                verdict="failed",
                reason="0 bars in declared capability window",
            ),
            CaseResult(
                case_id="adjust-diff-stock-none-vs-qfq",
                kind="adjust_diff",
                verdict="suspect",
                reason="adjust_no_op_suspect: adjusted series identical to unadjusted",
            ),
            CaseResult(
                case_id="history-depth-stock",
                kind="history_depth",
                verdict="observe",
                reason="calendar-boundary declaration (conservative upper bound; informational)",
                observation={
                    "earliest_estimate": "2003-05-01T00:00:00+08:00",
                    "first_bar": "2003-04-10T00:00:00+08:00",
                    "approx": True,
                    "snap": "month",
                    "calls_used": 12,
                    "complete": True,
                },
            ),
            CaseResult(
                case_id="stock-PT5M-none-eastmoney",
                kind="availability",
                verdict="skipped",
                reason="explicit skip: 测试原因",
            ),
        ],
    )


def test_render_markdown_sections() -> None:
    text = render_markdown(_run())
    assert "# tapehub Provider 探针报告 — fake-provider" in text
    assert "## 已确认能力（confirmed）" in text
    assert "## 声明未通过（failed）" in text
    assert "## suspect / observe" in text
    assert "## history.start 建议回填值（人工确认后再改 manifest）" in text
    assert "## skipped" in text
    assert "stock-P1D-none-eastmoney" in text
    assert "adjust_no_op_suspect" in text
    assert "运行时间（北京时间 +08:00）：2026-01-01T00:00:00+08:00" in text
    assert "2003-05-01T00:00:00+08:00" in text
    assert "2003-04-10T00:00:00+08:00" in text
    assert "测试原因" in text


def test_write_results_creates_files(tmp_path: Path) -> None:
    json_path, md_path = write_results(_run(), tmp_path)
    assert json_path.exists() and md_path.exists()
    assert json_path.parent.name == "fake-provider"
    assert "20260101T000000+0800" in json_path.name
    content = json_path.read_text(encoding="utf-8")
    assert '"provider": "fake-provider"' in content
    assert "2026-01-01T00:00:00+08:00" in content
