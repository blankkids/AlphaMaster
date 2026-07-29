"""CUDA-friendly causal EMA regression tests."""

from __future__ import annotations

import pytest
import torch

from model_core.config import ModelConfig
from model_core.ops import _ema_kernel, _ema_simple


@pytest.mark.parametrize("span", [5, 20])
@pytest.mark.parametrize("length", [1, 2, 37, 256, 4096])
def test_causal_conv_matches_exact_recurrence(span: int, length: int) -> None:
    generator = torch.Generator().manual_seed(20260729 + span + length)
    values = torch.randn(3, length, generator=generator)

    actual = _ema_simple(values, span)
    expected = _ema_simple(values, span, exact=True)

    torch.testing.assert_close(actual, expected, rtol=2e-6, atol=5e-6)


@pytest.mark.parametrize("span", [5, 20])
def test_causal_conv_preserves_constant_series(span: int) -> None:
    values = torch.full((2, 500), 3.25)

    actual = _ema_simple(values, span)

    torch.testing.assert_close(
        actual,
        values,
        rtol=1e-6,
        atol=1e-6,
    )


@pytest.mark.parametrize("span", [5, 20])
def test_future_values_cannot_change_past_ema(span: int) -> None:
    torch.manual_seed(11)
    values = torch.randn(2, 600)
    changed = values.clone()
    changed[:, 300:] = changed[:, 300:] * -17.0 + 9.0

    original = _ema_simple(values, span)
    mutated = _ema_simple(changed, span)

    torch.testing.assert_close(original[:, :300], mutated[:, :300], rtol=0, atol=0)


@pytest.mark.parametrize("span", [5, 20])
def test_cached_kernel_is_normalized_on_current_device(span: int) -> None:
    values = torch.zeros(1, 10, device=ModelConfig.DEVICE)

    kernel = _ema_kernel(values, span)

    assert kernel.device == values.device
    assert kernel.dtype == values.dtype
    torch.testing.assert_close(
        kernel.sum(),
        torch.ones((), device=values.device),
        rtol=1e-6,
        atol=1e-6,
    )


def test_model_device_automatically_tracks_cuda_availability() -> None:
    expected = "cuda" if torch.cuda.is_available() else "cpu"
    assert ModelConfig.DEVICE.type == expected


@pytest.mark.skipif(not torch.cuda.is_available(), reason="CUDA is unavailable")
@pytest.mark.parametrize("span", [5, 20])
def test_cuda_ema_matches_cpu(span: int) -> None:
    torch.manual_seed(23)
    cpu_values = torch.randn(4, 3500)

    cpu_result = _ema_simple(cpu_values, span)
    cuda_result = _ema_simple(cpu_values.cuda(), span).cpu()

    torch.testing.assert_close(cuda_result, cpu_result, rtol=1e-5, atol=1e-5)
