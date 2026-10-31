/* Product pages:
 *   1. New / edit form: live "profit per unit" preview.
 *   2. Product detail: the stock form shows only the fields that apply to the chosen action.
 * Both are conveniences. The server validates everything again.
 */
(function () {
  "use strict";
  const S = window.StockPilot;
  if (!S) return;

  // ---- 1. Profit preview -------------------------------------------------
  const productForm = document.querySelector("[data-product-form]");
  if (productForm) {
    const cost = productForm.querySelector("#cost_price");
    const price = productForm.querySelector("#selling_price");
    const output = productForm.querySelector("#margin-preview");

    // "1,250.5" -> 125050 (minor units), or null when the text is not a valid amount.
    function toMinorUnits(text) {
      const cleaned = text.trim().replace(/,/g, "");
      if (!/^[0-9]+(\.[0-9]{1,2})?$/.test(cleaned)) return null;
      const parts = cleaned.split(".");
      return parseInt(parts[0], 10) * 100 + parseInt(((parts[1] || "") + "00").slice(0, 2), 10);
    }

    function updatePreview() {
      const costMinor = toMinorUnits(cost.value);
      const priceMinor = toMinorUnits(price.value);
      output.classList.remove("is-negative");
      if (costMinor === null || priceMinor === null) {
        output.textContent = "Enter both prices to see your profit per unit.";
        return;
      }
      const profit = priceMinor - costMinor;
      let message = "Estimated profit per unit: " + S.formatMoney(profit);
      if (priceMinor > 0) message += " (" + ((profit * 100) / priceMinor).toFixed(1) + "% of the selling price)";
      if (profit < 0) {
        output.classList.add("is-negative");
        output.replaceChildren(S.icon("alert", 18), document.createTextNode(message + ". You would be selling below cost."));
      } else {
        output.textContent = message;
      }
    }

    cost.addEventListener("input", updatePreview);
    price.addEventListener("input", updatePreview);
    updatePreview();
  }

  // ---- 2. Stock form -----------------------------------------------------
  const stockForm = document.querySelector("[data-stock-form]");
  if (stockForm) {
    const radios = stockForm.querySelectorAll('input[name="action"]');
    const restockFields = stockForm.querySelector("#restock-fields");
    const notes = stockForm.querySelector("#stock-notes");
    const notesOptional = stockForm.querySelector("#notes-optional");

    function syncStockForm() {
      const chosen = stockForm.querySelector('input[name="action"]:checked');
      const isRestock = !chosen || chosen.value === "restock";
      restockFields.hidden = !isRestock;
      notes.required = !isRestock; // adjustments must explain themselves
      notesOptional.textContent = isRestock ? "(optional for a restock)" : "(required)";
    }

    radios.forEach(function (radio) { radio.addEventListener("change", syncStockForm); });
    syncStockForm();
  }
})();
