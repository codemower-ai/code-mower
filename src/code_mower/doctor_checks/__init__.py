"""Doctor check registry primitives.

The public CLI remains ``code-mower doctor``. This package keeps the
check/report data model independent from the large command adapter so runtime,
GitHub, provider, cloud, and output checks can move behind registries without
changing the CLI contract.
"""

from .models import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
    DoctorCheck,
    DoctorReport,
)
from .registry import DEFAULT_CHECK_GROUPS, DoctorCheckGroup, default_check_group_ids

__all__ = [
    "DEFAULT_CHECK_GROUPS",
    "DoctorCheck",
    "DoctorCheckGroup",
    "DoctorReport",
    "STATUS_FAIL",
    "STATUS_PASS",
    "STATUS_SKIP",
    "STATUS_WARN",
    "default_check_group_ids",
]
