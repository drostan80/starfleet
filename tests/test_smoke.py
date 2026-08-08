"""Scaffold smoke test — confirms the project/test harness itself works.

Replace/expand once real app code lands in Phase A (BUILD_PLAN.md).
"""

import lcars


def test_package_importable():
    assert lcars.__version__
