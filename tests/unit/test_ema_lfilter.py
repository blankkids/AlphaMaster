"""tests/unit/test_ema_lfilter.py — EMA lfilter 实现回归测试。

验证 _ema_simple（scipy.signal.lfilter 版）数值等价 exact 递推、因果、
常数保持、out[0]=x[0]、dtype 保持。
"""
from __future__ import annotations

import torch

from model_core.ops import _ema_simple


def _exact(x: torch.Tensor, span: int) -> torch.Tensor:
    """逐 t 递推参考实现（数值基准）。"""
    alpha = 2.0 / (span + 1.0)
    N, T = x.shape
    out = torch.zeros_like(x)
    out[:, 0] = x[:, 0]
    for t in range(1, T):
        out[:, t] = alpha * x[:, t] + (1 - alpha) * out[:, t - 1]
    return out


def test_lfilter_matches_exact() -> None:
    torch.manual_seed(0)
    x = torch.rand(5, 2000, dtype=torch.float32) * 100 + 50
    for span in [3, 12, 26, 50]:
        diff = (_ema_simple(x, span) - _exact(x, span)).abs().max().item()
        assert diff < 1e-4, f"span={span} lfilter 与 exact 偏差 {diff:.2e} 超阈值"


def test_out0_equals_x0() -> None:
    x = torch.tensor([[10.0, 20.0, 30.0, 40.0, 50.0]], dtype=torch.float32)
    assert _ema_simple(x, 3)[0, 0].item() == 10.0


def test_constant_preserved() -> None:
    x = torch.full((2, 100), 7.5, dtype=torch.float32)
    out = _ema_simple(x, 12)
    assert torch.allclose(out, x, atol=1e-5), "常数序列应保持为常数"


def test_causal() -> None:
    """因果：改变未来值不影响已计算的 EMA。"""
    torch.manual_seed(1)
    x = torch.rand(1, 100, dtype=torch.float32)
    x2 = x.clone()
    x2[0, 50:] += 100.0
    o1 = _ema_simple(x, 10)
    o2 = _ema_simple(x2, 10)
    assert torch.allclose(o1[0, :50], o2[0, :50], atol=1e-5)


def test_dtype_preserved() -> None:
    x = torch.rand(3, 50, dtype=torch.float32)
    assert _ema_simple(x, 5).dtype == torch.float32
