"""Amazon Advertising Products page automation.

Playwright opens Campaign Manager's **Products** page (Advertised products),
selects the Today or Yesterday date range, shows 200 results per page and
exports every page.  The page exports are merged, validated with pandas, and
the date is replaced in ``powerbidb.Products``.

Replaces the manual ``products_secondary_upload.py`` script.  Built on the
Amazon_ad_targeting_report_cron codebase: the browser session, sign-in,
selector, retry, lock and logging modules are reused from it.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
