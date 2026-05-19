"""Tests for serialize_value utility."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from app.services.lance_service import _clean_float, serialize_value


# ── NaN/Inf filtering ────────────────────────────────────────────


@pytest.mark.parametrize(
    "val,expected",
    [
        (1.0, 1.0),
        (0.0, 0.0),
        (-3.14, -3.14),
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
    ],
)
def test_clean_float(val, expected):
    assert _clean_float(val) == expected


# ── Scalar types ─────────────────────────────────────────────────


def test_none():
    assert serialize_value(None) is None


def test_string_passthrough():
    assert serialize_value("hello") == "hello"


def test_int_passthrough():
    assert serialize_value(42) == 42


def test_float_normal():
    assert serialize_value(3.14) == 3.14


def test_float_nan():
    assert serialize_value(float("nan")) is None


# ── Temporal types ───────────────────────────────────────────────


def test_datetime():
    dt = datetime(2024, 6, 15, 12, 30, 0)
    assert serialize_value(dt) == "2024-06-15T12:30:00"


def test_date():
    assert serialize_value(date(2024, 1, 1)) == "2024-01-01"


def test_time():
    assert serialize_value(time(14, 30)) == "14:30:00"


def test_timedelta():
    assert serialize_value(timedelta(hours=2, minutes=30)) == "2:30:00"


# ── Binary ───────────────────────────────────────────────────────


def test_binary_returns_size():
    result = serialize_value(b"some binary data")
    assert result == {"size": 16}


def test_binary_empty():
    result = serialize_value(b"")
    assert result == {"size": 0}


# ── Containers ───────────────────────────────────────────────────


def test_dict_recursive():
    val = {"a": 1, "b": float("nan"), "nested": {"c": datetime(2024, 1, 1)}}
    result = serialize_value(val)
    assert result["a"] == 1
    assert result["b"] is None
    assert result["nested"]["c"] == "2024-01-01T00:00:00"


def test_list_of_floats_cleans_nan():
    result = serialize_value([1.0, float("nan"), 3.0])
    assert result == [1.0, None, 3.0]


def test_list_of_mixed():
    result = serialize_value(["hello", 42, None])
    assert result == ["hello", 42, None]
