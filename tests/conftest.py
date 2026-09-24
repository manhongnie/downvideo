from __future__ import annotations

from contextlib import ExitStack

import pytest

from demo.server import DemoServer


@pytest.fixture
def demo_site():
    """Start a separate local HTTP fixture; no public network is used."""
    with ExitStack() as stack:
        def start(total=137, per_page=10, **kwargs):
            return stack.enter_context(DemoServer(total, per_page, **kwargs))
        yield start
