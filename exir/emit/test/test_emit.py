# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pye-strict

import typing
import unittest
from typing import List, Optional, Tuple

import executorch.exir as exir

import executorch.exir.schema as schema
import executorch.exir.tests.models as models
import torch
from executorch.exir import EdgeCompileConfig, ExecutorchProgramManager, to_edge
from executorch.exir.backend.backend_api import to_backend
from executorch.exir.backend.backend_details import BackendDetails, PreprocessResult
from executorch.exir.emit import emit_program  # noqa
from executorch.exir.passes.const_prop_pass import ConstPropPass
from executorch.exir.passes.sym_shape_eval_pass import ConstraintBasedSymShapeEvalPass
from executorch.exir.print_program import pretty_print, print_program  # noqa
from executorch.exir.schema import (
    Bool,
    DelegateCall,
    EValue,
    ExecutionPlan,
    Int,
    IntList,
    JumpFalseCall,
    KernelCall,
    KernelTypes,
    MoveCall,
    Null,
    Program,
    String,
    Tensor,
)
from executorch.exir.tests.common import register_additional_test_aten_ops
from executorch.exir.tests.models import Mul
from functorch.experimental import control_flow
from torch import nn

from torch.export import Dim, export


class WrapperModule(torch.nn.Module):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, *args, **kwargs):
        return self.fn(*args, **kwargs)


class TestEmit(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        register_additional_test_aten_ops()

    def setUp(self) -> None:
        self.compile_config = EdgeCompileConfig(_check_ir_validity=False)

    def check_tensor_buffer_loc(
        self,
        value_index: int,
        values: List[EValue],
        exp_buffer_idx: int,
        exp_mem_id: Optional[int],
        exp_mem_offset: Optional[int],
    ) -> None:
        value = typing.cast(schema.Tensor, values[value_index].val)
        self.assertIsInstance(value, schema.Tensor)

        self.assertEqual(value.constant_buffer_idx, exp_buffer_idx)

        if not value.allocation_info:
            self.assertIsNone(exp_mem_id)
            self.assertIsNone(exp_mem_offset)
        else:
            self.assertEqual(value.allocation_info.memory_id, exp_mem_id)
            assert value.allocation_info
            self.assertEqual(value.allocation_info.memory_offset, exp_mem_offset)

    def count_node(self, graph_module: torch.fx.GraphModule, opname: str) -> int:
        return [
            node.target._overloadpacket._qualified_op_name
            for node in graph_module.graph.nodes
            if node.op == "call_function"
        ].count(opname)

    def run_dce(self, graph_module: torch.fx.GraphModule) -> None:
        for submodule in graph_module.modules():
            self.assertIsInstance(submodule, torch.fx.GraphModule)
            typing.cast(torch.fx.GraphModule, submodule).graph.eliminate_dead_code()

    def check_value_types(self, values: List[EValue]) -> None:
        for value in values:
            self.assertTrue(type(value.val) in KernelTypes.__args__)

    def count_move_instructions(self, program: Program) -> int:
        instructions = program.execution_plan[0].chains[0].instructions
        assert instructions is not None
        res = 0
        for instr in instructions:
            if isinstance(instr.instr_args, MoveCall):
                res += 1
        return res

    def test_basic_api(self) -> None:
        class Foo(torch.nn.Module):
            def forward(self, x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
                return x * y + x

        f = Foo()

        program = (
            to_edge(
                export(
                    f,
                    (torch.ones(3, 2), torch.zeros(3, 2)),
                )
            )
            .to_executorch()
            .executorch_program
        )
        exec_plan = program.execution_plan[0]
        ops = exec_plan.operators
        for op in ops:
            self.assertEqual(op.overload, "out")

        self.assertEqual(ops[0].name, "aten::mul")
        self.assertEqual(ops[1].name, "aten::add")

        self.assertEqual(len(exec_plan.inputs), 2)
        self.assertEqual(len(exec_plan.outputs), 1)

        self.assertEqual(exec_plan.inputs[0], 0)
        self.assertEqual(exec_plan.outputs[0], 3)

    def test_basic_end_to_end(self) -> None:
        f = models.BasicSinMax()
        program = (
            to_edge(export(f, f.get_random_inputs())).to_executorch().executorch_program
        )
        exec_plan = program.execution_plan[0]
        ops = exec_plan.operators
        for op in ops:
            self.assertIn(op.overload, {"out", "unary_out"})

        self.assertEqual(ops[0].name, "aten::sin")

        self.assertEqual(len(exec_plan.inputs), 1)
        self.assertEqual(len(exec_plan.outputs), 1)

        self.assertEqual(exec_plan.inputs[0], 0)
        self.assertEqual(exec_plan.outputs[0], 1)

    def test_nested_return(self) -> None:
        class Foo(torch.nn.Module):
            def forward(
                self, x: torch.Tensor
            ) -> Tuple[torch.Tensor, torch.Tensor, List[torch.Tensor]]:
                return (
                    torch.Tensor(1),
                    torch.Tensor(2),
                    [torch.sin(x).max(), torch.cos(x).max()],
                )

        f = Foo()

        x = (torch.randn(100),)
        program = to_edge(export(f, x)).to_executorch().executorch_program
        exec_plan = program.execution_plan[0]
        self.assertEqual(len(exec_plan.outputs), 4)
        self.assertEqual(len(exec_plan.inputs), 1)

        self.assertEqual(
            program.execution_plan[0].container_meta_type.encoded_out_str,
            "T3#1#1#2($,$,L2#1#1($,$))",
        )

        self.assertEqual(
            program.execution_plan[0].container_meta_type.encoded_inp_str,
            "T2#1#0(T1#1($),D0())",
        )

    def test_buffers_with_perfect_alignment(self) -> None:
        class Foo(torch.nn.Module):
            def forward(self, x: torch.Tensor) -> torch.Tensor:
                return torch.ones(100) + x + (torch.ones(100) * 2)

        f = Foo()

        program = (
            to_edge(export(f, (torch.randn(100),)))
            .transform(
                [
                    ConstPropPass(),
                ]
            )
            .to_executorch()
            .executorch_program
        )
        self.assertEqual(len(program.constant_buffer), 3)
        instructions = program.execution_plan[0].chains[0].instructions
        values = program.execution_plan[0].values

        # first arg to first torch_add is a constant tensor
        self.check_tensor_buffer_loc(
            instructions[0].instr_args.args[0], values, 1, None, None  # pyre-ignore[16]
        )
        # second arg to first torch_add is an input tensor
        self.check_tensor_buffer_loc(
            instructions[0].instr_args.args[1], values, 0, 1, 0  # pyre-ignore[16]
        )
        # output of first torch_add is a dynamic tensor
        self.check_tensor_buffer_loc(
            instructions[0].instr_args.args[3], values, 0, 1, 400  # pyre-ignore[16]
        )
        # first arg to second torch_add is a dynamic tensor
        self.check_tensor_buffer_loc(
            instructions[1].instr_args.args[0], values, 0, 1, 400  # pyre-ignore[16]
        )
        # second arg to second torch_add is a constant tensor
        self.check_tensor_buffer_loc(
            instructions[1].instr_args.args[1], values, 2, None, None  # pyre-ignore[16]
        )
        # output of second torch_add is a dynamic tensor
        self.check_tensor_buffer_loc(
            instructions[1].instr_args.args[3], values, 0, 1, 0  # pyre-ignore[16]
        )

    def test_inplace_ops(self) -> None:
        class Foo(torch.nn.Module):
            def forward(self, x: torch.Tensor) -> torch.Tensor:
                y = torch.sin(x)
                z = y.view(100)
                torch.relu_(z)
                return z.max()

        f = Foo()

        inputs = (torch.ones((10, 10)),)
        edge = to_edge(export(f, inputs))

        removed_ops = ["aten::relu_", "aten::view"]
        expected_ops = ["aten::sin", "aten::relu", "aten::max", "aten::view_copy"]

        for opname in removed_ops:
            self.assertEqual(
                self.count_node(edge.exported_program().graph_module, opname), 0
            )
        for opname in expected_ops:
            self.assertTrue(
                self.count_node(edge.exported_program().graph_module, opname) >= 1
            )

        program = edge.to_executorch().executorch_program
        for opname in removed_ops:
            self.assertTrue(
                all(op.name != opname for op in program.execution_plan[0].operators)
            )
        for opname in expected_ops:
            self.assertTrue(
                any(op.name == opname for op in program.execution_plan[0].operators)
            )

    def test_operators_unique(self) -> None:
        class OpRepeatedModule(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.a = torch.ones(2, 2)
                self.b = 2 * torch.ones(2, 2)

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                for _ in range(10):
                    z = self.a * x
                    y = z + self.b
                return y

        model = OpRepeatedModule()

        inputs = (torch.ones(2, 2),)

        program = to_edge(export(model, inputs)).to_executorch().executorch_program

        self.assertEqual(len(program.execution_plan[0].operators), 2)

    def test_list_type(self) -> None:
        """Tests that the types of lists are correctly found"""

        class Foo(torch.nn.Module):
            def forward(self, x: torch.Tensor) -> torch.Tensor:
                return torch.permute(x, (2, 0, 1))

        f = Foo()

        program = (
            to_edge(export(f, (torch.randn(2, 3, 5),)))
            .to_executorch()
            .executorch_program
        )
        exir.print_program.pretty_print(program)

        deboxed_int_list = []
        for item in program.execution_plan[0].values[5].val.items:  # pyre-ignore[16]
            deboxed_int_list.append(
                program.execution_plan[0].values[item].val.int_val  # pyre-ignore[16]
            )

        self.assertEqual(IntList(deboxed_int_list), IntList([2, 0, 1]))

    def test_kwargs1(self) -> None:
        """Tests that the kwargs are placed in the order specified by
        native_functions.yaml
        """

        class Foo(torch.nn.Module):
            def forward(self, x: torch.Tensor) -> torch.Tensor:
                batch1 = torch.randn(10, 3, 4)
                batch2 = torch.randn(10, 4, 5)
                return torch.addbmm(x, batch1, batch2, alpha=2, beta=3)

        f = Foo()

        program = (
            to_edge(export(f, (torch.randn(3, 5),))).to_executorch().executorch_program
        )
        # The value for beta should appear before alpha
        self.assertEqual(program.execution_plan[0].values[12].val, Int(3))
        self.assertEqual(program.execution_plan[0].values[13].val, Int(2))

    def test_kwargs2(self) -> None:
        """Tests that the kwargs are placed in the order specified by
        native_functions.yaml
        """

        class Foo(torch.nn.Module):
            def forward(self, x: torch.Tensor) -> torch.Tensor:
                values = torch.randn(3, 2)
                return torch.searchsorted(x, values, side="right", right=True)

        f = Foo()

        x, _ = torch.sort(torch.randn(3, 4))
        program = to_edge(export(f, (x,))).to_executorch().executorch_program
        # The value for right should appear before side
        self.assertEqual(program.execution_plan[0].values[6].val, Bool(False))
        self.assertEqual(program.execution_plan[0].values[7].val, Bool(True))
        self.assertEqual(program.execution_plan[0].values[8].val, String("right"))
        self.assertEqual(program.execution_plan[0].values[9].val, Null())

    def _assertCallLength(self, program: Program, idx: int, expected_len: int) -> None:
        instr_args = program.execution_plan[0].chains[0].instructions[idx].instr_args

        if isinstance(instr_args, KernelCall) or isinstance(instr_args, DelegateCall):
            self.assertEqual(len(instr_args.args), expected_len)
        else:
            self.assertTrue(False)

    def test_out(self) -> None:
        class Foo(torch.nn.Module):
            def forward(self, x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
                z = y.clone()
                return torch.mul(x, y, out=z)

        f = Foo()

        program = (
            to_edge(export(f, (torch.ones(3), torch.ones(3))))
            .to_executorch()
            .executorch_program
        )

        self.assertEqual(len(program.execution_plan[0].chains[0].instructions), 1)
        self._assertCallLength(program, 0, 4)

    def test_model_out(self) -> None:
        class Module_out(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.a = 3 * torch.ones(2, 2, dtype=torch.int32)
                self.b = 2 * torch.ones(2, 2, dtype=torch.int32)

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                z = x.clone()
                torch.mul(self.a, x, out=z)
                y = x.clone()
                torch.add(z, self.b, alpha=2, out=y)
                return y

        model_out = Module_out()

        inputs = (torch.ones(2, 2, dtype=torch.int32),)

        # Trace to FX Graph.
        program = to_edge(export(model_out, inputs)).to_executorch().executorch_program

        self.assertEqual(len(program.execution_plan[0].chains[0].instructions), 2)
        self._assertCallLength(program, 0, 4)
        self._assertCallLength(program, 1, 5)

    def test_stacktrace(self) -> None:
        def f(x: torch.Tensor) -> torch.Tensor:
            return torch.mul(x, torch.randn(3, 2))

        def g(x: torch.Tensor) -> torch.Tensor:
            return torch.sin(f(x))

        class Foo(torch.nn.Module):
            def forward(self, x: torch.Tensor) -> torch.Tensor:
                return torch.add(g(x), torch.randn(3, 2))

        h = Foo()

        x = (torch.randn(3, 2),)
        exec_prog = to_edge(export(h, x)).to_executorch(
            exir.ExecutorchBackendConfig(emit_stacktrace=True)
        )
        program = exec_prog.executorch_program

        # Check the mul operator's stack trace contains f -> g -> h
        self.assertTrue(
            "return torch.mul(x, torch.randn(3, 2))"
            in program.execution_plan[0]  # pyre-ignore[16]
            .chains[0]
            .stacktrace[1]
            .items[-1]
            .context
        )
        self.assertEqual(
            program.execution_plan[0].chains[0].stacktrace[1].items[-1].name, "f"
        )
        self.assertEqual(
            program.execution_plan[0].chains[0].stacktrace[1].items[-2].name, "g"
        )
        self.assertEqual(
            program.execution_plan[0].chains[0].stacktrace[1].items[-3].name, "forward"
        )

        # Check the sin operator's stack trace contains g -> h
        self.assertEqual(
            program.execution_plan[0].chains[0].stacktrace[2].items[-1].name, "g"
        )
        self.assertEqual(
            program.execution_plan[0].chains[0].stacktrace[2].items[-2].name, "forward"
        )

    def test_stacktrace_off(self) -> None:
        class Foo(torch.nn.Module):
            def forward(self, x: torch.Tensor) -> torch.Tensor:
                return torch.mul(x, torch.randn(3, 2))

        f = Foo()

        class Goo(torch.nn.Module):
            def forward(self, x: torch.Tensor) -> torch.Tensor:
                return torch.sin(f(x))

        g = Goo()

        class Hoo(torch.nn.Module):
            def forward(self, x: torch.Tensor) -> torch.Tensor:
                return torch.add(g(x), torch.randn(3, 2))

        h = Hoo()

        x = (torch.randn(3, 2),)
        program = to_edge(export(h, x)).to_executorch().executorch_program

        # Check the stacktrace is None since we did not specify to get the stacktrace
        self.assertTrue(program.execution_plan[0].chains[0].stacktrace is None)

    def test_positional_argument_default_value(self) -> None:
        class Foo(torch.nn.Module):
            def forward(self, x: torch.Tensor, n: torch.Tensor) -> torch.Tensor:
                z = torch.ones(6, 2)
                return torch.ops.aten.cat.out((x, n), out=z)

        f = Foo()

        x = torch.randn(3, 2)
        program = (
            to_edge(export(f, (x, x)))
            # .to_edge(self.compile_config)  # TODO(larryliu): fix cat
            .to_executorch().executorch_program
        )

        self.assertEqual(len(program.execution_plan[0].chains[0].instructions), 1)
        self._assertCallLength(program, 0, 4)

    def test_emit_multiple_out(self) -> None:
        class Foo(torch.nn.Module):
            def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
                return torch.topk(x, 5)

        f = Foo()

        x = (torch.randn(10),)
        program = to_edge(export(f, x)).to_executorch().executorch_program
        self._assertCallLength(program, 0, 8)

    # Non contiguous tensors are not supported in ExecuTorch
    @unittest.expectedFailure
    def test_emit_layout(self) -> None:
        class Foo(torch.nn.Module):
            def forward(self, x: torch.Tensor) -> torch.Tensor:
                return torch.ones_like(x)

        f = Foo()

        x = (torch.randn(3, 2),)
        program = to_edge(export(f, x)).to_executorch().executorch_program

        vals = program.execution_plan[0].values
        for val in vals:
            v = val.val
            if isinstance(v, Tensor):
                self.assertEqual(v.layout, 0)

    def test_optional_tensor_list(self) -> None:
        class Foo(torch.nn.Module):
            def forward(self, x: torch.Tensor) -> torch.Tensor:
                a = torch.nonzero(x)
                b = torch.ops.aten.index.Tensor(x, [a])
                return b

        f = Foo()

        x = (torch.randn(3, 2),)
        program = (
            to_edge(
                export(f, x),
                compile_config=exir.EdgeCompileConfig(_check_ir_validity=False),
            )
            .to_executorch()
            .executorch_program
        )
        self._assertCallLength(program, 0, 3)
        self._assertCallLength(program, 1, 4)

    def test_optional_float_list(self) -> None:
        class M(torch.nn.Module):
            def forward(self, x):
                return torch.nn.functional.interpolate(x, scale_factor=2)

        x = (torch.randn(1, 1, 2, 2),)
        program = (
            to_edge(export(M(), x))
            .transform([ConstPropPass()])
            .to_executorch()
            .executorch_program
        )
        self.assertIsInstance(
            program.execution_plan[0].values[4].val, schema.OptionalTensorList
        )

    def test_emit_cond(self) -> None:
        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()

            def forward(self, pred, x):
                def true_fn(y: torch.Tensor) -> torch.Tensor:
                    y = y + y
                    y = torch.mm(y, y)
                    return y

                def false_fn(y: torch.Tensor) -> torch.Tensor:
                    return torch.mm(y, y)

                ret = control_flow.cond(pred, true_fn, false_fn, [x])
                return ret

        module = to_edge(export(M(), (torch.tensor(True), torch.ones(2, 2))))
        program = module.to_executorch().executorch_program

        num_mm = 0
        num_add = 0
        num_other = 0
        for inst in program.execution_plan[0].chains[0].instructions:
            if not isinstance(inst.instr_args, KernelCall):
                continue

            op = (
                program.execution_plan[0]
                .operators[inst.instr_args.op_index]  # pyre-ignore[16]
                .name
            )

            if "mm" in op:
                num_mm += 1
            elif "add" in op:
                num_add += 1
            else:
                num_other += 1

        self.assertEqual(num_mm, 2)
        self.assertEqual(num_add, 1)
        self.assertEqual(num_other, 0)

    def test_emit_map(self) -> None:
        class Foo(torch.nn.Module):
            def forward(self, x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
                def map_fn(x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
                    return x + y

                return control_flow.map(map_fn, x, y)

        f = Foo()

        inputs = (torch.ones(4, 4), torch.ones(4))
        module = to_edge(
            export(f, inputs),
            compile_config=exir.EdgeCompileConfig(_check_ir_validity=False),
        )
        program = module.to_executorch().executorch_program

        op_table = program.execution_plan[0].operators
        # The first two operators at the beginning of a map program should be sym_size
        # and select_copy, which is what we verify here. The first operator is to generate
        # the number of iterations and the second operator is to slice the input tensor to
        # generate the tensor on which this iteration will operate on.
        self.assertEqual(
            op_table[
                program.execution_plan[0]  # pyre-ignore[16]
                .chains[0]
                .instructions[0]
                .instr_args.op_index
            ].name,
            "aten::sym_size",
        )
        self.assertEqual(
            op_table[
                program.execution_plan[0]  # pyre-ignore[16]
                .chains[0]
                .instructions[1]
                .instr_args.op_index
            ].name,
            "aten::select_copy",
        )

        # The last three instructions in the map sub-program are:
        # - Calling the custom op to append the output of this iteration to the accumulator tensor
        # - Increment the iteration count.
        # - Then checking if we've completed all the iterations.
        # We check here that both of these have been generated.
        self.assertEqual(
            op_table[
                program.execution_plan[0]  # pyre-ignore[16]
                .chains[0]
                .instructions[-5]
                .instr_args.op_index
            ].name,
            "executorch_prim::et_copy_index",
        )
        self.assertEqual(
            op_table[
                program.execution_plan[0]  # pyre-ignore[16]
                .chains[0]
                .instructions[-4]
                .instr_args.op_index
            ].name,
            "executorch_prim::add",
        )
        self.assertEqual(
            op_table[
                program.execution_plan[0]  # pyre-ignore[16]
                .chains[0]
                .instructions[-3]
                .instr_args.op_index
            ].name,
            "executorch_prim::eq",
        )
        # The last two instructions in the overall program check if we should jump back to the
        # beginning of the loop and then resets the iteration counter if we fall through.
        self.assertTrue(
            isinstance(
                program.execution_plan[0].chains[0].instructions[-2].instr_args,
                JumpFalseCall,
            )
        )
        self.assertEqual(
            op_table[
                program.execution_plan[0]  # pyre-ignore[16]
                .chains[0]
                .instructions[-1]
                .instr_args.op_index
            ].name,
            "executorch_prim::sub",
        )

    def test_dim_order(self) -> None:
        class SimpleLinear(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.linear = torch.nn.Linear(5, 5)

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                return torch.nn.functional.relu(self.linear(x))

        model = SimpleLinear()
        inputs = (torch.ones(10, 5),)
        program = (
            to_edge(
                export(model, inputs),
                compile_config=exir.EdgeCompileConfig(_check_ir_validity=False),
            )
            .to_executorch()
            .executorch_program
        )

        addmm_found = False
        for inst in program.execution_plan[0].chains[0].instructions:
            kernel = inst.instr_args
            if isinstance(kernel, KernelCall):
                op_id = kernel.op_index
                op = program.execution_plan[0].operators[op_id]
                if op.name == "aten::addmm":
                    addmm_found = True
                    args = kernel.args
                    bias_id = args[0]
                    act_id = args[1]
                    weight_id = args[2]
                    bias_dim_order = [0]
                    act_dim_order = [0, 1]
                    weight_dim_order = [0, 1]
                    bias_tensor = typing.cast(
                        schema.Tensor, program.execution_plan[0].values[bias_id].val
                    )
                    act_tensor = typing.cast(
                        schema.Tensor, program.execution_plan[0].values[act_id].val
                    )
                    weight_tensor = typing.cast(
                        schema.Tensor, program.execution_plan[0].values[weight_id].val
                    )
                    self.assertTrue(bias_tensor.dim_order == bias_dim_order)
                    self.assertTrue(act_tensor.dim_order == act_dim_order)
                    self.assertTrue(weight_tensor.dim_order == weight_dim_order)
        self.assertTrue(addmm_found)

    # cant compare plans directly with __eq__ because of the plan names, and constant_buffer_idx in tensor values
    def _compare_execution_plans(
        self, plan_single: ExecutionPlan, plan_merged: ExecutionPlan
    ) -> None:
        self.assertEqual(
            plan_single.container_meta_type,
            plan_merged.container_meta_type,
        )
        self.assertEqual(
            plan_single.inputs,
            plan_merged.inputs,
        )
        self.assertEqual(
            plan_single.outputs,
            plan_merged.outputs,
        )
        self.assertEqual(
            plan_single.chains,
            plan_merged.chains,
        )
        self.assertEqual(
            plan_single.operators,
            plan_merged.operators,
        )
        self.assertEqual(
            plan_single.non_const_buffer_sizes,
            plan_merged.non_const_buffer_sizes,
        )
        self.assertEqual(
            len(plan_single.values),
            len(plan_merged.values),
        )
        for i in range(0, len(plan_single.values)):
            single_val = plan_single.values[i].val
            merged_val = plan_merged.values[i].val
            if isinstance(single_val, Tensor):
                # constant buffer index might be different as the constant buffer is shared between plans
                self.assertTrue(isinstance(merged_val, Tensor))
                self.assertEqual(single_val.storage_offset, merged_val.storage_offset)
                self.assertEqual(single_val.scalar_type, merged_val.scalar_type)
                self.assertEqual(single_val.sizes, merged_val.sizes)
                self.assertEqual(single_val.dim_order, merged_val.dim_order)
                self.assertEqual(single_val.requires_grad, merged_val.requires_grad)
                self.assertEqual(single_val.layout, merged_val.layout)
                self.assertEqual(single_val.allocation_info, merged_val.allocation_info)
                self.assertEqual(single_val.shape_dynamism, merged_val.shape_dynamism)
            else:
                self.assertEqual(single_val, merged_val)

    def test_emit_multiple_entry_points(self) -> None:
        class SimpleLinear(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.linear = torch.nn.Linear(5, 5)
                self.linear2 = torch.nn.Linear(5, 5)

            def forward_relu(self, x: torch.Tensor) -> torch.Tensor:
                return torch.nn.functional.relu(self.linear(x))

            def forward_sigmoid(self, x: torch.Tensor) -> torch.Tensor:
                return torch.nn.functional.sigmoid(self.linear2(x))

        model = SimpleLinear()
        inputs = (torch.ones(10, 5),)
        program_relu = to_edge(
            export(WrapperModule(model.forward_relu), inputs),
            compile_config=exir.EdgeCompileConfig(_check_ir_validity=False),
        ).to_executorch()
        program_sigmoid = to_edge(
            export(WrapperModule(model.forward_sigmoid), inputs),
            compile_config=exir.EdgeCompileConfig(_check_ir_validity=False),
        ).to_executorch()
        exir_input = {
            "forward_relu": program_relu.exported_program(),
            "forward_sigmoid": program_sigmoid.exported_program(),
        }
        merged_program = emit_program(exir_input, False).program
        self.assertEqual(len(merged_program.execution_plan), 2)

        self.assertEqual(
            merged_program.execution_plan[0].name,
            "forward_relu",
        )
        self.assertEqual(
            merged_program.execution_plan[1].name,
            "forward_sigmoid",
        )
        # reserved spot, weight, bias
        self.assertEqual(
            len(program_sigmoid._emitter_output.program.constant_buffer),
            3,
        )
        self.assertEqual(
            len(program_relu._emitter_output.program.constant_buffer),
            3,
        )
        # sum of the entry points minus 1 because we only have one reserved spot still
        self.assertEqual(
            len(merged_program.constant_buffer),
            len(program_sigmoid._emitter_output.program.constant_buffer)
            + len(program_relu._emitter_output.program.constant_buffer)
            - 1,
        )

        self._compare_execution_plans(
            merged_program.execution_plan[0],
            program_relu._emitter_output.program.execution_plan[0],
        )
        self._compare_execution_plans(
            merged_program.execution_plan[1],
            program_sigmoid._emitter_output.program.execution_plan[0],
        )

    def test_emit_weight_deduplication(self) -> None:
        class SimpleLinear(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.linear = torch.nn.Linear(5, 5)

            def forward_relu(self, x: torch.Tensor) -> torch.Tensor:
                return torch.nn.functional.relu(self.linear(x))

            def forward_sigmoid(self, x: torch.Tensor) -> torch.Tensor:
                return torch.nn.functional.sigmoid(self.linear(x))

        model = SimpleLinear()
        inputs = (torch.ones(10, 5),)
        program_relu = to_edge(
            export(WrapperModule(model.forward_relu), inputs)
        ).to_executorch()
        program_sigmoid = to_edge(
            export(WrapperModule(model.forward_sigmoid), inputs)
        ).to_executorch()
        exir_input = {
            "forward_relu": program_relu.exported_program(),
            "forward_sigmoid": program_sigmoid.exported_program(),
        }
        merged_program = emit_program(exir_input, False).program
        self.assertEqual(len(merged_program.execution_plan), 2)

        # reserved spot, weight, bias
        self.assertEqual(
            len(program_sigmoid._emitter_output.program.constant_buffer),
            3,
        )
        self.assertEqual(
            len(program_relu._emitter_output.program.constant_buffer),
            3,
        )
        # weights are shared between entry points so the merged one should deduplicate everything
        self.assertEqual(len(merged_program.constant_buffer), 3)

        self._compare_execution_plans(
            merged_program.execution_plan[0],
            program_relu._emitter_output.program.execution_plan[0],
        )
        self._compare_execution_plans(
            merged_program.execution_plan[1],
            program_sigmoid._emitter_output.program.execution_plan[0],
        )

    def test_emit_execution_plans_sorted(self) -> None:
        class Simple(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()

            def a(self, x: torch.Tensor) -> torch.Tensor:
                return x

            def b(self, x: torch.Tensor) -> torch.Tensor:
                return x

            def c(self, x: torch.Tensor) -> torch.Tensor:
                return x

        model = Simple()
        inputs = (torch.ones(10, 5),)

        def make_program(
            fn,
            inputs,
        ) -> "ExecutorchProgramManager":
            return to_edge(
                export(
                    WrapperModule(fn),
                    inputs,
                )
            ).to_executorch()

        program_a = make_program(model.a, inputs)
        program_b = make_program(model.b, inputs)
        program_c = make_program(model.c, inputs)

        exir_input = {
            "b": program_b.exported_program(),
            "c": program_c.exported_program(),
            "a": program_a.exported_program(),
        }
        merged_program = emit_program(exir_input, False).program
        self.assertEqual(len(merged_program.execution_plan), 3)
        self.assertEqual(merged_program.execution_plan[0].name, "a")
        self.assertEqual(merged_program.execution_plan[1].name, "b")
        self.assertEqual(merged_program.execution_plan[2].name, "c")

        # Create a second program equivalent to the first, but the input is in a different order.
        # python dicts are instertion ordered
        exir_input2 = {
            "a": program_b.exported_program(),
            "b": program_c.exported_program(),
            "c": program_a.exported_program(),
        }
        merged_program2 = emit_program(exir_input2, False).program
        self.assertEqual(
            merged_program2.execution_plan[0], merged_program.execution_plan[0]
        )
        self.assertEqual(
            merged_program2.execution_plan[1], merged_program.execution_plan[1]
        )
        self.assertEqual(
            merged_program2.execution_plan[2], merged_program.execution_plan[2]
        )

    def test_upper_bound_memory_planning_respect_input_constraints(self) -> None:
        class Foo(torch.nn.Module):
            def forward(self, k: torch.Tensor) -> torch.Tensor:
                k = torch.cat((k, torch.ones(1, 4)))
                return k

        func = Foo()

        k = torch.rand(2, 4)
        dim0_k = Dim("dim0_k", max=3)
        dynamic_shapes = {"k": {0: dim0_k}}
        captured = export(
            func,
            (k,),
            dynamic_shapes=dynamic_shapes,
        )
        edge = to_edge(captured)
        from executorch.exir.passes import MemoryPlanningPass

        config = exir.ExecutorchBackendConfig(
            sym_shape_eval_pass=ConstraintBasedSymShapeEvalPass(),
            memory_planning_pass=MemoryPlanningPass(
                memory_planning_algo="greedy",
                # allow_lifetime_and_storage_overlap: bool = False,
                alloc_graph_input=True,
                alloc_graph_output=False,
            ),
        )

        exe_prog = edge.to_executorch(config)
        program = exe_prog._emitter_output.program
        exir.print_program.pretty_print(exe_prog._emitter_output.program.execution_plan)
        execution_plan = program.execution_plan[0]
        self.check_tensor_buffer_loc(0, execution_plan.values, 0, 1, 0)
        self.check_tensor_buffer_loc(1, execution_plan.values, 0, 1, 48)

    def test_emit_prims(self) -> None:
        class Simple(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.linear = torch.nn.Linear(5, 5)
                self.x: int = 3
                self.y = 2

            def get_ints(self) -> Tuple[int]:
                return (self.x, self.y)

            def get_str(self) -> str:
                return "foo"

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                return torch.nn.functional.sigmoid(self.linear(x))

        model = Simple()
        inputs = (torch.ones(10, 5),)
        program = to_edge(export(model, inputs)).to_executorch()
        exir_input = {
            "forward": program.exported_program(),
        }
        getters = {}
        getters["get_ints"] = model.get_ints()
        getters["get_str"] = model.get_str()
        print(getters["get_str"])
        merged_program = emit_program(exir_input, False, getters).program
        self.assertEqual(len(merged_program.execution_plan), 3)

        self.assertEqual(
            merged_program.execution_plan[0].name,
            "forward",
        )
        self.assertEqual(
            merged_program.execution_plan[1].name,
            "get_ints",
        )
        self.assertEqual(
            merged_program.execution_plan[2].name,
            "get_str",
        )
        # no instructions in a getter
        self.assertEqual(
            len(merged_program.execution_plan[1].chains[0].instructions),
            0,
        )
        # 2 outputs for the flattened tuple
        self.assertEqual(
            len(merged_program.execution_plan[1].outputs),
            2,
        )
        # outputs are 0 and 1 in the values table
        self.assertEqual(
            merged_program.execution_plan[1].outputs,
            [0, 1],
        )
        # value 0 is 3
        self.assertEqual(
            # pyre-ignore
            merged_program.execution_plan[1].values[0].val.int_val,
            3,
        )
        self.assertEqual(
            # pyre-ignore
            merged_program.execution_plan[1].values[1].val.int_val,
            2,
        )
        self.assertEqual(
            len(merged_program.execution_plan[2].outputs),
            1,
        )
        self.assertEqual(
            # pyre-ignore
            merged_program.execution_plan[2].values[0].val.string_val,
            "foo",
        )

    def test_emit_debug_handle_map(self) -> None:
        mul_model = Mul()
        program_mul = to_edge(
            export(
                mul_model,
                mul_model.get_random_inputs(),
            )
        ).to_executorch()
        # this triggers the actual emission of the graph
        program_mul._emitter_output.program
        self.assertIsNotNone(program_mul.debug_handle_map)

    def test_final_graph_module_update_debug_handle(self) -> None:
        class SimpleAddMul(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                a = x + 1
                return a * 2

        mul_model = SimpleAddMul()
        program_mul = to_edge(
            export(
                mul_model,
                (torch.ones(2, 2),),
            )
        ).to_executorch()

        # this triggers the actual emission of the graph
        program = program_mul._emitter_output.program
        node = None
        program.execution_plan[0].chains[0].instructions[  # pyre-ignore[16]
            0
        ].instr_args.op_index

        # Find the multiplication node in the graph that was emitted.
        for node in program_mul.exported_program().graph.nodes:
            if node.target == torch.ops.aten.mul.out:
                break
        self.assertIsNotNone(node)

        idx = 0
        # Find the multiplication instruction in the program that was emitted.
        for idx in range(len(program.execution_plan[0].chains[0].instructions)):
            instruction = program.execution_plan[0].chains[0].instructions[idx]
            op_index = instruction.instr_args.op_index  # pyre-ignore[16]
            if "mul" in program.execution_plan[0].operators[op_index].name:
                break

        # The instruction id of the multiplication instruction and the debug handle of the
        # multiplication node in the graph module (which was updated in the emitter to be
        # the same as the instruction id) must be the same.
        self.assertEqual(
            idx,
            node.meta.get("debug_handle"),
        )

    def test_delegate_with_input_list(self) -> None:
        class BackendWithCompilerDemo(BackendDetails):
            @staticmethod
            def preprocess(
                edge_program,
                compile_specs,
            ) -> bytes:
                return PreprocessResult(
                    processed_bytes=bytes(str("test"), encoding="utf8"),
                    debug_handle_map=None,
                )

        class TestModel(nn.Module):
            def __init__(self):
                super(TestModel, self).__init__()

            def forward(self, x):
                return torch.cat(x)

        inputs = ([torch.ones(2, 2), torch.ones(2, 2)],)
        model = TestModel()
        edgeir_m = to_edge(export(model, inputs))
        lowered_module = to_backend(
            "BackendWithCompilerDemo", edgeir_m.exported_program(), []
        )

        class CompositeModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.lowered_module = lowered_module

            def forward(self, list_a):
                return self.lowered_module(list_a)

        composite_model = CompositeModule()
        exec_prog = to_edge(
            export(composite_model, inputs),
        ).to_executorch()
        exec_prog.buffer

    def test_delegate_with_input_tuple(self) -> None:
        class BackendWithCompilerDemo(BackendDetails):
            @staticmethod
            def preprocess(
                edge_program,
                compile_specs,
            ) -> bytes:
                return PreprocessResult(
                    processed_bytes=bytes(str("test"), encoding="utf8"),
                    debug_handle_map=None,
                )

        class AddMulModule(torch.nn.Module):
            def __init__(self):
                super().__init__()

            def forward(self, input):  # a, x, b):
                y = torch.mm(input[0], input[1])
                z = torch.add(y, input[2])
                return z

        model_inputs = ((torch.ones(2, 2), 2 * torch.ones(2, 2), 3 * torch.ones(2, 2)),)
        model = AddMulModule()
        edgeir_m = to_edge(export(model, model_inputs))
        lowered_module = to_backend(
            "BackendWithCompilerDemo", edgeir_m.exported_program(), []
        )

        class CompositeModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.lowered_module = lowered_module

            def forward(self, list_a):
                return self.lowered_module(list_a)

        composite_model = CompositeModule()
        exec_prog = to_edge(
            export(composite_model, model_inputs),
        ).to_executorch()
        exec_prog.buffer

    def test_delegate_mapping(self) -> None:
        debug_handle_map = {1: [1, 2]}

        class BackendWithCompilerDemo(BackendDetails):
            @staticmethod
            def preprocess(
                edge_program,
                compile_specs,
            ) -> bytes:
                return PreprocessResult(
                    processed_bytes=bytes(str("test"), encoding="utf8"),
                    debug_handle_map=debug_handle_map,
                )

        class TestModel(nn.Module):
            def __init__(self):
                super(TestModel, self).__init__()

            def forward(self, x, y):
                return torch.add(x, y)

        inputs = (torch.ones(2, 2), torch.ones(2, 2))
        model = TestModel()
        edgeir_m = to_edge(export(model, inputs))
        lowered_module = to_backend(
            "BackendWithCompilerDemo", edgeir_m.exported_program(), []
        )

        class CompositeModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.lowered_module = lowered_module

            def forward(self, x, y):
                return self.lowered_module(x, y)

        composite_model = CompositeModule()
        exec_prog = to_edge(
            export(composite_model, inputs),
        ).to_executorch()
        # Reading the program triggers the call to emit_program underneath which
        # we need to be done for our test to succeed.
        exec_prog._emitter_output.program
        self.assertIsNotNone(exec_prog.delegate_map)
        self.assertIsNotNone(exec_prog.delegate_map.get("forward"))
        self.assertIsNotNone(
            exec_prog.delegate_map.get("forward").get(0)  # pyre-ignore[16]
        )
        self.assertEqual(
            exec_prog.delegate_map.get("forward").get(0).get("name"),
            "BackendWithCompilerDemo",
        )
        self.assertTrue(
            len(exec_prog.delegate_map.get("forward").get(0).get("delegate_map")) != 0
        )

    def test_emit_weight_view(self) -> None:
        class ModWithWeightViews(nn.Module):
            def __init__(self):
                super(ModWithWeightViews, self).__init__()
                self.W = torch.nn.Parameter(torch.randn(2))
                self.W1 = self.W[:1]
                self.W2 = self.W[1:]

            def forward(self, x):
                return self.W1 + self.W2 + x

        model = ModWithWeightViews()
        # each weight is a view of the same storage
        self.assertEqual(model.W1.nbytes, 4)
        self.assertEqual(model.W1.untyped_storage().nbytes(), 8)
        self.assertEqual(model.W2.nbytes, 4)
        self.assertEqual(model.W2.untyped_storage().nbytes(), 8)
        program = to_edge(
            export(
                model,
                (torch.ones(1),),
            )
        ).to_executorch()

        program = program._emitter_output.program
        # each emitted weight is not a view
        self.assertEqual(len(program.constant_buffer[1].storage), 4)
        self.assertEqual(len(program.constant_buffer[2].storage), 4)
