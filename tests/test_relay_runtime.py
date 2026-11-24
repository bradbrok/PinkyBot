"""The relay's newer runtime requirement never makes daemon imports fail."""

import sys

import pytest

from pinky_relay.store import StoreRefusalError, require_runtime


def test_relay_refuses_python_311_loudly_before_opening_a_store():
    with pytest.raises(StoreRefusalError, match="relay requires Python >= 3.12"):
        require_runtime((3, 11, 99))


def test_relay_runtime_is_supported_or_refused_explicitly():
    if sys.version_info < (3, 12):
        with pytest.raises(StoreRefusalError, match="relay requires Python >= 3.12"):
            require_runtime()
    else:
        require_runtime()
