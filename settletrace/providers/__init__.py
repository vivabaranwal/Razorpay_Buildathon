"""Data providers: the Razorpay sandbox client and the sample-data generator.

Both satisfy the same informal interface so the reconciliation engine cannot
tell them apart. That is what lets the demo run with no credentials and no
network, while the same code path serves live sandbox data when configured.
"""

from .base import DataProvider
from .factory import get_provider

__all__ = ["DataProvider", "get_provider"]
