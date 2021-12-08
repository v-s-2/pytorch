
r"""
The torch.onnx module contains functions to export models into the ONNX
IR format.  These models can be loaded with the ONNX library and then
converted to models which run on other deep learning frameworks.
"""

import torch
import torch.jit
import torch.autograd
import torch.serialization
import re
import collections
import contextlib
import copy
import numbers
import warnings
import inspect
from torch._six import string_classes
from torch.jit import _unique_state_dict
from torch.onnx import ONNX_ARCHIVE_MODEL_PROTO_NAME, ExportTypes, OperatorExportTypes, SymbolicContext, TrainingMode, CheckerError
from torch._C import ListType, OptionalType, _propagate_and_assign_input_shapes, _check_onnx_proto
from typing import List, Tuple, Union

# the flag to tell the user whether it's in the middle of ONNX export or not
__IN_ONNX_EXPORT = False


def is_in_onnx_export():
    global __IN_ONNX_EXPORT
    return __IN_ONNX_EXPORT


# Skip check due to cannot import IValue from torch._C
_params_dict = {}  # type: ignore[var-annotated]


@contextlib.contextmanager
def select_model_mode_for_export(model, mode):
    if not isinstance(model, torch.jit.ScriptFunction):
        is_originally_training = model.training

        if mode is None:
            mode = TrainingMode.EVAL
            # if the model is in training mode but the user did not specify
            # to export the model in training mode, export the model in inference
            # mode (default) and warn them
            if is_originally_training:
                warnings.warn("You are exporting the model to ONNX while in training mode with "
                              "'train' parameter not specified. The model will default to inference mode export. "
                              "If you wish to export a training amenable ONNX model, specify training=TrainingMode.TRAINING or "
                              "training=TrainingMode.PRESERVE (to preserve the original model state) in torch.onnx.export().")

        # if mode == TrainingMode.EVAL or (mode == TrainingMode.PRESERVE and not is_originally_training) => is_training = False
        is_export_training = False
        # ONNX opset 12 has better support for training amenable models, with updated
        # versions of the dropout and batch_norm operators
        if mode == TrainingMode.TRAINING or (mode == TrainingMode.PRESERVE and is_originally_training):
            from torch.onnx.symbolic_helper import _export_onnx_opset_version
            if _export_onnx_opset_version < 12:
                warnings.warn("You are exporting the model in training mode with onnx opset version {}. "
                              "Opset versions lower than opset 12 will not be able to export nodes such as "
                              "Dropout and BatchNorm correctly.".format(_export_onnx_opset_version))
            is_export_training = True

        from torch.onnx.symbolic_helper import _set_training_mode
        _set_training_mode(is_export_training)
        model.train(is_export_training)
    try:
        yield
    finally:
        if not isinstance(model, torch.jit.ScriptFunction):
            model.train(is_originally_training)

@contextlib.contextmanager
def disable_apex_o2_state_dict_hook(model):
    # Apex O2 hook state_dict to return fp16 weights as fp32.
    # Exporter cannot identify them as same tensors.
    # Since this hook is only used by optimizer, it is safe to
    # remove this hook while exporting.
    if not isinstance(model, torch.jit.ScriptFunction):
        tmp_map = {}  # type: ignore[var-annotated]
        for module in model.modules():
            for k, v in module._state_dict_hooks.items():
                if type(v).__name__ == 'O2StateDictHook':
                    if module not in tmp_map:
                        tmp_map[module] = {}
                    tmp_map[module][k] = v
            if module in tmp_map:
                for k in tmp_map[module].keys():
                    module._state_dict_hooks.pop(k)
    try:
        yield
    finally:
        if not isinstance(model, torch.jit.ScriptFunction):
            for module, m_map in tmp_map.items():
                for k, v in m_map.items():
                    module._state_dict_hooks[k] = v

@contextlib.contextmanager
def exporter_context(model, mode):
    with select_model_mode_for_export(model, mode) as mode_ctx, \
            disable_apex_o2_state_dict_hook(model) as apex_ctx:
        yield (mode_ctx, apex_ctx)


def export(model, args, f, export_params=True, verbose=False, training=None,
           input_names=None, output_names=None, operator_export_type=OperatorExportTypes.ONNX,
           opset_version=None, do_constant_folding=True, dynamic_axes=None,
           keep_initializers_as_inputs=None, custom_opsets=None,
           export_modules_as_functions=False):

    _export(model, args, f, export_params, verbose, training, input_names, output_names,
            operator_export_type=operator_export_type, opset_version=opset_version,
            do_constant_folding=do_constant_folding, dynamic_axes=dynamic_axes,
            keep_initializers_as_inputs=keep_initializers_as_inputs,
            custom_opsets=custom_opsets, export_modules_as_functions=export_modules_as_functions)


def _is_constant_tensor_list(node):
    if node.kind() != "prim::Constant":
        return False
    output_type = node.output().type()
    if output_type.isSubtypeOf(ListType.ofTensors()):
        return True
    if output_type.isSubtypeOf(ListType(OptionalType.ofTensor())):
        return True

# ONNX can't handle constants that are lists of tensors, which can
# get generated in constant prop. So we split them back into prim::ListConstructs


def _split_tensor_list_constants(g, block):
    for node in block.nodes():
        for subblock in node.blocks():
            _split_tensor_list_constants(g, subblock)
        if _is_constant_tensor_list(node):
            inputs = []
            for val in node.output().toIValue():
                input = g.insertConstant(val)
                input.node().moveBefore(node)
                input.node().copyMetadata(node)
                inputs.append(input)

            lc = (g.create("prim::ListConstruct", inputs)
                  .insertBefore(node)
                  .output()
                  .setType(ListType.ofTensors()))
            lc.node().copyMetadata(node)
            node.output().replaceAllUsesWith(lc)


def _optimize_graph(graph, operator_export_type, _disable_torch_constant_prop=False, fixed_batch_size=False,
                    params_dict=None, dynamic_axes=None, input_names=None, module=None):
    # Inline everything
    torch._C._jit_pass_inline(graph)

    # Remove fork/wait nodes
    torch._C._jit_pass_inline_fork_wait(graph)
    torch._C._jit_pass_lint(graph)
    torch._C._jit_pass_lower_all_tuples(graph)

    # we now record some ops like ones/zeros
    # into a trace where we previously recorded constants.
    # use constant prop to maintain our current level of onnx support
    # without implementing symbolics for all of them
    if _disable_torch_constant_prop is False:
        torch._C._jit_pass_constant_propagation(graph)

    _split_tensor_list_constants(graph, graph)
    # run dce to eliminate dead parts of the graph that might have been
    # left behind by things like symbolic_override
    torch._C._jit_pass_dce(graph)
    torch._C._jit_pass_lint(graph)

    torch._C._jit_pass_canonicalize_graph_fuser_ops(graph)
    torch._C._jit_pass_lint(graph)
    torch._C._jit_pass_peephole(graph, True)
    torch._C._jit_pass_fuse_addmm(graph)
    torch._C._jit_pass_lint(graph)
    from torch.onnx.symbolic_helper import _onnx_shape_inference, _export_onnx_opset_version

    torch._C._jit_pass_peephole(graph, True)
    torch._C._jit_pass_lower_all_tuples(graph)
    # in _jit_pass_onnx, symbolic functions are called for each node for conversion.
    # However, there are nodes that cannot be converted without additional context.
    # For example, the number of outputs from split (and whether it is static or dynamic) is unknown
    # until the point where it is unpacked by listUnpack node.
    # This pass does a preprocess, and prepares the nodes such that enough context can be received
    # by the symbolic function.
    torch._C._jit_pass_onnx_remove_inplace_ops_for_onnx(graph, module)
    torch._C._jit_pass_onnx_preprocess(graph)

    # onnx does not support tuples, so try to remove them
    torch._C._jit_pass_lint(graph)

    # onnx only supports tensors, but 1 / 2 = 0.5 and tensor(1) / tensor(2) = 0
    torch._C._jit_pass_prepare_division_for_onnx(graph)

    torch._C._jit_pass_onnx_remove_print(graph)
    torch._C._jit_pass_onnx_preprocess_caffe2(graph)

    # Caffe2-specific optimization
    is_caffe2_aten_fallback = (operator_export_type == OperatorExportTypes.ONNX_ATEN_FALLBACK and
                               torch.onnx._CAFFE2_ATEN_FALLBACK)
    if is_caffe2_aten_fallback:
        torch.onnx.symbolic_helper._quantized_ops.clear()
        # Unpack quantized weights for conv and linear ops and insert into graph.
        torch._C._jit_pass_onnx_unpack_quantized_weights(graph, params_dict)
        # Insert permutes before and after each conv op to ensure correct order.
        torch._C._jit_pass_onnx_quantization_insert_permutes(graph, params_dict)

        # Find consecutive permutes that are no-ops and remove them.
        torch._C._jit_pass_custom_pattern_based_rewrite_graph("""
        graph(%Pi):
            %Pq = quantized::nhwc2nchw(%Pi)
            %Pr = quantized::nchw2nhwc(%Pq)
            return (%Pr)""", """
        graph(%Ri):
            return (%Ri)""", graph)

    # onnx only supports tensors, so we turn all out number types into tensors
    torch._C._jit_pass_erase_number_types(graph)

    if _onnx_shape_inference:
        input_names = [] if input_names is None else input_names
        dynamic_axes = {} if dynamic_axes is None else dynamic_axes
        torch._C._jit_pass_onnx_set_dynamic_input_shape(graph, dynamic_axes, input_names)
    torch._C._jit_pass_onnx_lint(graph)
    graph = torch._C._jit_pass_onnx(graph, operator_export_type)
    torch._C._jit_pass_onnx_lint(graph)
    torch._C._jit_pass_lint(graph)

    torch._C._jit_pass_onnx_scalar_type_analysis(graph, True, _export_onnx_opset_version)
    torch._C._jit_pass_lint(graph)

    torch._C._jit_pass_onnx_peephole(graph, _export_onnx_opset_version, fixed_batch_size)
    torch._C._jit_pass_lint(graph)

    # graph is not a valid jit graph anymore because types have been replaced
    # (e.g. int with Tensor), so it now contains operators that don't actually
    # exist. We can't run normal dead code elimination because it'd fail trying
    # to look up if an operator has side effects, but we can run a dead code
    # elimination variant that doesn't need to look up if an op has side effects.
    torch._C._jit_pass_dce_allow_deleting_nodes_with_side_effects(graph)
    torch._C._jit_pass_lint(graph)
    graph = torch._C._jit_pass_canonicalize(graph)
    torch._C._jit_pass_lint(graph)
    if _onnx_shape_inference:
        torch._C._jit_pass_onnx_graph_shape_type_inference(graph, params_dict, _export_onnx_opset_version)
    return graph


# We accept dictionaries and strings as ONNX inputs,
# but they should be only for configuration use.
# we detect here if these inputs are modified, and if so
# we warn the user that the changes won't take effect in the
# traced ONNX graph
def warn_on_static_input_change(input_states):
    for input, traced_input in zip(input_states[0], input_states[1]):
        if isinstance(input, dict):
            if list(input.keys()) != list(traced_input.keys()):
                warning = "We detected that you are modifying a dictionary that is an input to your " \
                          "model. " \
                          "Note that dictionaries are allowed as inputs in ONNX but they should be " \
                          "handled with care. " \
                          "Usages of dictionaries is not recommended, and should not be used except " \
                          "for configuration use. " \
                          "Also note that the order and values of the keys must remain the same. "
                warnings.warn(warning)
        elif isinstance(input, str):
            if input != traced_input:
                warning = "The model seems to have string inputs/outputs. " \
                          "Note that strings will not appear as inputs/outputs of the ONNX graph. "
                warnings.warn(warning)


def _resolve_args_by_export_type(arg_name, arg_value, operator_export_type):
    # This helper method resolves the arguments that are ignored when export_type != operator_export_type.ONNX
    if operator_export_type is not operator_export_type.ONNX:
        if arg_value is True:
            warnings.warn("`{}' can be set to True only when 'operator_export_type' is "
                          "`ONNX`. Since 'operator_export_type' is not set to 'ONNX', "
                          "`{}` argument will be ignored.".format(arg_name, arg_name))
        arg_value = False
    return arg_value


def _decide_keep_init_as_input(keep_initializers_as_inputs, operator_export_type,
                               opset_version):
    # This method encapsulates the logic to decide whether the initializers in the graph
    # should be listed as ONNX graph inputs (i.e., whether to choose ONNX IR v3 or v4).
    # If keep_initializers_as_inputs is not specified (None), then we decide whether to keep
    # initializers as graph inputs (val_keep_init_as_ip) based on export type. If export type
    # is ONNX, then do not keep initializers as input (val_keep_init_as_ip=False). For all other
    # export types keep initializers as input (val_keep_init_as_ip=True).
    # If keep_initializers_as_inputs is specified, then respect it. Unless opset version <= 8,
    # in which case it must be ignored because for opset version <= 8, all initializers MUST be
    # part of graph input (only ONNX IR v3 is allowed), i.e. val_keep_init_as_ip=True.

    # Special handling is needed for opset version 8 or lower, because irrespective
    # of user input for keep_initializers_as_inputs, the graph must follow ONNX IR v3
    # semantics, i.e. all initializers must be listed as ONNX graph input.
    if opset_version < 9:
        if keep_initializers_as_inputs is False:
            warnings.warn("Setting 'keep_initializers_as_inputs=False' for opset version"
                          "8 or lower would lead to an invalid ONNX graph. Therefore, "
                          "'keep_initializers_as_inputs=False' is ignored during export."
                          "Exported model will have initializers as graph inputs (compliant "
                          " to ONNX IR v3).")
        return True  # i.e. True == initializers are part of graph input (ONNX IR v3)
    val_keep_init_as_ip = True if keep_initializers_as_inputs is None else keep_initializers_as_inputs
    if keep_initializers_as_inputs is None and operator_export_type is OperatorExportTypes.ONNX:
        val_keep_init_as_ip = False
    return val_keep_init_as_ip


def _decide_add_node_names(add_node_names, operator_export_type):
    return _resolve_args_by_export_type("add_node_names", add_node_names, operator_export_type)


def _decide_constant_folding(do_constant_folding, operator_export_type, training):
    do_constant_folding = _resolve_args_by_export_type("do_constant_folding", do_constant_folding, operator_export_type)
    if do_constant_folding and (training is not None and training is not TrainingMode.EVAL):
        warnings.warn("It is recommended that constant folding be turned off ('do_constant_folding=False') "
                      "when exporting the model in training-amenable mode, i.e. with 'training=TrainingMode.TRAIN' "
                      "or 'training=TrainingMode.PRESERVE' (when model is in training mode). Otherwise, some "
                      "learnable model parameters may not translate correctly in the exported ONNX model "
                      "because constant folding mutates model parameters. Please consider "
                      "turning off constant folding or setting the training=TrainingMode.EVAL.")
    return do_constant_folding


def _decide_input_format(model, args):
    try:
        sig = inspect.signature(model.forward)
        ordered_list_keys = list(sig.parameters.keys())
        if isinstance(args[-1], dict):
            args_dict = args[-1]
            args = list(args)[:-1]
            n_nonkeyword = len(args)
            for optional_arg in ordered_list_keys[n_nonkeyword:]:
                if optional_arg in args_dict:
                    args.append(args_dict[optional_arg])
                # Check if this arg has a default value
                else:
                    param = sig.parameters[optional_arg]
                    if param.default is param.empty:
                        args.append(None)
                    else:
                        args.append(param.default)
            args = tuple(args)
        return args
    # Cases of models without forward functions and dict inputs
    except (AttributeError, ValueError):
        warnings.warn("Model has no forward function")
        return args
    # Cases of models with no input args
    except IndexError:
        warnings.warn("No input args")
        return args
    except Exception as e:
        warnings.warn("Skipping _decide_input_format\n {}".format(e.args[0]))
        return args

def _trace(func, args, operator_export_type, return_outs=False):
    # Special case for common case of passing a single Tensor
    if isinstance(args, torch.Tensor):
        args = (args, )

    trace_graph, torch_out, inputs_states = \
        torch.jit._get_trace_graph(func, args, strict=False, _force_outplace=False, _return_inputs_states=True)
    warn_on_static_input_change(inputs_states)

    trace_graph = _optimize_graph(trace_graph, operator_export_type, params_dict={})
    if return_outs:
        return trace_graph, torch_out
    return trace_graph


def _trace_and_get_graph_from_model(model, args):

    # A basic sanity check: make sure the state_dict keys are the same
    # before and after running the model.  Fail fast!
    orig_state_dict_keys = _unique_state_dict(model).keys()

    trace_graph, torch_out, inputs_states = \
        torch.jit._get_trace_graph(model, args, strict=False, _force_outplace=False, _return_inputs_states=True)
    warn_on_static_input_change(inputs_states)

    if orig_state_dict_keys != _unique_state_dict(model).keys():
        raise RuntimeError("state_dict changed after running the tracer; "
                           "something weird is happening in your model!")

    return trace_graph, torch_out


def _get_param_count_list(method_graph, args_params):
    param_count_list = []
    for input_, arg_params_ in zip(method_graph.inputs(), args_params):
        if "PackedParams" in str(input_.type()):
            in_vars, _ = torch.jit._flatten(arg_params_)
            param_count_list.append(len(in_vars))
        else:
            param_count_list.append(1)
    return param_count_list


def _create_jit_graph(model, args):
    torch_out = None
    params: Union[List, Tuple]
    if isinstance(model, torch.jit.ScriptModule):
        try:
            graph = model.forward.graph
            torch._C._jit_pass_onnx_function_substitution(graph)
            freezed_m = torch._C._freeze_module(model._c, preserveParameters=True)
            module, params = torch._C._jit_onnx_list_model_parameters(freezed_m)
            method_graph = module._get_method("forward").graph
            args_params = tuple(args) + tuple(params)
            param_count_list = _get_param_count_list(method_graph, args_params)
            in_vars, _ = torch.jit._flatten(args_params)
            graph = _propagate_and_assign_input_shapes(
                method_graph, tuple(in_vars), param_count_list, False, False)
        except AttributeError as e:
            raise RuntimeError("'forward' method must be a script method") from e
        return graph, params, torch_out, module
    elif isinstance(model, torch.jit.ScriptFunction):
        params = ()
        in_vars, in_desc = torch.jit._flatten(tuple(args))
        graph = model.graph
        torch._C._jit_pass_onnx_function_substitution(graph)
        param_count_list = _get_param_count_list(graph, args)
        graph = _propagate_and_assign_input_shapes(
            graph, tuple(in_vars), param_count_list, False, False)
        return graph, params, torch_out, None
    else:
        graph, torch_out = _trace_and_get_graph_from_model(model, args)
        torch._C._jit_pass_onnx_lint(graph)
        state_dict = _unique_state_dict(model)
        params = list(state_dict.values())
        graph_inputs = list(graph.inputs())
        user_input_num = len(graph_inputs) - len(state_dict)
        param_names = list(state_dict.keys())
        for i, inp in enumerate(graph_inputs):
            if i >= user_input_num:
                inp.setDebugName(param_names[i - user_input_num])
        torch._C._jit_pass_onnx_function_substitution(graph)
        return graph, params, torch_out, None


def _get_named_param_dict(graph, params):
    input_and_param_names = [val.debugName() for val in graph.inputs()]
    param_names = input_and_param_names[len(input_and_param_names) - len(params):]
    _params_dict = dict(zip(param_names, params))
    return _params_dict


def _get_example_outputs(model, args):
    input_args = copy.deepcopy(args)
    input_kwargs = {}
    if input_args and isinstance(input_args[-1], dict):
        input_kwargs = input_args[-1]
        input_args = input_args[:-1]

    example_outputs = model(*input_args, **input_kwargs)
    if isinstance(example_outputs, (torch.Tensor, int, float, bool)):
        example_outputs = (example_outputs,)

    if isinstance(example_outputs, list):
        example_outputs = [example_outputs]
    return example_outputs


def _model_to_graph(model, args, verbose=False,
                    input_names=None, output_names=None,
                    operator_export_type=OperatorExportTypes.ONNX,
                    do_constant_folding=True,
                    _disable_torch_constant_prop=False, fixed_batch_size=False,
                    training=None, dynamic_axes=None):
    r"""Converts model into an ONNX graph.

    Returns:
      graph (torch._C.Graph): A TorchScript IR Graph with ONNX nodes.
      params_dict (Dict[str, torch.Tensor]): Dict from input param name to param value.
      torch_out (Union[NoneType, torch.Tensor, Tuple[torch.Tensor], List[torch.Tensor]]):
        The output tensors resulting from the trace of ``model``.
        If ``model`` is a :class:`torch.jit.ScriptModule` or :class:`torch.jit.ScriptFunction`,
        this will be None, since we are not doing any tracing.
    """
    # TODO: can we simplify this to always return a tuple of Tensor or None?
    from torch.onnx.symbolic_helper import _export_onnx_opset_version
    # Special case for common case of passing a single Tensor
    if isinstance(args, (torch.Tensor, int, float, bool)):
        args = (args, )

    graph, params, torch_out, module = _create_jit_graph(model, args)

    params_dict = _get_named_param_dict(graph, params)

    graph = _optimize_graph(graph, operator_export_type,
                            _disable_torch_constant_prop=_disable_torch_constant_prop,
                            fixed_batch_size=fixed_batch_size, params_dict=params_dict,
                            dynamic_axes=dynamic_axes, input_names=input_names,
                            module=module)
    from torch.onnx.symbolic_helper import _onnx_shape_inference
    if isinstance(model, torch.jit.ScriptModule) or isinstance(model, torch.jit.ScriptFunction):
        example_outputs = _get_example_outputs(model, args)
        out_vars, desc = torch.jit._flatten(tuple(example_outputs))
        torch._C._jit_pass_onnx_assign_output_shape(graph, out_vars, desc, _onnx_shape_inference)
    else:
        flatten_args, _ = torch._C._jit_flatten(args)
        # make sure that the param dict and the graph match each other
        assert len(params) + len(flatten_args) == sum(1 for _ in graph.inputs())

    # NB: ONNX requires complete information about output types, which might be
    # erased by some optimizations, so we need to set it explicitly again.
    if torch_out is not None:
        if not (isinstance(torch_out, list) or isinstance(torch_out, tuple)):
            output_wrapped = [torch_out]
        else:
            output_wrapped = torch_out  # type: ignore[assignment]

        output_tensors, out_desc = torch._C._jit_flatten(tuple(output_wrapped))
        torch._C._jit_pass_onnx_assign_output_shape(graph, output_tensors, out_desc, _onnx_shape_inference)

    _set_input_and_output_names(graph, input_names, output_names)
    params_dict = _get_named_param_dict(graph, params)

    if training is None or training == TrainingMode.EVAL:
        params_dict = torch._C._jit_pass_onnx_eval_peephole(graph, params_dict)

    if do_constant_folding and _export_onnx_opset_version in torch.onnx.constant_folding_opset_versions:
        params_dict = torch._C._jit_pass_onnx_constant_fold(graph, params_dict,
                                                            _export_onnx_opset_version)
        torch._C._jit_pass_dce_allow_deleting_nodes_with_side_effects(graph)

    if _onnx_shape_inference:
        torch._C._jit_pass_onnx_graph_shape_type_inference(graph, params_dict, _export_onnx_opset_version)

    params_dict = torch._C._jit_pass_onnx_eliminate_unused_items(graph, params_dict)

    # For ONNX opset < 9, constants only have three data types: float16, float, double.
    # In this pass transform constants of other data types to float/double + cast operator.
    if _export_onnx_opset_version < 9:
        torch._C._jit_pass_onnx_cast_all_constant_to_floating(graph)

    if verbose:
        print(graph)

    params_dict = torch._C._jit_pass_filter_non_tensor_arguments(params_dict)
    torch._C._jit_decay_packed_param_input_types(graph)

    # If output names lack a proper name and are identified only by their unique
    # give them a legible name for debugging purposes
    _apply_friendly_debug_names(graph, params_dict)

    return graph, params_dict, torch_out


def export_to_pretty_string(model, args, export_params=True, verbose=False, training=None,
                            input_names=None, output_names=None, operator_export_type=OperatorExportTypes.ONNX,
                            export_type=ExportTypes.PROTOBUF_FILE, google_printer=False, opset_version=None,
                            keep_initializers_as_inputs=None, custom_opsets=None, add_node_names=True,
                            do_constant_folding=True, dynamic_axes=None):
    from torch.onnx.symbolic_helper import _default_onnx_opset_version, _set_opset_version
    from torch.onnx.symbolic_helper import _set_operator_export_type
    if opset_version is None:
        opset_version = _default_onnx_opset_version
    if custom_opsets is None:
        custom_opsets = {}
    _set_opset_version(opset_version)
    _set_operator_export_type(operator_export_type)
    from torch.onnx.symbolic_helper import _set_onnx_shape_inference
    _set_onnx_shape_inference(True)
    with exporter_context(model, training):
        val_keep_init_as_ip = _decide_keep_init_as_input(keep_initializers_as_inputs,
                                                         operator_export_type,
                                                         opset_version)
        val_add_node_names = _decide_add_node_names(add_node_names, operator_export_type)
        val_do_constant_folding = _decide_constant_folding(do_constant_folding, operator_export_type, training)
        args = _decide_input_format(model, args)
        graph, params_dict, torch_out = _model_to_graph(model, args, verbose, input_names,
                                                        output_names, operator_export_type,
                                                        val_do_constant_folding,
                                                        training=training, dynamic_axes=dynamic_axes)

        return graph._pretty_print_onnx(params_dict, opset_version, False,
                                        operator_export_type, google_printer,
                                        val_keep_init_as_ip, custom_opsets, val_add_node_names)

def unconvertible_ops(model, args, training=TrainingMode.EVAL, opset_version=None):
    r"""
    Converts the model with operator_export_type set to
    OperatorExportTypes.ONNX_FALLTHROUGH once in order to get a list of
    all the ops that are not supported/implemented by the exporter.

    Args:
        model: Same as corresponding arg to torch.onnx.export.
        args: Same as corresponding arg to torch.onnx.export.
        training: Same as corresponding arg to torch.onnx.export.
        opset_version: Same as corresponding arg to torch.onnx.export.

    Returns:
        Tuple[torch._C.Graph, List[str]], where the list includes the names
          of the unconvertible ops.
    """
    from torch.onnx.symbolic_helper import _default_onnx_opset_version, _set_opset_version
    opset_version = opset_version or _default_onnx_opset_version
    _set_opset_version(opset_version)
    # operator_export_type is set to ONNX_FALLTHROUGH by default so that if an op is not supported
    # in ONNX, fall through will occur and export the operator as is, as a custom ONNX op.
    operator_export_type = OperatorExportTypes.ONNX_FALLTHROUGH
    with exporter_context(model, training):
        args = _decide_input_format(model, args)
        graph, params_dict, torch_out = _model_to_graph(
            model, args,
            # So that if an op connot be converted to ONNX, it will be kept
            # as-is rather than cause a failure.
            operator_export_type=OperatorExportTypes.ONNX_FALLTHROUGH)
    unsupported_ops = list()
    supported_namespaces = ("onnx", "prim")
    for node in graph.nodes():
        if node.kind().split(":")[0] not in supported_namespaces:
            unsupported_ops.append(node.kind())
    return graph, unsupported_ops

def _setup_trace_module_map(model, export_modules_as_functions):
    def __setup_trace_module_map():
        trace_module_map = {_m : torch.typename(type(_m)) for _m in model.modules()}
        torch.jit._trace._trace_module_map = trace_module_map
        return trace_module_map

    if isinstance(export_modules_as_functions, bool) and export_modules_as_functions:
        trace_module_map = __setup_trace_module_map()
        export_modules_as_functions = {v for k, v in trace_module_map.items()}
    elif isinstance(export_modules_as_functions, set) and len(export_modules_as_functions) > 0:
        def _find_typename(v):
            if isinstance(v, type):
                return torch.typename(v)
            else:
                raise RuntimeError("Only type of the `nn.Module` should be "
                                   "passed in the set for argument `export_modules_as_functions`. "
                                   "Got `%s`." % (type(v).__name__))
        trace_module_map = __setup_trace_module_map()
        module_typenames = {_find_typename(v) for v in export_modules_as_functions}
        export_modules_as_functions = module_typenames
    else:
        export_modules_as_functions = None
    return export_modules_as_functions

def _reset_trace_module_map():
    torch.jit._trace._trace_module_map = None

def _export(model, args, f, export_params=True, verbose=False, training=None,
            input_names=None, output_names=None, operator_export_type=OperatorExportTypes.ONNX,
            export_type=ExportTypes.PROTOBUF_FILE, opset_version=None,
            do_constant_folding=True, dynamic_axes=None, keep_initializers_as_inputs=None,
            fixed_batch_size=False, custom_opsets=None, add_node_names=True,
            onnx_shape_inference=True, export_modules_as_functions=False):

    export_modules_as_functions = _setup_trace_module_map(model, export_modules_as_functions)

    if isinstance(model, torch.nn.DataParallel):
        raise ValueError("torch.nn.DataParallel is not supported by ONNX "
                         "exporter, please use 'attribute' module to "
                         "unwrap model from torch.nn.DataParallel. Try "
                         "torch.onnx.export(model.module, ...)")
    global __IN_ONNX_EXPORT
    assert __IN_ONNX_EXPORT is False
    __IN_ONNX_EXPORT = True
    try:
        from torch.onnx.symbolic_helper import _set_onnx_shape_inference
        _set_onnx_shape_inference(onnx_shape_inference)

        from torch.onnx.symbolic_helper import _default_onnx_opset_version, _set_opset_version
        from torch.onnx.symbolic_helper import _set_operator_export_type
        if opset_version is None:
            opset_version = _default_onnx_opset_version
        if not operator_export_type:
            if torch.onnx._CAFFE2_ATEN_FALLBACK:
                operator_export_type = OperatorExportTypes.ONNX_ATEN_FALLBACK
            else:
                operator_export_type = OperatorExportTypes.ONNX

        # By default, training=None, (which defaults to TrainingMode.EVAL),
        # which is good because running a model in training mode could result in
        # internal buffers getting updated, dropout getting applied, etc.
        # If you really know what you're doing, you can turn
        # training=TrainingMode.TRAINING or training=TrainingMode.PRESERVE,
        # (to preserve whatever the original training mode was.)
        _set_opset_version(opset_version)
        _set_operator_export_type(operator_export_type)
        with exporter_context(model, training):
            val_keep_init_as_ip = _decide_keep_init_as_input(keep_initializers_as_inputs,
                                                             operator_export_type,
                                                             opset_version)
            val_add_node_names = _decide_add_node_names(add_node_names, operator_export_type)
            val_do_constant_folding = _decide_constant_folding(do_constant_folding, operator_export_type, training)
            # Normally f can be a file-like object, but for large models, the external data format requires a
            # valid `model_file_location`. Code in export.cpp will enforce this.
            if isinstance(f, str):
                model_file_location = f
            else:
                model_file_location = str()
            args = _decide_input_format(model, args)
            if dynamic_axes is None:
                dynamic_axes = {}
            _validate_dynamic_axes(dynamic_axes, model, input_names, output_names)

            graph, params_dict, torch_out = \
                _model_to_graph(model, args, verbose, input_names,
                                output_names, operator_export_type,
                                val_do_constant_folding,
                                fixed_batch_size=fixed_batch_size,
                                training=training,
                                dynamic_axes=dynamic_axes)

            # TODO: Don't allocate a in-memory string for the protobuf
            defer_weight_export = export_type is not ExportTypes.PROTOBUF_FILE
            if custom_opsets is None:
                custom_opsets = {}

            torch._C._jit_pass_dce_allow_deleting_nodes_with_side_effects(graph)
            node_attr_to_name = {}  # type: ignore[var-annotated]
            if export_modules_as_functions is not None:
                # NOTE: cannot call DCE after this pass. DCE will remove function definition nodes.
                node_attr_to_name = torch._C._jit_pass_onnx_function_extraction(
                    graph, export_modules_as_functions, list(params_dict.keys()))
            params_dict = torch._C._jit_pass_onnx_deduplicate_initializers(graph, params_dict,
                                                                           training == TrainingMode.TRAINING)
            if export_params:
                proto, export_map, val_use_external_data_format = graph._export_onnx(
                    params_dict, opset_version, dynamic_axes, defer_weight_export,
                    operator_export_type, not verbose, val_keep_init_as_ip, custom_opsets,
                    val_add_node_names, model_file_location, node_attr_to_name)
            else:
                proto, export_map, val_use_external_data_format = graph._export_onnx(
                    {}, opset_version, dynamic_axes, False, operator_export_type,
                    not verbose, val_keep_init_as_ip, custom_opsets, val_add_node_names,
                    model_file_location, node_attr_to_name)
            if export_type == ExportTypes.PROTOBUF_FILE:
                assert(len(export_map) == 0)
                with torch.serialization._open_file_like(f, "wb") as opened_file:
                    opened_file.write(proto)
            elif export_type in [ExportTypes.ZIP_ARCHIVE, ExportTypes.COMPRESSED_ZIP_ARCHIVE]:
                import zipfile
                compression = zipfile.ZIP_DEFLATED \
                    if export_type == ExportTypes.COMPRESSED_ZIP_ARCHIVE \
                    else zipfile.ZIP_STORED
                with zipfile.ZipFile(f, "w", compression=compression) as z:
                    z.writestr(ONNX_ARCHIVE_MODEL_PROTO_NAME, proto)
                    for k, v in export_map.items():
                        z.writestr(k, v)
            elif export_type == ExportTypes.DIRECTORY:
                import os
                if os.path.exists(f):
                    assert(os.path.isdir(f))
                else:
                    os.makedirs(f)

                model_proto_file = os.path.join(f, ONNX_ARCHIVE_MODEL_PROTO_NAME)
                with torch.serialization._open_file_like(model_proto_file, "wb") as opened_file:
                    opened_file.write(proto)

                for k, v in export_map.items():
                    weight_proto_file = os.path.join(f, k)
                    with torch.serialization._open_file_like(weight_proto_file, "wb") as opened_file:
                        opened_file.write(v)
            else:
                raise RuntimeError("Unknown export type")

            # The ONNX checker only works for ONNX graph. So if the operator_export_type is not ONNX,
            # we can skip this check.
            # If large model format export is enabled, proto will only contain data location instead of
            # raw data and _check_onnx_proto() will fail because it can only handle the raw ONNX proto
            # string in memory.
            if (operator_export_type is OperatorExportTypes.ONNX) and (not val_use_external_data_format):
                try:
                    _check_onnx_proto(proto)
                except RuntimeError as e:
                    raise CheckerError(e)
    finally:
        assert __IN_ONNX_EXPORT
        __IN_ONNX_EXPORT = False
        _reset_trace_module_map()
    return torch_out


def _apply_friendly_debug_names(graph, params):
    for n in graph.nodes():
        for v in n.inputs():
            old_name = v.debugName()
            if old_name != str(v.unique()):
                continue
            new_name = f"{n.kind()}_{v.unique()}"
            v.setDebugName(new_name)
            if old_name in params:
                params[new_name] = params.pop(old_name)


def _set_input_and_output_names(graph, input_names, output_names):
    def set_names(node_list, name_list, descriptor):
        if name_list is None:
            return
        if len(name_list) > len(node_list):
            raise RuntimeError(
                "number of %s names provided (%d) exceeded number of %ss (%d)"
                % (descriptor, len(name_list), descriptor, len(node_list)))

        # Mark if the output node DebugName is set before.
        output_node_set = set()
        for i, (name, node) in enumerate(zip(name_list, node_list)):
            # Duplicated output node, insert onnx::Identity to avoid setting the same DebugName after setDebugName().
            if descriptor == "output":
                if node in output_node_set:
                    identity_node = graph.create("onnx::Identity")
                    identity_node.insertAfter(node.node())
                    identity_node.addInput(node)
                    identity_node.output().setType(node.type())
                    graph.return_node().replaceInput(i, identity_node.output())
                    node = identity_node.output()
                output_node_set.add(node)

            if node.debugName() != name:
                node.setDebugName(name)

    set_names(list(graph.inputs()), input_names, "input")
    set_names(list(graph.outputs()), output_names, "output")


attr_pattern = re.compile("^(.+)_([ifstgz])$")


def _run_symbolic_method(g, op_name, symbolic_fn, args):
    r"""
    This trampoline function gets invoked for every symbolic method
    call from C++.
    """
    try:
        return symbolic_fn(g, *args)
    except TypeError as e:
        # Handle the specific case where we didn't successfully dispatch
        # to symbolic_fn.  Otherwise, the backtrace will have the clues
        # you need.
        e.args = ("{} (occurred when translating {})".format(e.args[0], op_name),)
        raise


def _is_onnx_list(value):
    if not isinstance(value, string_classes) and \
            not isinstance(value, torch.Tensor) and \
            isinstance(value, collections.abc.Iterable):
        return True
    return False


def _add_attribute(node, key, value, aten):
    r""" initializes the right attribute based on type of value """
    m = attr_pattern.match(key)
    if m is None:
        raise IndexError((
            "Invalid attribute specifier '{}' names " +
            " must be suffixed with type, e.g. 'dim_i' or 'dims_i'").format(key))
    name, kind = m.group(1), m.group(2)
    if _is_onnx_list(value):
        kind += "s"
    if aten:
        if isinstance(value, torch.Tensor):
            # Caffe2 proto does not support tensor attribute.
            if value.numel() > 1:
                raise ValueError("Should not pass tensor attribute")
            value = _scalar(value)
            if isinstance(value, float):
                kind = "f"
            else:
                kind = "i"
    return getattr(node, kind + "_")(name, value)


def _scalar(x):
    """Convert a scalar tensor into a Python value."""
    assert x.numel() == 1
    return x[0]


def _newNode(g, opname, outputs, *args, **kwargs):
    if "::" in opname:
        aten = False
        ns_opname = opname
    else:
        aten = kwargs.pop("aten", False)
        ns = "aten" if aten else "onnx"
        ns_opname = ns + "::" + opname
    n = g.create(ns_opname, args, outputs)
    for k, v in sorted(kwargs.items()):
        # TODO: enable inplace in aten exporting mode.
        if k == "inplace":
            continue
        _add_attribute(n, k, v, aten=aten)
    return n


def _graph_op(g, opname, *raw_args, **kwargs):
    r"""
    Create an ONNX operator "opname", taking "args" as inputs and attributes
    "kwargs"; returning the node representing the single output of this operator
    (see the `outputs` keyword argument for multi-return nodes).

    The set of operators and the inputs/attributes they take
    is documented at https://github.com/onnx/onnx/blob/master/docs/Operators.md

    This function is monkey-patched onto Graph.

    Args:
        opname (string): The ONNX operator name, e.g., `Abs` or `Add`.
        args (Node...): The inputs to the operator; usually provided
            as arguments to the `symbolic` definition.
        kwargs: The attributes of the ONNX operator, with keys named
            according to the following convention: `alpha_f` indicates
            the `alpha` attribute with type `f`.  The valid type specifiers are
            `f` (float), `i` (int), `s` (string) or `t` (Tensor).  An attribute
            specified with type float accepts either a single float, or a
            list of floats (e.g., you would say `dims_i` for a `dims` attribute
            that takes a list of integers).
        outputs (int, optional):  The number of outputs this operator returns;
            by default an operator is assumed to return a single output.
            If `outputs` is greater than one, this functions returns a tuple
            of output `Node`, representing each output of the ONNX operator
            in positional.
    """
    outputs = kwargs.pop("outputs", 1)

    # Filter out None attributes, this can be convenient client side because
    # now they can pass through None attributes, and have them not show up
    kwargs = dict((k, v) for k, v in kwargs.items() if v is not None)

    def const_if_tensor(arg):
        if arg is None:
            return arg
        elif isinstance(arg, torch._C.Value):
            return arg
        else:
            return g.op("Constant", value_z=arg)

    args = list(const_if_tensor(arg) for arg in raw_args)
    n = g.insertNode(_newNode(g, opname, outputs, *args, **kwargs))

    from torch.onnx.symbolic_helper import _onnx_shape_inference
    if _onnx_shape_inference:
        from torch.onnx.symbolic_helper import _export_onnx_opset_version as opset_version
        torch._C._jit_pass_onnx_node_shape_type_inference(n, _params_dict, opset_version)

    if outputs == 1:
        return n.output()
    return tuple(o for o in n.outputs())


def _block_op(b, opname, *args, **kwargs):
    if "::" in opname:
        aten = False
        ns_opname = opname
    else:
        aten = kwargs.pop("aten", False)
        ns = "aten" if aten else "onnx"
        ns_opname = ns + "::" + opname
    n = b.addNode(ns_opname, list(args))
    for k, v in sorted(kwargs.items()):
        # TODO: enable inplace in aten exporting mode.
        if k == "inplace":
            continue
        _add_attribute(n, k, v, aten=aten)
    if len(list(n.outputs())) == 1:
        return n.output()
    return tuple(o for o in n.outputs())


def _add_block(node):
    return node.addBlock()


def _add_input_to_block(block):
    return block.addInputToBlock()


def _add_output_to_block(block, value):
    new_output = block.registerOutput(value)
    return new_output


# Note [Export inplace]
# ~~~~~~~~~~~~~~~~~~~~~
# In abstract, it would be better for us to export inplace annotations,
# than to not export them, since it is useful information that can
# help the target of an ONNX export export more efficiently.  However,
# ONNX doesn't currently formalize inplace. Fortunately, it's sound to drop
# inplace annotations, but we are losing information this way.


def _find_symbolic_in_registry(domain, op_name, opset_version, operator_export_type):
    import torch.onnx.symbolic_registry as sym_registry
    if not sym_registry.is_registered_op(op_name, domain, opset_version):
        if operator_export_type == OperatorExportTypes.ONNX_FALLTHROUGH:
            # Use the original node directly
            return None
    return sym_registry.get_registered_op(op_name, domain, opset_version)


def _should_aten_fallback(ns, op_name, opset_version, operator_export_type):
    import torch.onnx.symbolic_registry as sym_registry
    is_exportable_aten_op = sym_registry.is_registered_op(op_name, "", opset_version)
    is_onnx_aten_export = operator_export_type == OperatorExportTypes.ONNX_ATEN
    is_aten_fallback_export = operator_export_type == OperatorExportTypes.ONNX_ATEN_FALLBACK
    return is_onnx_aten_export or (not is_exportable_aten_op and is_aten_fallback_export)


def _need_symbolic_context(symbolic_fn):
    # Check if the first argument to symbolic_fn is annotated as type `torch.onnx.SymbolicContext`
    params = list(inspect.signature(symbolic_fn).parameters.values())
    return params and issubclass(params[0].annotation, SymbolicContext)

def _run_symbolic_function(g, block, n, inputs, env, operator_export_type=OperatorExportTypes.ONNX):
    # NB: Returning None means the node gets cloned as is into
    # the new graph
    try:
        import torch
        from torch.onnx.symbolic_helper import _export_onnx_opset_version as opset_version
        import torch.onnx.symbolic_registry as sym_registry

        sym_registry.register_version("", opset_version)

        # Caffe2-specific: Quantized op symbolics are registered for opset 9 only.
        is_caffe2_aten_fallback = (operator_export_type == OperatorExportTypes.ONNX_ATEN_FALLBACK and
                                   torch.onnx._CAFFE2_ATEN_FALLBACK)
        if is_caffe2_aten_fallback and opset_version == 9:
            import torch.onnx.symbolic_caffe2
            torch.onnx.symbolic_caffe2.register_quantized_ops("caffe2", opset_version)

        # See Note [Export inplace]
        # TODO: I think this is not necessary anymore
        if n.kind().endswith("_"):
            ns_op_name = n.kind()[:-1]
        else:
            ns_op_name = n.kind()
        ns, op_name = ns_op_name.split("::")

        domain = ns
        if ns == "aten":
            domain = ""
        if ns == "quantized":
            domain = ""
            # Caffe2-specific quantized op
            if is_caffe2_aten_fallback:
                domain = "caffe2"

        if sym_registry.is_registered_op(op_name, domain, opset_version):
            symbolic_fn = _find_symbolic_in_registry(domain, op_name, opset_version, operator_export_type)
            attrs = {k: n[k] for k in n.attributeNames()}
            if _need_symbolic_context(symbolic_fn):
                ctx = SymbolicContext(_params_dict, env, n, block)
                return symbolic_fn(ctx, g, *inputs, **attrs)
            # TODO: https://msdata.visualstudio.com/Vienna/_workitems/edit/1408006
            # PythonOp symbolic need access to the node to resolve the name conflict,
            # this is inconsistent with regular op symbolic.
            if op_name == "PythonOp":
                inputs = (n, *inputs)
            return symbolic_fn(g, *inputs, **attrs)
        elif ns == "onnx":
            # Clone node to trigger ONNX shape inference
            attrs = {k + "_" + n.kindOf(k)[0]: n[k] for k in n.attributeNames()}
            return g.op(op_name, *inputs, **attrs, outputs=n.outputsSize())
        elif _should_aten_fallback(ns, op_name, opset_version, operator_export_type):
            # Direct ATen export requested
            attrs = {k + "_" + n.kindOf(k)[0]: n[k] for k in n.attributeNames()}
            outputs = n.outputsSize()
            attrs["outputs"] = outputs
            return g.at(op_name, *inputs, aten=True, **attrs)
        else:
            raise sym_registry.UnsupportedOperatorError(domain, op_name, opset_version)
    except RuntimeError:
        if operator_export_type == OperatorExportTypes.ONNX_FALLTHROUGH:
            return None
        raise
    except TypeError as e:
        # Handle the specific case where we didn't successfully dispatch.
        # Otherwise, the backtrace will have the clues you need.
        e.args = ("{} \n(Occurred when translating {}).".format(e.args[0], op_name),)
        raise


# Generate an ONNX ATen op node.
def _aten_op(g, operator, *args, overload_name="", **kwargs):
    return g.op("ATen", *args, operator_s=operator, overload_name_s=overload_name, **kwargs)


# This helper function can create either constant tensor or constant scalar.
# If dims is None or 0 or [0], generate a 0-d tensor (scalar).
#
# TODO: We might not need this anymore, since most scalars now show up
# as tensors
def _graph_constant(g, value, dims, type, *args, **kwargs):
    assert isinstance(value, numbers.Number)
    assert type is not None
    isscalar = False
    if dims is None or dims == 0 or set(dims) == set([0]):
        dims = [1]
        isscalar = True
    type = type.lower()
    tensor: Union[torch.CharTensor, torch.ShortTensor,
                  torch.IntTensor, torch.LongTensor,
                  torch.HalfTensor, torch.FloatTensor,
                  torch.DoubleTensor]
    if type == "char":
        tensor = torch.CharTensor(*dims)
    elif type == "short":
        tensor = torch.ShortTensor(*dims)
    elif type == "int":
        tensor = torch.IntTensor(*dims)
    elif type == "long":
        tensor = torch.LongTensor(*dims)
    elif type == "half":
        tensor = torch.HalfTensor(*dims)
    elif type == "float":
        tensor = torch.FloatTensor(*dims)
    elif type == "double":
        tensor = torch.DoubleTensor(*dims)
    else:
        raise ValueError("Unknown type, type should be one of the following strings: "
                         "char, short, int, long, half, float, double")
    tensor.fill_(value)  # type: ignore[call-overload]
    if isscalar:
        return g.op("Constant", *args, value_z=tensor, **kwargs)
    return g.op("Constant", *args, value_t=tensor, **kwargs)


def _node_getitem(self, k):
    r"""
    Accessor for attributes of a node which is polymorphic over
    return type.

    NB: This is monkey-patched onto Node.
    """
    sel = self.kindOf(k)
    return getattr(self, sel)(k)


def get_ns_op_name_from_custom_op(symbolic_name):
    if not bool(re.match(r"^[a-zA-Z0-9-_]*::[a-zA-Z-_]+[a-zA-Z0-9-_]*$", symbolic_name)):
        raise ValueError("Failed to register operator {}. \
                          The symbolic name must match the format Domain::Name, \
                          and should start with a letter and contain only \
                          alphanumerical characters".format(symbolic_name))
    ns, op_name = symbolic_name.split("::")
    if ns == "onnx":
        raise ValueError("Failed to register operator {}. \
                          {} domain cannot be modified."
                         .format(symbolic_name, ns))

    if ns == "aten":
        ns = ""

    return ns, op_name


# When the user registers symbolic for custom/contrib ops,
# it is highly recommended to add shape inference for that operator via setType API,
# otherwise the exported graph may have incorrect shape inference in some extreme cases.
# An example of setType is test_aten_embedding_2 in test_operators.py..
def register_custom_op_symbolic(symbolic_name, symbolic_fn, opset_version):
    ns, op_name = get_ns_op_name_from_custom_op(symbolic_name)
    import torch.onnx.symbolic_registry as sym_registry
    from torch.onnx.symbolic_helper import _onnx_stable_opsets, _onnx_main_opset

    for version in _onnx_stable_opsets + [_onnx_main_opset]:
        if version >= opset_version:
            sym_registry.register_op(op_name, symbolic_fn, ns, version)


def unregister_custom_op_symbolic(symbolic_name, opset_version):
    ns, op_name = get_ns_op_name_from_custom_op(symbolic_name)
    import torch.onnx.symbolic_registry as sym_registry
    from torch.onnx.symbolic_helper import _onnx_stable_opsets, _onnx_main_opset

    for version in _onnx_stable_opsets + [_onnx_main_opset]:
        if version >= opset_version:
            sym_registry.unregister_op(op_name, ns, version)


# This helper function ensures dynamic axes argument is following the expected format
def _validate_dynamic_axes(dynamic_axes, model, input_names, output_names):
    if len(dynamic_axes) == 0:
        return

    if(hasattr(model, "graph")):
        # Extracting set of valid input/output names that shall be used for dynamic_axes
        if (input_names is None) or len(input_names) == 0:
            input_names = [x.debugName() for x in model.graph.inputs()]
        if (output_names is None) or len(output_names) == 0:
            output_names = [y.debugName() for y in model.graph.outputs()]

    valid_names = set((input_names or []) + (output_names or []))

    # If dynamic axes are provided as a list rather than dictionary, they should
    # first get converted to a dictionary in expected format. If desired axes names
    # are not provided for dynamic axes, automatic names shall be generated for
    # provided dynamic axes of specified input/output
    for key, value in dynamic_axes.items():
        if key not in valid_names:
            warnings.warn("Provided key {} for dynamic axes is not a valid input/output name".format(key))
        if isinstance(value, list):
            warnings.warn("No names were found for specified dynamic axes of provided input."
                          "Automatically generated names will be applied to each dynamic axes of input {}".format(key))

            value_dict = {}
            for i, x in enumerate(value):
                if not isinstance(x, int):
                    raise ValueError("The type of axis index is expected to be an integer")
                if x in value_dict:
                    warnings.warn("Duplicate dynamic axis index {} was provided for input {}."
                                  .format(x, key))
                else:
                    value_dict[x] = str(key) + "_dynamic_axes_" + str(i + 1)
            dynamic_axes[key] = value_dict


torch._C.Graph.op = _graph_op  # type: ignore[attr-defined]
torch._C.Graph.at = _aten_op  # type: ignore[attr-defined]
torch._C.Block.op = _block_op  # type: ignore[attr-defined]
torch._C.Graph.constant = _graph_constant  # type: ignore[attr-defined]
torch._C.Node.__getitem__ = _node_getitem  # type: ignore[attr-defined, misc, assignment]
