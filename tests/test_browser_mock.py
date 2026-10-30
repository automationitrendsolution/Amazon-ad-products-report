"""The real Playwright code against a local imitation of the Products page.

The mock has the controls the recorded steps use -- Date range with Today /
Yesterday and Apply, "Results per page", Export (downloads the visible page
only), and the next-page arrow -- so the whole page loop runs in Chromium
without Amazon.  Skipped when Chromium is not installed.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.browser import BrowserSession
from app.exceptions import DateMismatchError
from app.periods import ResolvedPeriod
from app.products_csv import combine_exports
from app.products_page import ProductsPage

from .conftest import HEADER

PRODUCTS = 437  # 3 pages of 200

MOCK_PAGE = """<!doctype html><html><body>
<div role="tablist"><button role="tab" aria-selected="true">Advertised products</button></div>
<div>Filter <span>Clicks: greater than 0</span></div>
<button id="range">Date range: Oct 1, 2026</button>
<div id="picker" hidden>
  <ul role="listbox"><li role="option">Today</li><li role="option">Yesterday</li></ul>
  <button id="apply">Apply</button>
</div>
<button id="export">Export</button>
<div role="grid"><div id="rows"></div></div>
<button id="perpage">Results per page: 25</button>
<ul id="sizes" role="listbox" hidden>
  <li role="option">25</li><li role="option">50</li><li role="option">100</li><li role="option">200</li>
</ul>
<span id="footer"></span>
<button aria-label="Next page" id="next">&gt;</button>
<script>
const TOTAL = __TOTAL__, HEADER = __HEADER__;
let size = 25, first = 1, chosen = null, label = "Oct 1, 2026";
const LABELS = {Today: "Oct 29, 2026", Yesterday: "__YESTERDAY__"};
function render() {
  const last = Math.min(first + size - 1, TOTAL);
  document.getElementById("footer").textContent = `${first}-${last} of ${TOTAL} results`;
  document.getElementById("range").textContent = `Date range: ${label}`;
  document.getElementById("perpage").textContent = `Results per page: ${size}`;
  document.getElementById("next").disabled = last >= TOTAL;
}
function line(i) {
  return `"Product ${i}",Delivering,Product,Eligible,"$${i}.50",9.71%,"1,${String(i).padStart(3, "0")}",${i},"$1.00",1,0,"$0.00"`;
}
document.getElementById("range").onclick = () => document.getElementById("picker").hidden = false;
document.querySelectorAll("#picker li").forEach(li => li.onclick = () => chosen = li.textContent);
document.getElementById("apply").onclick = () => {
  label = LABELS[chosen]; first = 1; document.getElementById("picker").hidden = true; render();
};
document.getElementById("perpage").onclick = () => document.getElementById("sizes").hidden = false;
document.querySelectorAll("#sizes li").forEach(li => li.onclick = () => {
  document.getElementById("sizes").hidden = true;
  setTimeout(() => { size = Number(li.textContent); first = 1; render(); }, 300);
});
document.getElementById("next").onclick = () => setTimeout(() => { first += size; render(); }, 300);
document.getElementById("export").onclick = () => {
  const rows = [HEADER];
  for (let i = __CUMULATIVE__ ? 1 : first; i <= Math.min(first + size - 1, TOTAL); i++) rows.push(line(i));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([rows.join("\\n") + "\\n"], {type: "text/csv"}));
  a.download = "Products_Oct_29_2026.csv";
  document.body.appendChild(a); a.click(); a.remove();
};
render();
</script></body></html>"""


def mock_page(yesterday_label: str = "Oct 28, 2026", *, cumulative: bool = False) -> str:
    """``cumulative``: Export downloads rows 1 to the end of the current page, as the live site does."""
    return (
        MOCK_PAGE.replace("__CUMULATIVE__", "true" if cumulative else "false")
        .replace("__TOTAL__", str(PRODUCTS))
        .replace("__HEADER__", json.dumps(HEADER))
        .replace("__YESTERDAY__", yesterday_label)
    )


@pytest.fixture
def products_page(settings):
    try:
        session = BrowserSession(settings, headless=True)
        session.start()
    except Exception as exc:  # pragma: no cover - environment specific
        pytest.skip(f"Chromium not available: {exc}")
    products = ProductsPage(session, settings)
    products.export_gap_s = 0
    try:
        yield products
    finally:
        session.close()


def test_exports_every_page_of_yesterday(products_page):
    products_page.page.set_content(mock_page())
    yesterday = ResolvedPeriod("yesterday", date(2026, 10, 28))

    products_page.check_required_filter()
    products_page.select_period(yesterday)
    products_page.set_results_per_page(200)
    paged = products_page.export_all(yesterday)

    assert paged.total == PRODUCTS
    assert len(paged.files) == 3
    export = combine_exports(paged.paths, expected_total=paged.total)
    assert export.row_count == PRODUCTS
    assert export.frame["Products"].iloc[[0, 200, -1]].tolist() == ["Product 1", "Product 201", "Product 437"]


def test_cumulative_export_uses_the_export_that_holds_every_row(products_page, settings):
    products_page.page.set_content(mock_page(cumulative=True))
    yesterday = ResolvedPeriod("yesterday", date(2026, 10, 28))

    products_page.select_period(yesterday)
    products_page.set_results_per_page(200)
    paged = products_page.export_all(yesterday)

    assert len(paged.files) == 1 and "p03" in paged.files[0].path.name
    assert [f.name for f in settings.download_dir.glob("*.csv")] == [paged.files[0].path.name]  # partials removed
    export = combine_exports(paged.paths, expected_total=paged.total)
    assert export.row_count == PRODUCTS


def test_wrong_date_on_the_page_stops_before_export(products_page):
    products_page.page.set_content(mock_page(yesterday_label="Oct 27, 2026"))
    with pytest.raises(DateMismatchError):
        products_page.select_period(ResolvedPeriod("yesterday", date(2026, 10, 28)))


FILTER_BAR = """<!doctype html><html><body>
<div id="bar">
  <div id="v:filterTag-clicks"><button>Clicks: greater than 0</button><button>remove</button></div>
  <div id="v:filterTag-orders"><button>Orders: equals 0</button><button>remove</button></div>
</div>
<script>
document.querySelectorAll("[id*=':filterTag-'] button:last-child").forEach(b =>
  b.onclick = () => setTimeout(() => b.parentElement.remove(), 300));
</script></body></html>"""


def test_extra_filter_is_removed_and_the_required_one_kept(products_page):
    products_page.page.set_content(FILTER_BAR)
    products_page.check_required_filter()
    tags = products_page.page.locator("[id*=':filterTag-']")
    assert tags.count() == 1 and tags.first.get_attribute("id") == "v:filterTag-clicks"
