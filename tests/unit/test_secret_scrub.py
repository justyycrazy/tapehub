"""日志凭据脱敏测试（审查项 M2）。

覆盖点：

- ``application.scrub.scrub``：值替换、空元组、多值串行；
- ``Settings.credential_values()``：非空收集、空白过滤、路径字段不纳入；
- 三个应用服务 catch-all 日志：异常文本中的伪凭据替换为掩码
  （caplog 断言原文不出现、掩码出现）。
"""

import logging

import pytest

from tapehub.application.bar_query_service import BarQueryService
from tapehub.application.quote_stream_service import QuoteStreamService
from tapehub.application.scrub import CREDENTIAL_MASK, scrub
from tapehub.domain.errors import UpstreamError
from tests.conftest import make_settings

FAKE_KEY = "SECRET-TICKFLOW-KEY"


# ----------------------------------------------------------------------
# scrub 函数
# ----------------------------------------------------------------------


def test_scrub_replaces_value() -> None:
    text = f"connect failed: https://api.example.test/v1?apikey={FAKE_KEY}"
    assert scrub(text, (FAKE_KEY,)) == (
        "connect failed: https://api.example.test/v1?apikey=***"
    )
    assert scrub(text, (FAKE_KEY,)) == scrub(text, (FAKE_KEY, CREDENTIAL_MASK))


def test_scrub_empty_values_noop() -> None:
    text = "plain error text"
    assert scrub(text, ()) == text


def test_scrub_multiple_values_serial() -> None:
    text = "user=u1 password=p1 tail"
    assert scrub(text, ("u1", "p1")) == "user=*** password=*** tail"


def test_scrub_skips_blank_values() -> None:
    assert scrub("k1 and k2", ("", "k2")) == "k1 and ***"


# ----------------------------------------------------------------------
# Settings.credential_values()
# ----------------------------------------------------------------------


def test_credential_values_collects_non_empty() -> None:
    settings = make_settings(
        tickflow_api_key="k1",
        tqsdk_auth_password=" p2 ",
        futunn_private_key_path="/some/path.pem",  # 路径字段不纳入
    )
    assert settings.credential_values() == ("k1", "p2")


def test_credential_values_empty_when_no_credentials() -> None:
    assert make_settings().credential_values() == ()


# ----------------------------------------------------------------------
# 服务层 catch-all 日志脱敏
# ----------------------------------------------------------------------


class _ExplodingProvider:
    """抛出含伪凭据异常的 Fake 适配器（bars/quotes 同一错误）。"""

    async def fetch_bars(self, query: object) -> object:
        raise RuntimeError(f"connect failed: https://up.test/x?apikey={FAKE_KEY}")

    async def fetch_quote(self, query: object) -> object:
        raise RuntimeError(f"connect failed: https://up.test/x?apikey={FAKE_KEY}")


class _StubRecord:
    name = "stub-provider"
    adapter = _ExplodingProvider()


class _StubRouter:
    def select(self, query: object) -> object:  # type: ignore[no-untyped-def]
        from tapehub.application.provider_router import ProviderSelection
        from tapehub.domain.models import DataVariant

        return ProviderSelection(record=_StubRecord(), variant=DataVariant.TICKFLOW)  # type: ignore[arg-type]

    def select_quote(self, query: object) -> object:  # type: ignore[no-untyped-def]
        return self.select(query)


def test_bar_query_log_scrubs_credentials(caplog: pytest.LogCaptureFixture) -> None:
    import asyncio

    from tapehub.domain.intervals import Interval
    from tapehub.domain.models import (
        Adjust,
        AssetType,
        BarQuery,
        DataVariant,
        Instrument,
    )

    query = BarQuery(
        asset_type=AssetType.STOCK,
        instrument=Instrument.parse("CN-SSE-600000"),
        interval=Interval.parse("P1D"),
        start="2025-01-01T00:00:00+08:00",
        end="2025-01-05T00:00:00+08:00",
        adjust=Adjust.NONE,
        variant=DataVariant.TICKFLOW,
    )
    service = BarQueryService(_StubRouter(), secret_values=(FAKE_KEY,))
    with caplog.at_level(logging.WARNING, logger="tapehub.application.bar_query_service"):
        with pytest.raises(UpstreamError):
            asyncio.run(service.query(query))
    assert FAKE_KEY not in caplog.text
    assert CREDENTIAL_MASK in caplog.text


def test_quote_query_log_scrubs_credentials(caplog: pytest.LogCaptureFixture) -> None:
    import asyncio

    from tapehub.application.quote_query_service import QuoteQueryService
    from tapehub.domain.models import AssetType, DataVariant, Instrument, QuoteQuery

    query = QuoteQuery(
        asset_type=AssetType.STOCK,
        instrument=Instrument.parse("CN-SSE-600000"),
        variant=DataVariant.TICKFLOW,
    )
    service = QuoteQueryService(_StubRouter(), secret_values=(FAKE_KEY,))
    with caplog.at_level(logging.WARNING, logger="tapehub.application.quote_query_service"):
        with pytest.raises(UpstreamError):
            asyncio.run(service.query(query))
    assert FAKE_KEY not in caplog.text
    assert CREDENTIAL_MASK in caplog.text


def test_stream_log_scrubs_credentials(caplog: pytest.LogCaptureFixture) -> None:
    """流内上游异常日志脱敏（_drive 错误事件化路径）。"""
    import asyncio
    from dataclasses import dataclass

    from tapehub.domain.models import (
        AssetType,
        DataVariant,
        Instrument,
        QuoteQuery,
        QuoteStreamEvent,
        StreamStatusKind,
    )
    from tapehub.providers.registry import ProviderRecord, ResolvedCandidate

    FAKE_WS_KEY = "SECRET-WS-KEY"

    class _ScriptAdapter:
        def stream_quote(self, query: QuoteQuery):
            async def _gen():
                raise RuntimeError(f"ws auth failed token={FAKE_WS_KEY}")
                yield  # pragma: no cover - 使其成为生成器

            return _gen()

    @dataclass
    class _Router:
        variant: DataVariant = DataVariant.TQSDK

        def select_quote(self, query: QuoteQuery) -> ResolvedCandidate:
            record = ProviderRecord(name="stub-provider", manifest=object())
            record.adapter = _ScriptAdapter()
            return ResolvedCandidate(record=record, variant=self.variant)

    service = QuoteStreamService(_Router(), secret_values=(FAKE_WS_KEY,))
    stream = service.open(
        QuoteQuery(
            asset_type=AssetType.STOCK,
            instrument=Instrument.parse("CN-SSE-600000"),
        )
    )

    async def _collect() -> list[QuoteStreamEvent]:
        out: list[QuoteStreamEvent] = []
        async for event in stream.events:
            out.append(event)
        return out

    with caplog.at_level(
        logging.WARNING, logger="tapehub.application.quote_stream_service"
    ):
        events = asyncio.run(_collect())
    assert events == [QuoteStreamEvent(status=StreamStatusKind.UPSTREAM_DISCONNECTED)]
    assert FAKE_WS_KEY not in caplog.text
    assert CREDENTIAL_MASK in caplog.text
