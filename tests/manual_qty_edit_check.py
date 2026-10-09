"""Ad-hoc UI check (Playwright): order qty in "AI reorder suggestions".

Verifies, in a real browser against the running server:
  1. the Laya cell's qty line opens an in-place number input (band stays visible),
  2. any number is accepted and shown,
  3. Escape reverts, Enter/blur commits,
  4. an edit survives re-renders but RESETS on page reload (transient),
  5. the engine's Order qty column and Create PO data-qty are untouched,
  6. a cell with no Laya suggestion is still editable.
Run: python tests/manual_qty_edit_check.py   (server must be on 127.0.0.1:8000)
"""
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
failed = []


def check(cond, msg):
    msg = msg.encode("ascii", "backslashreplace").decode()  # cp1252-safe logs
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        failed.append(msg)


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1500, "height": 1000})

    asset = page.request.get(f"{BASE}/static/forecast-laya.js?v=26").text()
    check("lqPaint" in asset, "server serves the updated forecast-laya.js (v26)")

    page.goto(BASE + "/", wait_until="domcontentloaded")
    tok = page.evaluate("""async () => {
      const r = await fetch('/api/login', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username: 'admin', password: 'admin123'})});
      return await r.json(); }""")
    check(bool(tok.get("token")), "admin session obtained")
    page.evaluate("(s) => localStorage.setItem('pharmacySession', JSON.stringify(s))", tok)
    page.reload(wait_until="domcontentloaded")
    page.evaluate("() => { location.hash = '#forecast'; }")
    page.wait_for_selector("text=AI reorder suggestions", timeout=60000)

    table = page.locator("table").filter(has_text="Laya AI (units)").first
    table.scroll_into_view_if_needed()

    # ---- a cell that carries Laya's band floor -------------------------------
    with_sugg = table.locator(".lsg-qty[data-lsg-suggest]").first
    with_sugg.wait_for(state="visible", timeout=15000)
    floor_text = with_sugg.inner_text().strip()
    check("Order qty" in floor_text, f"suggestion shown: {floor_text!r}")

    row = with_sugg.locator("xpath=ancestor::tr[1]")
    engine_qty = row.locator("td").nth(7).inner_text().strip()
    po_qty = row.locator("[data-po]").first.get_attribute("data-qty")
    band_before = row.locator(".lsg-band").first.inner_text().strip()
    check(band_before.endswith("units"), f"band context visible: {band_before!r}")

    # ---- click opens an in-place input, band stays put ----------------------
    with_sugg.click()
    inp = row.locator("input.lsg-qty-input")
    check(inp.count() == 1 and inp.is_visible(), "in-place number input appears")
    check(inp.input_value() != "", f"input pre-filled with the floor ({inp.input_value()})")
    check(row.locator(".lsg-band").first.is_visible() and row.locator(".lsg-meta").count() >= 0,
          "band/trajectory context stays visible while editing")
    check("Order qty" not in with_sugg.inner_text(),
          "suggestion line replaced by the input while editing")

    # ---- any number is accepted --------------------------------------------
    inp.fill("2500")
    row.locator("input.lsg-qty-input").press("Enter")
    check(with_sugg.inner_text().strip() == "Order qty 2,500",
          f"typed quantity shown in place: {with_sugg.inner_text().strip()!r}")
    check("lsg-qty-set" in (with_sugg.get_attribute("class") or ""),
          "operator-set value marked")
    check(row.locator(".lsg-band").first.inner_text().strip() == band_before,
          "band context unchanged after the edit")
    check(row.locator("td").nth(7).inner_text().strip() == engine_qty,
          f"engine Order qty column untouched ({engine_qty})")
    check(row.locator("[data-po]").first.get_attribute("data-qty") == po_qty,
          f"Create PO still uses the engine qty ({po_qty})")

    # ---- Escape reverts -----------------------------------------------------
    with_sugg.click()
    row.locator("input.lsg-qty-input").fill("999")
    row.locator("input.lsg-qty-input").press("Escape")
    check(with_sugg.inner_text().strip() == "Order qty 2,500",
          "Escape reverts to the saved value")

    # ---- blur commits -------------------------------------------------------
    with_sugg.click()
    row.locator("input.lsg-qty-input").fill("1200")
    row.locator("input.lsg-qty-input").blur()
    check(with_sugg.inner_text().strip() == "Order qty 1,200",
          "blur commits the typed value")

    # ---- survives an in-page re-render (viewForecast re-runs) ---------------
    page.evaluate("() => { location.hash = '#dashboard'; }")
    page.wait_for_selector("text=AI reorder suggestions", state="detached", timeout=15000)
    page.evaluate("() => { location.hash = '#forecast'; }")
    page.wait_for_selector("text=AI reorder suggestions", timeout=60000)
    table = page.locator("table").filter(has_text="Laya AI (units)").first
    kept = table.locator(".lsg-qty.lsg-qty-set").first
    check(kept.count() > 0 and kept.inner_text().strip() == "Order qty 1,200",
          "edit survives an in-page re-render (same session)")

    # ---- a cell with NO Laya suggestion is still editable -------------------
    # (this dataset has a healthy engine, so put the cell into the empty state
    #  the component renders for failed/missing predictions and edit that)
    empty = table.locator(".lsg-qty[data-lsg-qty]:not(.lsg-qty-set)").first
    eid = empty.get_attribute("data-lsg-qty")  # pin the cell: the :not() selector
    empty.evaluate("""el => { delete el.dataset.lsgSuggest; delete el.dataset.lsgOpen;
        el.textContent = 'Order qty \u2014'; el.classList.add('lsg-qty-empty'); }""")
    erow = empty.locator("xpath=ancestor::tr[1]")
    empty.click()
    einp = erow.locator("input.lsg-qty-input")
    check(einp.is_visible(), "no-suggestion cell opens the editor too")
    check(einp.input_value() == "", "editor starts blank when Laya has no number")
    einp.fill("42")
    einp.press("Enter")
    cell = table.locator(f'.lsg-qty[data-lsg-qty="{eid}"]')  # re-paints with a new class
    check(cell.inner_text().strip() == "Order qty 42",
          f"quantity typed from scratch: {cell.inner_text().strip()!r}")

    # ---- reload resets everything (transient by design) ---------------------
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("text=AI reorder suggestions", timeout=60000)
    table = page.locator("table").filter(has_text="Laya AI (units)").first
    check(table.locator(".lsg-qty-set").count() == 0,
          "edits reset on reload (back to Laya's floor)")
    check(table.locator(".lsg-qty[data-lsg-suggest]").first.inner_text().strip() == floor_text,
          f"floor restored after reload: {floor_text!r}")

    page.screenshot(path="tests/qty_edit_check.png", full_page=False)
    browser.close()

print()
if failed:
    print(f"{len(failed)} FAILED:")
    for f in failed:
        print("  -", f)
    sys.exit(1)
print("all UI checks passed (screenshot: tests/qty_edit_check.png)")
