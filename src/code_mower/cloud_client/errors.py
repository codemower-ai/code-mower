"""Shared CodeMower.com client exceptions."""

from __future__ import annotations


class CloudBundleError(ValueError):
    """Raised when a cloud bundle or upload request is unsafe or invalid."""
