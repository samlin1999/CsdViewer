"""Cocos2d-x tweenfunc 緩動曲線。

EasingData Type 的數值直接對應 cocos2d-x `tweenfunc::TweenType` 的列舉順序，
所以這裡照那個順序實作即可。-1 是自訂貝茲曲線（用 <Points> 描述）。
"""
from __future__ import annotations

import math

_PI = math.pi


def _sine_in(t):     return -math.cos(t * _PI / 2) + 1
def _sine_out(t):    return math.sin(t * _PI / 2)
def _sine_in_out(t): return -0.5 * (math.cos(_PI * t) - 1)


def _pow_in(n):      return lambda t: t ** n
def _pow_out(n):     return lambda t: 1 - (1 - t) ** n


def _pow_in_out(n):
    def f(t):
        t *= 2
        if t < 1:
            return 0.5 * t ** n
        return 1 - 0.5 * abs((2 - t) ** n)
    return f


def _expo_in(t):     return 0.0 if t == 0 else 2 ** (10 * (t - 1)) - 0.001
def _expo_out(t):    return 1.0 if t == 1 else -(2 ** (-10 * t)) + 1


def _expo_in_out(t):
    if t in (0.0, 1.0):
        return t
    t *= 2
    if t < 1:
        return 0.5 * 2 ** (10 * (t - 1))
    return 0.5 * (-(2 ** (-10 * (t - 1))) + 2)


def _circ_in(t):     return -(math.sqrt(max(0.0, 1 - t * t)) - 1)
def _circ_out(t):    return math.sqrt(max(0.0, 1 - (t - 1) ** 2))


def _circ_in_out(t):
    t *= 2
    if t < 1:
        return -0.5 * (math.sqrt(max(0.0, 1 - t * t)) - 1)
    t -= 2
    return 0.5 * (math.sqrt(max(0.0, 1 - t * t)) + 1)


def _elastic_in(t, period=0.3):
    if t in (0.0, 1.0):
        return t
    s = period / 4
    t -= 1
    return -(2 ** (10 * t)) * math.sin((t - s) * 2 * _PI / period)


def _elastic_out(t, period=0.3):
    if t in (0.0, 1.0):
        return t
    s = period / 4
    return 2 ** (-10 * t) * math.sin((t - s) * 2 * _PI / period) + 1


def _elastic_in_out(t, period=0.3):
    if t in (0.0, 1.0):
        return t
    t *= 2
    s = period / 4
    t -= 1
    if t < 0:
        return -0.5 * 2 ** (10 * t) * math.sin((t - s) * 2 * _PI / period)
    return 2 ** (-10 * t) * math.sin((t - s) * 2 * _PI / period) * 0.5 + 1


_BACK = 1.70158


def _back_in(t):     return t * t * ((_BACK + 1) * t - _BACK)
def _back_out(t):
    t -= 1
    return t * t * ((_BACK + 1) * t + _BACK) + 1


def _back_in_out(t):
    s = _BACK * 1.525
    t *= 2
    if t < 1:
        return 0.5 * (t * t * ((s + 1) * t - s))
    t -= 2
    return 0.5 * (t * t * ((s + 1) * t + s) + 2)


def _bounce_out(t):
    if t < 1 / 2.75:
        return 7.5625 * t * t
    if t < 2 / 2.75:
        t -= 1.5 / 2.75
        return 7.5625 * t * t + 0.75
    if t < 2.5 / 2.75:
        t -= 2.25 / 2.75
        return 7.5625 * t * t + 0.9375
    t -= 2.625 / 2.75
    return 7.5625 * t * t + 0.984375


def _bounce_in(t):   return 1 - _bounce_out(1 - t)


def _bounce_in_out(t):
    if t < 0.5:
        return _bounce_in(t * 2) * 0.5
    return _bounce_out(t * 2 - 1) * 0.5 + 0.5


# 索引即 EasingData Type
_TABLE = [
    lambda t: t,                                            # 0  Linear
    _sine_in, _sine_out, _sine_in_out,                      # 1-3
    _pow_in(2), _pow_out(2), _pow_in_out(2),                # 4-6   Quad
    _pow_in(3), _pow_out(3), _pow_in_out(3),                # 7-9   Cubic
    _pow_in(4), _pow_out(4), _pow_in_out(4),                # 10-12 Quart
    _pow_in(5), _pow_out(5), _pow_in_out(5),                # 13-15 Quint
    _expo_in, _expo_out, _expo_in_out,                      # 16-18
    _circ_in, _circ_out, _circ_in_out,                      # 19-21
    _elastic_in, _elastic_out, _elastic_in_out,             # 22-24
    _back_in, _back_out, _back_in_out,                      # 25-27
    _bounce_in, _bounce_out, _bounce_in_out,                # 28-30
]

NAMES = [
    "Linear",
    "Sine_EaseIn", "Sine_EaseOut", "Sine_EaseInOut",
    "Quad_EaseIn", "Quad_EaseOut", "Quad_EaseInOut",
    "Cubic_EaseIn", "Cubic_EaseOut", "Cubic_EaseInOut",
    "Quart_EaseIn", "Quart_EaseOut", "Quart_EaseInOut",
    "Quint_EaseIn", "Quint_EaseOut", "Quint_EaseInOut",
    "Expo_EaseIn", "Expo_EaseOut", "Expo_EaseInOut",
    "Circ_EaseIn", "Circ_EaseOut", "Circ_EaseInOut",
    "Elastic_EaseIn", "Elastic_EaseOut", "Elastic_EaseInOut",
    "Back_EaseIn", "Back_EaseOut", "Back_EaseInOut",
    "Bounce_EaseIn", "Bounce_EaseOut", "Bounce_EaseInOut",
]


def name(easing_type: int) -> str:
    if 0 <= easing_type < len(NAMES):
        return NAMES[easing_type]
    return "Custom" if easing_type < 0 else f"Unknown({easing_type})"


def _bezier_y_at_x(points: list[tuple[float, float]], x: float) -> float:
    """在三次貝茲曲線上求 x 對應的 y（牛頓法 + 二分保底）。"""
    if len(points) < 4:
        return x
    p0, p1, p2, p3 = points[0], points[1], points[2], points[3]

    def bez(a, b, c, d, t):
        mt = 1 - t
        return mt ** 3 * a + 3 * mt * mt * t * b + 3 * mt * t * t * c + t ** 3 * d

    lo, hi = 0.0, 1.0
    for _ in range(24):
        mid = (lo + hi) / 2
        if bez(p0[0], p1[0], p2[0], p3[0], mid) < x:
            lo = mid
        else:
            hi = mid
    return bez(p0[1], p1[1], p2[1], p3[1], (lo + hi) / 2)


def apply(t: float, easing_type: int, points: list[tuple[float, float]] | None = None) -> float:
    """把線性進度 t（0~1）換成緩動後的進度。"""
    t = min(1.0, max(0.0, t))
    if points:
        return _bezier_y_at_x(points, t)
    if 0 <= easing_type < len(_TABLE):
        try:
            return _TABLE[easing_type](t)
        except (ValueError, OverflowError):
            return t
    return t
