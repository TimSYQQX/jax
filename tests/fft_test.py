# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import itertools

import numpy as np

from absl.testing import absltest
from absl.testing import parameterized

import jax
from jax import lax
from jax import numpy as jnp
from jax._src import test_util as jtu

from jax.config import config
config.parse_flags_with_absl()


float_dtypes = jtu.dtypes.floating
inexact_dtypes = jtu.dtypes.inexact
real_dtypes = float_dtypes + jtu.dtypes.integer + jtu.dtypes.boolean
all_dtypes = real_dtypes + jtu.dtypes.complex


def _get_fftn_test_axes(shape):
  axes = [[]]
  ndims = len(shape)
  # XLA's FFT op only supports up to 3 innermost dimensions.
  if ndims <= 3:
    axes.append(None)
  for naxes in range(1, min(ndims, 3) + 1):
    axes.extend(itertools.combinations(range(ndims), naxes))
  for index in range(1, ndims + 1):
    axes.append((-index,))
  return axes

def _get_fftn_test_s(shape, axes):
  s_list = [None]
  if axes is not None:
    s_list.extend(itertools.product(*[[shape[ax]+i for i in range(-shape[ax]+1, shape[ax]+1)] for ax in axes]))
  return s_list

def _get_fftn_func(module, inverse, real):
  if inverse:
    return _irfft_with_zeroed_inputs(module.irfftn) if real else module.ifftn
  else:
    return module.rfftn if real else module.fftn


def _irfft_with_zeroed_inputs(irfft_fun):
  # irfft isn't defined on the full domain of inputs, so in order to have a
  # well defined derivative on the whole domain of the function, we zero-out
  # the imaginary part of the first and possibly the last elements.
  def wrapper(z, axes, s=None):
    return irfft_fun(_zero_for_irfft(z, axes), axes=axes, s=s)
  return wrapper


def _zero_for_irfft(z, axes):
  if axes is not None and not axes:
    return z
  axis = z.ndim - 1 if axes is None else axes[-1]
  try:
    size = z.shape[axis]
  except IndexError:
    return z  # only if axis is invalid, as occurs in some tests
  if size % 2:
    parts = [lax.slice_in_dim(z.real, 0, 1, axis=axis).real,
             lax.slice_in_dim(z.real, 1, size - 1, axis=axis),
             lax.slice_in_dim(z.real, size - 1, size, axis=axis).real]
  else:
    parts = [lax.slice_in_dim(z.real, 0, 1, axis=axis).real,
             lax.slice_in_dim(z.real, 1, size, axis=axis)]
  return jnp.concatenate(parts, axis=axis)


class FftTest(jtu.JaxTestCase):

  def testNotImplemented(self):
    for name in jnp.fft._NOT_IMPLEMENTED:
      func = getattr(jnp.fft, name)
      with self.assertRaises(NotImplementedError):
        func()

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inverse={}_real={}_shape={}_axes={}_s={}".format(
          inverse, real, jtu.format_shape_dtype_string(shape, dtype), axes, s),
       "axes": axes, "shape": shape, "dtype": dtype, "inverse": inverse, "real": real, "s": s}
      for inverse in [False, True]
      for real in [False, True]
      for dtype in (real_dtypes if real and not inverse else all_dtypes)
      for shape in [(10,), (10, 10), (9,), (2, 3, 4), (2, 3, 4, 5)]
      for axes in _get_fftn_test_axes(shape)
      for s in _get_fftn_test_s(shape, axes)))
  @jtu.skip_on_devices("rocm")
  def testFftn(self, inverse, real, shape, dtype, axes, s):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: (rng(shape, dtype),)
    jnp_op = _get_fftn_func(jnp.fft, inverse, real)
    np_op = _get_fftn_func(np.fft, inverse, real)
    jnp_fn = lambda a: jnp_op(a, axes=axes)
    np_fn = lambda a: np_op(a, axes=axes) if axes is None or axes else a
    # Numpy promotes to complex128 aggressively.
    self._CheckAgainstNumpy(np_fn, jnp_fn, args_maker, check_dtypes=False,
                            tol=1e-4)
    self._CompileAndCheck(jnp_fn, args_maker)
    # Test gradient for differentiable types.
    if (config.x64_enabled and
        dtype in (float_dtypes if real and not inverse else inexact_dtypes)):
      # TODO(skye): can we be more precise?
      tol = 0.15
      jtu.check_grads(jnp_fn, args_maker(), order=2, atol=tol, rtol=tol)

  @jtu.skip_on_devices("rocm")
  def testIrfftTranspose(self):
    # regression test for https://github.com/google/jax/issues/6223
    def build_matrix(linear_func, size):
      return jax.vmap(linear_func)(jnp.eye(size, size))

    def func(x):
      return jnp.fft.irfft(jnp.concatenate([jnp.zeros(1), x[:2] + 1j*x[2:]]))

    def func_transpose(x):
      return jax.linear_transpose(func, x)(x)[0]

    matrix = build_matrix(func, 4)
    matrix2 = build_matrix(func_transpose, 4).T
    self.assertAllClose(matrix, matrix2)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inverse={}_real={}".format(inverse, real),
       "inverse": inverse, "real": real}
      for inverse in [False, True]
      for real in [False, True]))
  def testFftnErrors(self, inverse, real):
    rng = jtu.rand_default(self.rng())
    name = 'fftn'
    if real:
      name = 'r' + name
    if inverse:
      name = 'i' + name
    func = _get_fftn_func(jnp.fft, inverse, real)
    self.assertRaisesRegex(
        ValueError,
        "jax.numpy.fft.{} only supports 1D, 2D, and 3D FFTs. "
        "Got axes None with input rank 4.".format(name),
        lambda: func(rng([2, 3, 4, 5], dtype=np.float64), axes=None))
    self.assertRaisesRegex(
        ValueError,
        "jax.numpy.fft.{} does not support repeated axes. Got axes \\[1, 1\\].".format(name),
        lambda: func(rng([2, 3], dtype=np.float64), axes=[1, 1]))
    self.assertRaises(
        ValueError, lambda: func(rng([2, 3], dtype=np.float64), axes=[2]))
    self.assertRaises(
        ValueError, lambda: func(rng([2, 3], dtype=np.float64), axes=[-3]))

  def testFftEmpty(self):
    out = jnp.fft.fft(jnp.zeros((0,), jnp.complex64)).block_until_ready()
    self.assertArraysEqual(jnp.zeros((0,), jnp.complex64), out)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inverse={}_real={}_hermitian={}_shape={}_n={}_axis={}".format(
          inverse, real, hermitian, jtu.format_shape_dtype_string(shape, dtype), n, axis),
       "axis": axis, "shape": shape, "dtype": dtype, "inverse": inverse, "real": real,
       "hermitian": hermitian, "n": n}
      for inverse in [False, True]
      for real in [False, True]
      for hermitian in [False, True]
      for dtype in (real_dtypes if (real and not inverse) or (hermitian and inverse)
                                else all_dtypes)
      for shape in [(10,)]
      for n in [None, 1, 7, 13, 20]
      for axis in [-1, 0]))
  @jtu.skip_on_devices("rocm")
  def testFft(self, inverse, real, hermitian, shape, dtype, n, axis):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: (rng(shape, dtype),)
    name = 'fft'
    if real:
      name = 'r' + name
    elif hermitian:
      name = 'h' + name
    if inverse:
      name = 'i' + name
    jnp_op = getattr(jnp.fft, name)
    np_op = getattr(np.fft, name)
    jnp_fn = lambda a: jnp_op(a, n=n, axis=axis)
    np_fn = lambda a: np_op(a, n=n, axis=axis)
    # Numpy promotes to complex128 aggressively.
    self._CheckAgainstNumpy(np_fn, jnp_fn, args_maker, check_dtypes=False,
                            tol=1e-4)
    self._CompileAndCheck(jnp_op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inverse={}_real={}_hermitian={}".format(inverse, real, hermitian),
       "inverse": inverse, "real": real, "hermitian": hermitian}
      for inverse in [False, True]
      for real in [False, True]
      for hermitian in [False, True]))
  def testFftErrors(self, inverse, real, hermitian):
    rng = jtu.rand_default(self.rng())
    name = 'fft'
    if real:
      name = 'r' + name
    elif hermitian:
      name = 'h' + name
    if inverse:
      name = 'i' + name
    func = getattr(jnp.fft, name)

    self.assertRaisesRegex(
      ValueError,
      f"jax.numpy.fft.{name} does not support multiple axes. "
      f"Please use jax.numpy.fft.{name}n. Got axis = \\[1, 1\\].",
      lambda: func(rng([2, 3], dtype=np.float64), axis=[1, 1])
    )
    self.assertRaisesRegex(
      ValueError,
      f"jax.numpy.fft.{name} does not support multiple axes. "
      f"Please use jax.numpy.fft.{name}n. Got axis = \\(1, 1\\).",
      lambda: func(rng([2, 3], dtype=np.float64), axis=(1, 1))
    )
    self.assertRaises(
        ValueError, lambda: func(rng([2, 3], dtype=np.float64), axis=[2]))
    self.assertRaises(
        ValueError, lambda: func(rng([2, 3], dtype=np.float64), axis=[-3]))

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inverse={}_real={}_shape={}_axes={}".format(
          inverse, real, jtu.format_shape_dtype_string(shape, dtype), axes),
       "axes": axes, "shape": shape, "dtype": dtype, "inverse": inverse, "real": real}
      for inverse in [False, True]
      for real in [False, True]
      for dtype in (real_dtypes if real and not inverse else all_dtypes)
      for shape in [(16, 8, 4, 8), (16, 8, 4, 8, 4)]
      for axes in [(-2, -1), (0, 1), (1, 3), (-1, 2)]))
  @jtu.skip_on_devices("rocm")
  def testFft2(self, inverse, real, shape, dtype, axes):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: (rng(shape, dtype),)
    name = 'fft2'
    if real:
      name = 'r' + name
    if inverse:
      name = 'i' + name
    jnp_op = getattr(jnp.fft, name)
    np_op = getattr(np.fft, name)
    # Numpy promotes to complex128 aggressively.
    self._CheckAgainstNumpy(np_op, jnp_op, args_maker, check_dtypes=False,
                            tol=1e-4)
    self._CompileAndCheck(jnp_op, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
      {"testcase_name": "_inverse={}_real={}".format(inverse, real),
       "inverse": inverse, "real": real}
      for inverse in [False, True]
      for real in [False, True]))
  def testFft2Errors(self, inverse, real):
    rng = jtu.rand_default(self.rng())
    name = 'fft2'
    if real:
      name = 'r' + name
    if inverse:
      name = 'i' + name
    func = getattr(jnp.fft, name)

    self.assertRaisesRegex(
      ValueError,
      "jax.numpy.fft.{} only supports 2 axes. "
      "Got axes = \\[0\\].".format(name),
      lambda: func(rng([2, 3], dtype=np.float64), axes=[0])
    )
    self.assertRaisesRegex(
      ValueError,
      "jax.numpy.fft.{} only supports 2 axes. "
      "Got axes = \\(0, 1, 2\\).".format(name),
      lambda: func(rng([2, 3, 3], dtype=np.float64), axes=(0, 1, 2))
    )
    self.assertRaises(
      ValueError, lambda: func(rng([2, 3], dtype=np.float64), axes=[2, 3]))
    self.assertRaises(
      ValueError, lambda: func(rng([2, 3], dtype=np.float64), axes=[-3, -4]))

  @parameterized.named_parameters(jtu.cases_from_list(
    {"testcase_name": "_size={}_d={}".format(
      jtu.format_shape_dtype_string([size], dtype), d),
      "dtype": dtype, "size": size, "d": d}
    for dtype in all_dtypes
    for size in [9, 10, 101, 102]
    for d in [0.1, 2.]))
  def testFftfreq(self, size, d, dtype):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: (rng([size], dtype),)
    jnp_op = jnp.fft.fftfreq
    np_op = np.fft.fftfreq
    jnp_fn = lambda a: jnp_op(size, d=d)
    np_fn = lambda a: np_op(size, d=d)
    # Numpy promotes to complex128 aggressively.
    self._CheckAgainstNumpy(np_fn, jnp_fn, args_maker, check_dtypes=False,
                            tol=1e-4)
    self._CompileAndCheck(jnp_fn, args_maker)
    # Test gradient for differentiable types.
    if dtype in inexact_dtypes:
      tol = 0.15  # TODO(skye): can we be more precise?
      jtu.check_grads(jnp_fn, args_maker(), order=2, atol=tol, rtol=tol)

  @parameterized.named_parameters(jtu.cases_from_list(
    {"testcase_name": "_n={}".format(n),
     "n": n}
    for n in [[0,1,2]]))
  def testFftfreqErrors(self, n):
    name = 'fftfreq'
    func = jnp.fft.fftfreq
    self.assertRaisesRegex(
      ValueError,
      "The n argument of jax.numpy.fft.{} only takes an int. "
      "Got n = \\[0, 1, 2\\].".format(name),
      lambda: func(n=n)
    )
    self.assertRaisesRegex(
      ValueError,
      "The d argument of jax.numpy.fft.{} only takes a single value. "
      "Got d = \\[0, 1, 2\\].".format(name),
      lambda: func(n=10, d=n)
    )

  @parameterized.named_parameters(jtu.cases_from_list(
    {"testcase_name": "_size={}_d={}".format(
      jtu.format_shape_dtype_string([size], dtype), d),
      "dtype": dtype, "size": size, "d": d}
    for dtype in all_dtypes
    for size in [9, 10, 101, 102]
    for d in [0.1, 2.]))
  def testRfftfreq(self, size, d, dtype):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: (rng([size], dtype),)
    jnp_op = jnp.fft.rfftfreq
    np_op = np.fft.rfftfreq
    jnp_fn = lambda a: jnp_op(size, d=d)
    np_fn = lambda a: np_op(size, d=d)
    # Numpy promotes to complex128 aggressively.
    self._CheckAgainstNumpy(np_fn, jnp_fn, args_maker, check_dtypes=False,
                            tol=1e-4)
    self._CompileAndCheck(jnp_fn, args_maker)
    # Test gradient for differentiable types.
    if dtype in inexact_dtypes:
      tol = 0.15  # TODO(skye): can we be more precise?
      jtu.check_grads(jnp_fn, args_maker(), order=2, atol=tol, rtol=tol)

  @parameterized.named_parameters(jtu.cases_from_list(
    {"testcase_name": "_n={}".format(n),
     "n": n}
    for n in [[0, 1, 2]]))
  def testRfftfreqErrors(self, n):
    name = 'rfftfreq'
    func = jnp.fft.rfftfreq
    self.assertRaisesRegex(
      ValueError,
      "The n argument of jax.numpy.fft.{} only takes an int. "
      "Got n = \\[0, 1, 2\\].".format(name),
      lambda: func(n=n)
    )
    self.assertRaisesRegex(
      ValueError,
      "The d argument of jax.numpy.fft.{} only takes a single value. "
      "Got d = \\[0, 1, 2\\].".format(name),
      lambda: func(n=10, d=n)
    )

  @parameterized.named_parameters(jtu.cases_from_list(
    {"testcase_name": "dtype={}_axes={}".format(
      jtu.format_shape_dtype_string(shape, dtype), axes),
      "dtype": dtype, "shape": shape, "axes": axes}
    for dtype in all_dtypes
    for shape in [[9], [10], [101], [102], [3, 5], [3, 17], [5, 7, 11]]
    for axes in _get_fftn_test_axes(shape)))
  def testFftshift(self, shape, dtype, axes):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: (rng(shape, dtype),)
    jnp_fn = lambda arg: jnp.fft.fftshift(arg, axes=axes)
    np_fn = lambda arg: np.fft.fftshift(arg, axes=axes)
    self._CheckAgainstNumpy(np_fn, jnp_fn, args_maker)

  @parameterized.named_parameters(jtu.cases_from_list(
    {"testcase_name": "dtype={}_axes={}".format(
      jtu.format_shape_dtype_string(shape, dtype), axes),
      "dtype": dtype, "shape": shape, "axes": axes}
    for dtype in all_dtypes
    for shape in [[9], [10], [101], [102], [3, 5], [3, 17], [5, 7, 11]]
    for axes in _get_fftn_test_axes(shape)))
  def testIfftshift(self, shape, dtype, axes):
    rng = jtu.rand_default(self.rng())
    args_maker = lambda: (rng(shape, dtype),)
    jnp_fn = lambda arg: jnp.fft.ifftshift(arg, axes=axes)
    np_fn = lambda arg: np.fft.ifftshift(arg, axes=axes)
    self._CheckAgainstNumpy(np_fn, jnp_fn, args_maker)

if __name__ == "__main__":
  absltest.main(testLoader=jtu.JaxTestLoader())
