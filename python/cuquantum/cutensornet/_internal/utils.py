"""
A collection of (internal use) helper functions.
"""

import functools
from typing import Callable, Dict, Optional

import cupy as cp
import numpy as np

from . import tensor_wrapper
from . import mem_limit

def infer_object_package(obj):
    """
    Infer the package that defines this object.
    """
    module = obj.__class__.__module__
    return module.split('.')[0]


def check_or_create_options(cls, options, options_description):
    """
    Create the specified options dataclass from a dictionary of options or None.
    """

    if options is None:
        options = cls()
    elif isinstance(options, Dict):
        options = cls(**options)

    if not isinstance(options, cls):
        raise TypeError(f"The {options_description} must be provided as an object " 
                        f"of type {cls.__name__} or as a dict with valid {options_description}. " 
                        f"The provided object is '{options}'.")

    return options


def get_or_create_stream(device, stream):
    """
    Create a stream object from a stream pointer or extract the stream pointer from a stream object.
    Return the stream object as well as the stream pointer.
    """

    if stream is None:
        with device: 
            stream = cp.cuda.get_current_stream()
            stream_ptr = stream.ptr
        return stream, stream_ptr

    if isinstance(stream, int):
        stream_ptr = stream
        stream = cp.cuda.ExternalStream(stream_ptr)

        return stream, stream_ptr

    module = infer_object_package(stream)

    if module not in ['cupy', 'torch']:
        raise TypeError("The CUDA stream must be specified as a CuPy or Torch stream object. "
                        "Alternatively, the stream pointer can be directly provided as an int.")

    if module == 'cupy':
        stream_ptr = stream.ptr

    if module == 'torch':
        stream_ptr = stream.cuda_stream
        stream = cp.cuda.ExternalStream(stream_ptr)

    return stream, stream_ptr


def get_memory_limit(memory_limit, device):
    """
    Parse user provided memory limit and return the memory limit in bytes.
    """
    import re

    _, total_memory = device.mem_info
    if isinstance(memory_limit, (int, float)):
        if memory_limit <= 0:
            raise ValueError("The specified memory limit must be greater than 0.")
        if memory_limit < 1:
            memory_limit *= total_memory
        return int(memory_limit)

    m = mem_limit.MEM_LIMIT_RE_PCT.match(memory_limit)
    if m:
        factor = float(m.group(1))
        if factor <= 0 or factor > 100:
            raise ValueError("The memory limit percentage must be in the range (0, 100].")
        return int(factor * total_memory / 100.)

    m = mem_limit.MEM_LIMIT_RE_VAL.match(memory_limit)
    if not m:
        raise ValueError(mem_limit.MEM_LIMIT_DOC % memory_limit)

    base = 1000
    if m.group('binary'):
        base = 1024

    powers = { '' : 0, 'k' : 1, 'm' : 2, 'g' : 3 }
    unit = m.group('units').lower() if m.group('units') else ''
    multiplier = base ** powers[unit]

    value = float(m.group('value'))
    memory_limit = int(value * multiplier)

    return memory_limit


def get_operands_data(operands):
    """
    Get the raw data pointer of the input operands and their alignment for cutensornet.
    """
    op_data = tuple(o.data_ptr for o in operands)    
    alignments = tuple(get_maximal_alignment(p) for p in op_data)
    return op_data, alignments


def create_empty_tensor(cls, extents, dtype, device):
    """
    Create a wrapped tensor of the same type as (the wrapped) cls on the specified device having the 
    specified extents and dtype.
    """
    tensor = cls.empty(extents, dtype=dtype, device=device)
    tensor = tensor_wrapper.wrap_operand(tensor)
    return tensor


def create_output_tensor(cls, output, size_dict, device_id, data_type):
    """
    Create output tensor and associated data (modes, extents, strides, alignment)
    """
    modes = tuple(m for m in output)
    extents = tuple(size_dict[m] for m in output)

    output = create_empty_tensor(cls, extents, data_type, device_id)

    strides = output.strides
    alignment = get_maximal_alignment(output.data_ptr)

    return output, modes, extents, strides, alignment


def get_network_device_id(operands):
    """
    Return the id (ordinal) of the device the tensor network is on, or None if it is on the CPU.
    """
    device_id = operands[0].device_id
    if not all(operand.device_id == device_id for operand in operands):
        devices = set(operand.device_id for operand in operands)
        raise ValueError(f"All tensors in the network are not on the same device. Devices = {devices}.")

    return device_id


def get_operands_dtype(operands):
    """
    Return the data type name of the tensors.
    """
    dtype = operands[0].dtype
    if not all(operand.dtype == dtype for operand in operands):
        dtypes = set(operand.dtype for operand in operands)
        raise ValueError(f"All tensors in the network must have the same data type. Data types found = {dtypes}.")
    return dtype


def get_maximal_alignment(address):
    """
    Calculate the maximal alignment of the provided memory location.
    """
    alignment = 1
    while address % alignment == 0 and alignment < 256:
        alignment *= 2

    return alignment


def check_operands_match(orig_operands, new_operands, attribute, description):
    """
    Check if the specified attribute matches between the corresponding new and old operands, and raise an exception if it 
    doesn't.
    """
    checks = [getattr(o, attribute) == getattr(n, attribute) for o, n in zip(orig_operands, new_operands)]

    if not all(checks): 
        mismatch = [f"{location}: {getattr(orig_operands[location], attribute)} => {getattr(new_operands[location], attribute)}"
                        for location, predicate in enumerate(checks) if predicate is False]
        mismatch = np.array2string(np.array(mismatch, dtype='object'), separator=', ', formatter={'object': lambda s: s})
        message = f"""The {description} of each new operand must match the {description} of the corresponding original operand.
The mismatch in {description} as a sequence of "operand position: original {description} => new {description}" is: \n{mismatch}"""
        raise ValueError(message)


def check_alignments_match(orig_alignments, new_alignments):
    """
    Check if alignment matches between the corresponding new and old operands, and raise an exception if it doesn't.
    """
    checks = [o == n for o, n in zip(orig_alignments, new_alignments)]

    if not all(checks): 
        mismatch = [f"{location}: {orig_alignments[location]} => {new_alignments[location]}" 
                        for location, predicate in enumerate(checks) if predicate is False] 
        mismatch = np.array2string(np.array(mismatch, dtype='object'), separator=', ', formatter={'object': lambda s: s})
        message = f"""The data alignment of each new operand must match the data alignment of the corresponding original operand.
The mismatch in data alignment as a sequence of "operand position: original alignment => new alignment" is: \n{mismatch}"""
        raise ValueError(message)


def convert_memory_with_units(memory):
    """
    Convert the provided memory value into a form suitable for printing.
    """
    base = 1024

    if memory < base:
        value, unit = memory, 'B'
    elif memory < base**2:
        value, unit = memory/base, 'KiB'
    elif memory < base**3:
        value, unit = memory/base**2, 'MiB'
    else:
        value, unit = memory/base**3, 'GiB'

    return value, unit


def check_autotune_params(iterations):
    """
    Check if the autotune parameters are of the correct type and within range.
    """

    if not isinstance(iterations, int):
        raise ValueError("Integer expected.")
    if iterations < 0:
        raise ValueError("Integer >= 0 expected.")

    message = f"Autotuning parameters: iterations = {iterations}."

    return message


# Decorator definitions

def atomic(handler: Callable[[Optional[object]], None], method: bool = False) -> Callable:
    """
    A decorator that provides "succeed or roll-back" semantics. A typical use for this is to release partial resources if an
    exception occurs.

    Args:
        handler: A function to call when an exception occurs. The handler takes a single argument, which is the exception
            object, and returns a boolean stating whether the same exception should be reraised. We assume that this function
            does not raise an exception.
        method: Specify if the wrapped function as well as the exception handler are methods bound to the same object 
            (method = True) or they are free functions (method = False). 

    Returns:
        Callable: A decorator that creates the wrapping. 
    """
    def outer(wrapped_function):
        """
        A decorator that actually wraps the function for exception handling.
        """
        @functools.wraps(wrapped_function)
        def inner(*args, **kwargs):
            """
            Call the wrapped function and return the result. If an exception occurs, then call the exception handler and
            reraise the exception.
            """
            try:
                result = wrapped_function(*args, **kwargs)
            except BaseException as e:
                if method:
                    flag = handler(args[0], e)
                else:
                    flag = handler(e)

                if flag:
                    raise e

            return result

        return inner

    return outer


def precondition(checker: Callable[..., None], what: str = "") -> Callable:
    """
    A decorator that adds checks to ensure any preconditions are met.

    Args:
        checker: The function to call to check whether the preconditions are met. It has the same signature as the wrapped
            function with the addition of the keyword argument `what`.
        what: A string that is passed in to `checker` to provide context information.

    Returns:
        Callable: A decorator that creates the wrapping. 
    """
    def outer(wrapped_function):
        """
        A decorator that actually wraps the function for checking preconditions.
        """
        @functools.wraps(wrapped_function)
        def inner(*args, **kwargs):
            """
            Check preconditions and if they are met, call the wrapped function.
            """
            checker(*args, **kwargs, what=what)
            result = wrapped_function(*args, **kwargs)

            return result

        return inner

    return outer


