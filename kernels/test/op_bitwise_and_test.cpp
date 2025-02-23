/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the BSD-style license found in the
 * LICENSE file in the root directory of this source tree.
 */

#include <executorch/kernels/test/FunctionHeaderWrapper.h> // Declares the operator
#include <executorch/kernels/test/TestUtil.h>
#include <executorch/runtime/core/exec_aten/exec_aten.h>
#include <executorch/runtime/core/exec_aten/testing_util/tensor_factory.h>
#include <executorch/runtime/core/exec_aten/testing_util/tensor_util.h>

#include <gtest/gtest.h>

using namespace ::testing;
using exec_aten::Scalar;
using exec_aten::ScalarType;
using exec_aten::Tensor;
using torch::executor::testing::TensorFactory;

Tensor& op_bitwise_and_scalar_out(
    const Tensor& self,
    const Scalar& other,
    Tensor& out) {
  exec_aten::RuntimeContext context{};
  return torch::executor::aten::bitwise_and_outf(context, self, other, out);
}

Tensor& op_bitwise_and_tensor_out(
    const Tensor& self,
    const Tensor& other,
    Tensor& out) {
  exec_aten::RuntimeContext context{};
  return torch::executor::aten::bitwise_and_outf(context, self, other, out);
}
