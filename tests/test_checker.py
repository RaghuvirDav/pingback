"""White-box tests for the HTTP checker service: status classification, errors, timeouts."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_checker_classifies_2xx_as_up(app_ctx, httpx_mock):
    from pingback.services.checker import check_url

    httpx_mock.add_response(url="https://ok.example.com/", status_code=200, text="ok")
    result = await check_url("https://ok.example.com/")
    assert result.status == "up"
    assert result.status_code == 200
    assert result.response_time_ms is not None and result.response_time_ms >= 0
    assert result.error is None


@pytest.mark.asyncio
async def test_checker_classifies_5xx_as_down(app_ctx, httpx_mock):
    from pingback.services.checker import check_url

    httpx_mock.add_response(url="https://bad.example.com/", status_code=503, text="nope")
    result = await check_url("https://bad.example.com/")
    assert result.status == "down"
    assert result.status_code == 503


@pytest.mark.asyncio
async def test_checker_classifies_network_error_as_down(app_ctx, httpx_mock):
    import httpx

    from pingback.services.checker import check_url

    httpx_mock.add_exception(httpx.ConnectError("refused"))
    result = await check_url("https://gone.example.com/")
    # Connect errors are classified as `error` (vs. `down` for HTTP 5xx).
    assert result.status in ("down", "error")
    assert result.status_code is None
    assert result.error is not None
