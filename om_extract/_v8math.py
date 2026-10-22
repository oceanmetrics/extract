"""`Math.sin` as V8 computes it, so turf's `area()` can be reproduced to the last bit.

V8's `Math.sin` is fdlibm (`src/base/ieee754.cc`: `sin`, `__kernel_sin`, `__kernel_cos`, `__ieee754_rem_pio2`),
compiled by clang with floating-point contraction on, so on arm64 every `a + b * c` in the kernels is one fused
multiply-add. Python's `math.sin` (the platform libm) differs from it in the last bit for about 3.6% of
latitudes, and plain fdlibm without the fused multiply-adds for about 0.9%; this port, with them, matched
V8 (node 26.10, arm64) on 4,000,000 of 4,000,000 latitudes in [-90, 90] degrees (checked 2026-10-22).

Only |x| < 3*pi/4 is implemented (the `n = +-1` branch of `__ieee754_rem_pio2`): a latitude in radians never
leaves it, and anything outside raises.
"""
from __future__ import annotations

import math
import struct

def fma_exact(a: float, b: float, c: float) -> float:
  """a * b + c rounded once (python 3.12 has no `math.fma`): the exact value as integers over a power of two,
  then CPython's correctly rounded int / int division. The sign of a zero result is not kept (no caller needs it)."""
  an, ad = a.as_integer_ratio()
  bn, bd = b.as_integer_ratio()
  cn, cd = c.as_integer_ratio()
  return (an * bn * cd + cn * ad * bd) / (ad * bd * cd)


_fma = getattr(math, "fma", fma_exact)  # math.fma: python >= 3.13

_S1 = -1.66666666666666324348e-01
_S2 = 8.33333333332248946124e-03
_S3 = -1.98412698298579493134e-04
_S4 = 2.75573137070700676789e-06
_S5 = -2.50507602534068634195e-08
_S6 = 1.58969099521155010221e-10
_C1 = 4.16666666666666019037e-02
_C2 = -1.38888888888741095749e-03
_C3 = 2.48015872894767294178e-05
_C4 = -2.75573143513906633035e-07
_C5 = 2.08757232129817482790e-09
_C6 = -1.13596475577881948265e-11
_PIO2_1 = 1.57079632673412561417e+00
_PIO2_1T = 6.07710050650619224932e-11
_PIO2_2 = 6.07710050630396597660e-11
_PIO2_2T = 2.02226624879595063154e-21

_pack = struct.Struct("<d").pack
_unpack_q = struct.Struct("<q").unpack
_unpack_d = struct.Struct("<d").unpack
_pack_q = struct.Struct("<q").pack


def _high(x: float) -> int:
  return (_unpack_q(_pack(x))[0] >> 32) & 0x7FFFFFFF


def _kernel_sin(x: float) -> float:
  # __kernel_sin(x, y = 0, iy = 0)
  if _high(x) < 0x3E400000 and int(x) == 0:
    return x
  z = x * x
  v = z * x
  r = _fma(z, _fma(z, _fma(z, _fma(z, _S6, _S5), _S4), _S3), _S2)
  return _fma(v, _fma(z, r, _S1), x)


def _kernel_cos(x: float, y: float) -> float:
  ix = _high(x)
  if ix < 0x3E400000 and int(x) == 0:
    return 1.0
  z = x * x
  r = z * _fma(z, _fma(z, _fma(z, _fma(z, _fma(z, _C6, _C5), _C4), _C3), _C2), _C1)
  t = _fma(z, r, -(x * y))  # z * r - x * y, contracted on the first product
  if ix < 0x3FD33333:
    return 1.0 - (0.5 * z - t)  # 0.5 * z is exact, so contraction cannot change it
  if ix > 0x3FE90000:
    qx = 0.28125
  else:
    qx = _unpack_d(_pack_q((ix - 0x00200000) << 32))[0]
  hz = 0.5 * z - qx
  a = 1.0 - qx
  return a - (hz - t)


def v8_sin(x: float) -> float:
  """`Math.sin(x)` bit for bit as V8 on arm64 returns it, for |x| < 3*pi/4."""
  ix = _high(x)
  if ix <= 0x3FE921FB:  # |x| ~< pi/4
    return _kernel_sin(x)
  if ix >= 0x4002D97C:
    raise ValueError(f"v8_sin: |x| = {abs(x)} is outside the ported range (< 3*pi/4)")
  if x > 0:
    z = x - _PIO2_1
    if ix != 0x3FF921FB:
      y0 = z - _PIO2_1T
      y1 = (z - y0) - _PIO2_1T
    else:  # near pi/2: 33+33+53 bit pi
      z -= _PIO2_2
      y0 = z - _PIO2_2T
      y1 = (z - y0) - _PIO2_2T
    return _kernel_cos(y0, y1)
  z = x + _PIO2_1
  if ix != 0x3FF921FB:
    y0 = z + _PIO2_1T
    y1 = (z - y0) + _PIO2_1T
  else:
    z += _PIO2_2
    y0 = z + _PIO2_2T
    y1 = (z - y0) + _PIO2_2T
  return -_kernel_cos(y0, y1)
