"""A native library's panic is told from a request to stop by what it is not (`lakehouse.supervision.is_panic`).

pyo3 raises a Rust panic as ``pyo3_runtime.PanicException``, a BaseException that is no Exception and that no module
exports, so the panic catcher, the request deadline and maintenance's task handler each recognise one by elimination: it
is neither an Exception nor a request to stop. Mistaking a cancellation or ``SystemExit`` for a panic would answer a
request the server is stopping, or keep a process its owner told to exit; mistaking a panic for a stop would let it end
the loop it escaped. A group is a panic when it holds no request to stop.
"""

from __future__ import annotations

import asyncio

import pytest

from lakehouse.supervision import is_panic


class _Panic(BaseException):
    """Where ``pyo3_runtime.PanicException`` sits: a BaseException that is no Exception and no request to stop."""


@pytest.mark.parametrize(
    ("raised", "panic"),
    [
        pytest.param(RuntimeError("a fault"), False, id="an-exception"),
        pytest.param(asyncio.CancelledError(), False, id="a-cancellation"),
        pytest.param(SystemExit(70), False, id="an-exit"),
        pytest.param(_Panic("called `Option::unwrap()` on a `None` value"), True, id="a-native-panic"),
        pytest.param(BaseExceptionGroup("in a task group", [_Panic("a panic")]), True, id="a-group-of-a-panic"),
        pytest.param(BaseExceptionGroup("in a task group", [_Panic("a panic"), asyncio.CancelledError()]), False, id="a-group-with-a-stop"),
    ],
)
def test_a_panic_is_neither_an_exception_nor_a_request_to_stop(raised: BaseException, panic: bool) -> None:
    assert is_panic(raised) is panic
