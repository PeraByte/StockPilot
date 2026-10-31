/* Point of sale.
 *
 * The browser keeps the cart and shows running totals for convenience, but it
 * is NOT trusted. At checkout it sends only product ids and quantities; the
 * server looks up the real prices, checks stock and ownership again, and
 * returns the recorded total.
 *
 * All product text is written with textContent (never innerHTML), so a product
 * named "<img onerror=...>" is shown as plain text.
 */
(function () {
  "use strict";
  const S = window.StockPilot;
  const searchInput = document.getElementById("pos-search");
  if (!S || !searchInput) return;

  const $ = function (id) { return document.getElementById(id); };
  const resultsEl = $("pos-results");
  const statusEl = $("pos-status");
  const cartLinesEl = $("cart-lines");
  const cartEmptyEl = $("cart-empty");
  const unitsEl = $("cart-units");
  const subtotalEl = $("cart-subtotal");
  const totalEl = $("cart-total");
  const notesEl = $("sale-notes");
  const clearButton = $("clear-cart");
  const checkoutButton = $("checkout-button");
  const successEl = $("sale-success");

  const cart = new Map(); // product id -> { id, name, sku, price, available, quantity }
  let results = [];
  let busy = false;
  let searchController = null;
  let searchTimer = null;

  const el = S.el;

  function cartTotal() {
    let total = 0;
    cart.forEach(function (line) { total += line.price * line.quantity; });
    return total;
  }

  // ---------------------------------------------------------------- search
  async function loadProducts() {
    if (searchController) searchController.abort(); // only the newest search may update the screen
    searchController = new AbortController();
    const query = searchInput.value.trim();
    statusEl.textContent = "Loading products…";
    try {
      const data = await S.fetchJSON("/api/products/search?q=" + encodeURIComponent(query), {
        signal: searchController.signal,
      });
      results = data.products;
      // Keep the cart in step with fresh stock levels and prices.
      results.forEach(function (product) {
        const line = cart.get(product.id);
        if (!line) return;
        line.available = product.available;
        line.price = product.price;
        line.name = product.name;
        if (line.quantity > line.available) line.quantity = line.available;
      });
      renderResults();
      renderCart();
      if (results.length) {
        statusEl.textContent = results.length + (results.length === 1 ? " product" : " products") + " shown.";
      } else if (query) {
        statusEl.textContent = "No in-stock products match “" + query + "”.";
      } else {
        statusEl.textContent = "No products are in stock yet. Add a product or restock one to start selling.";
      }
    } catch (error) {
      if (error.name === "AbortError") return;
      statusEl.textContent = "Products could not be loaded.";
      S.toast(error.message, "error");
    }
  }

  function renderResults(focusId) {
    const tiles = results.map(function (product) {
      const inCart = cart.has(product.id) ? cart.get(product.id).quantity : 0;
      const full = inCart >= product.available;
      const tile = el("button", "product-tile");
      tile.type = "button";
      tile.dataset.id = product.id;
      if (full) tile.setAttribute("aria-disabled", "true");
      const hidden = el("span", "sr-only", "Add to sale: ");
      const name = el("span", "tile-name");
      name.append(hidden, document.createTextNode(product.name));
      tile.append(
        name,
        el("span", "tile-meta", "SKU " + product.sku + " · " + product.category),
        el("span", "tile-meta", product.available + " in stock" + (inCart ? " · " + inCart + " in cart" : "")),
        el("span", "tile-price", S.formatMoney(product.price))
      );
      return tile;
    });
    resultsEl.replaceChildren.apply(resultsEl, tiles);
    if (focusId) {
      const again = resultsEl.querySelector('[data-id="' + focusId + '"]');
      if (again) again.focus();
    }
  }

  // ------------------------------------------------------------------ cart
  function addToCart(product) {
    successEl.hidden = true;
    const existing = cart.get(product.id);
    if (existing) {
      if (existing.quantity >= existing.available) {
        S.toast("All " + existing.available + " in stock of “" + existing.name + "” are already in the cart.", "warning");
        return;
      }
      existing.quantity += 1;
    } else {
      cart.set(product.id, {
        id: product.id, name: product.name, sku: product.sku,
        price: product.price, available: product.available, quantity: 1,
      });
    }
    renderCart();
    renderResults(product.id);
  }

  function setQuantity(id, requested) {
    const line = cart.get(id);
    if (!line) return;
    let quantity = Math.floor(Number(requested));
    if (!Number.isFinite(quantity) || quantity < 1) quantity = 1;
    if (quantity > line.available) {
      quantity = line.available;
      S.toast("Only " + line.available + " of “" + line.name + "” in stock.", "warning");
    }
    line.quantity = quantity;
  }

  function buildLine(line) {
    const item = el("li", "cart-line");
    item.dataset.id = line.id;

    const info = el("div");
    info.append(
      el("div", "cart-line-name", line.name),
      el("div", "cell-sub", "SKU " + line.sku + " · " + S.formatMoney(line.price) + " each · " + line.available + " available")
    );
    const total = el("div", "cart-line-total", S.formatMoney(line.price * line.quantity));

    const controls = el("div", "cart-line-controls");
    const stepper = el("div", "stepper");

    const decrease = el("button", "icon-btn");
    decrease.type = "button";
    decrease.dataset.action = "decrease";
    decrease.dataset.id = line.id;
    decrease.setAttribute("aria-label", "Decrease quantity of " + line.name);
    decrease.appendChild(S.icon("minus", 16));

    const input = el("input", "control");
    input.type = "number";
    input.min = "1";
    input.max = String(line.available);
    input.step = "1";
    input.value = String(line.quantity);
    input.dataset.qty = line.id;
    input.setAttribute("aria-label", "Quantity of " + line.name);
    input.setAttribute("inputmode", "numeric");

    const increase = el("button", "icon-btn");
    increase.type = "button";
    increase.dataset.action = "increase";
    increase.dataset.id = line.id;
    increase.setAttribute("aria-label", "Increase quantity of " + line.name);
    increase.appendChild(S.icon("plus", 16));
    stepper.append(decrease, input, increase);

    const remove = el("button", "btn btn-sm btn-ghost", "Remove");
    remove.type = "button";
    remove.dataset.action = "remove";
    remove.dataset.id = line.id;
    remove.setAttribute("aria-label", "Remove " + line.name + " from the sale");

    controls.append(stepper, remove);
    item.append(info, total, controls);
    return item;
  }

  function renderCart(focusSelector) {
    const lines = Array.from(cart.values());
    cartLinesEl.replaceChildren.apply(cartLinesEl, lines.map(buildLine));
    const hasItems = lines.length > 0;
    cartLinesEl.hidden = !hasItems;
    cartEmptyEl.hidden = hasItems;

    let units = 0;
    lines.forEach(function (line) { units += line.quantity; });
    unitsEl.textContent = String(units);
    subtotalEl.textContent = S.formatMoney(cartTotal());
    totalEl.textContent = S.formatMoney(cartTotal());
    checkoutButton.disabled = !hasItems || busy;
    clearButton.disabled = !hasItems || busy;

    if (focusSelector) {
      const target = cartLinesEl.querySelector(focusSelector);
      if (target) target.focus();
    }
  }

  // Cart buttons and quantity boxes use event delegation (one listener for all lines).
  cartLinesEl.addEventListener("click", function (event) {
    const button = event.target.closest("[data-action]");
    if (!button || busy) return;
    const id = Number(button.dataset.id);
    const line = cart.get(id);
    if (!line) return;
    const action = button.dataset.action;
    if (action === "remove" || (action === "decrease" && line.quantity <= 1)) {
      cart.delete(id);
      renderCart();
      searchInput.focus();
    } else if (action === "decrease") {
      line.quantity -= 1;
      renderCart('[data-action="decrease"][data-id="' + id + '"]');
    } else if (action === "increase") {
      if (line.quantity >= line.available) {
        S.toast("Only " + line.available + " of “" + line.name + "” in stock.", "warning");
        return;
      }
      line.quantity += 1;
      renderCart('[data-action="increase"][data-id="' + id + '"]');
    }
    renderResults();
  });

  cartLinesEl.addEventListener("change", function (event) {
    const input = event.target.closest("[data-qty]");
    if (!input || busy) return;
    const id = Number(input.dataset.qty);
    setQuantity(id, input.value);
    renderCart('[data-qty="' + id + '"]');
    renderResults();
  });

  resultsEl.addEventListener("click", function (event) {
    const tile = event.target.closest(".product-tile");
    if (!tile || busy) return;
    const product = results.find(function (item) { return item.id === Number(tile.dataset.id); });
    if (!product) return;
    if (tile.getAttribute("aria-disabled") === "true") {
      S.toast("All " + product.available + " in stock of “" + product.name + "” are already in the cart.", "warning");
      return;
    }
    addToCart(product);
  });

  clearButton.addEventListener("click", function () {
    if (busy) return;
    cart.clear();
    renderCart();
    renderResults();
    searchInput.focus();
  });

  // -------------------------------------------------------------- checkout
  function setBusy(value) {
    busy = value;
    checkoutButton.classList.toggle("is-loading", value);
    checkoutButton.setAttribute("aria-busy", String(value));
    checkoutButton.disabled = value || cart.size === 0;
    clearButton.disabled = value || cart.size === 0;
  }

  function showSuccess(data, expectedTotal) {
    const panel = el("div", "success-panel");
    const badge = el("span", "kpi-icon");
    badge.appendChild(S.icon("check", 24));
    const text = el("div");
    text.appendChild(el("h2", "", "Sale " + data.receipt_number + " recorded"));
    text.appendChild(el("p", "", "Total " + S.formatMoney(data.total) + ". Stock has been updated."));
    if (data.total !== expectedTotal) {
      text.appendChild(el("p", "muted small",
        "A price changed since you added the items. The recorded total is the one above."));
    }
    const actions = el("div", "success-actions");
    const receiptLink = el("a", "btn btn-primary", "View receipt");
    if (typeof data.receipt_url === "string" && data.receipt_url.charAt(0) === "/") receiptLink.href = data.receipt_url;
    const another = el("button", "btn", "Start another sale");
    another.type = "button";
    another.addEventListener("click", function () { successEl.hidden = true; searchInput.focus(); });
    actions.append(receiptLink, another);
    text.appendChild(actions);
    panel.append(badge, text);
    successEl.replaceChildren(panel);
    successEl.hidden = false;
    successEl.focus();
  }

  async function checkout() {
    if (busy || cart.size === 0) return;
    setBusy(true);
    const expectedTotal = cartTotal();
    const items = Array.from(cart.values()).map(function (line) {
      return { product_id: line.id, quantity: line.quantity };
    });
    try {
      const data = await S.fetchJSON("/api/checkout", {
        method: "POST",
        body: { items: items, notes: notesEl.value.trim() },
      });
      // Only now, after the server confirmed, do we clear the cart.
      cart.clear();
      notesEl.value = "";
      showSuccess(data, expectedTotal);
      S.toast("Sale " + data.receipt_number + " recorded.", "success");
    } catch (error) {
      S.toast(error.message, "error");
      if (error.data && Array.isArray(error.data.items)) {
        error.data.items.forEach(function (info) {
          const line = cart.get(info.product_id);
          if (!line) return;
          if (info.available <= 0) {
            cart.delete(info.product_id);
          } else {
            line.available = info.available;
            line.quantity = Math.min(line.quantity, info.available);
          }
        });
      }
    } finally {
      setBusy(false);
    }
    renderCart();
    await loadProducts(); // stock changed, so refresh the tiles and the cart limits
  }

  checkoutButton.addEventListener("click", checkout);

  searchInput.addEventListener("input", function () {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(loadProducts, 250); // wait for a pause in typing
  });

  // Warn before leaving with an unfinished sale.
  window.addEventListener("beforeunload", function (event) {
    if (cart.size > 0) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  renderCart();
  loadProducts();
})();
