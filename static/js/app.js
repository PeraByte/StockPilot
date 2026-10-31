/* StockPilot - shared browser code.
 *
 * Loaded on every page. It provides:
 *   - sidebar collapse, mobile drawer and profile menu
 *   - toast notifications (flash messages become toasts)
 *   - an accessible confirmation dialog for forms marked data-confirm
 *   - double-submit protection for forms
 *   - StockPilot.fetchJSON()   : fetch wrapper that checks the HTTP status
 *   - StockPilot.formatMoney() : integer minor units -> "PKR 1,250.50"
 *   - StockPilot.renderChart() : Chart.js chart, or a plain HTML fallback
 *
 * No inline scripts are used anywhere (the Content-Security-Policy forbids
 * them), so every page loads this file and, where needed, one page script.
 */
(function () {
  "use strict";

  const doc = document;
  const body = doc.body;
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const SVG_NS = "http://www.w3.org/2000/svg";

  // localStorage can throw (private windows, blocked storage), so wrap it.
  const store = {
    get(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } },
    set(key, value) { try { window.localStorage.setItem(key, value); } catch (e) { /* ignore */ } },
  };

  // ------------------------------------------------------------------
  // Small helpers
  // ------------------------------------------------------------------
  const ICON_PATHS = {
    success: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l3 3 5-6"/>',
    error: '<path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
    warning: '<path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
    close: '<path d="M18 6L6 18M6 6l12 12"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    minus: '<path d="M5 12h14"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    alert: '<path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
  };

  // Builds an <svg> icon from the constant path data above (never from user text).
  function icon(name, size) {
    const svg = doc.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("width", size || 20);
    svg.setAttribute("height", size || 20);
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.8");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    svg.innerHTML = ICON_PATHS[name] || "";
    return svg;
  }

  // Creates an element. Text is always set with textContent, so user data can never inject HTML.
  function el(tag, className, text) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  const currencyLabel = body.dataset.currency || "";

  // Money arrives as whole minor units (paisa). Integer maths avoids rounding errors.
  function formatMoney(minorUnits, withLabel) {
    const value = Math.round(Number(minorUnits) || 0);
    const sign = value < 0 ? "-" : "";
    const absolute = Math.abs(value);
    const whole = Math.floor(absolute / 100);
    const fraction = String(absolute % 100).padStart(2, "0");
    const text = whole.toLocaleString("en-US") + "." + fraction;
    return withLabel === false || !currencyLabel ? sign + text : sign + currencyLabel + " " + text;
  }

  function csrfToken() {
    const meta = doc.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  }

  function readJSON(id) {
    const node = doc.getElementById(id);
    if (!node) return null;
    try { return JSON.parse(node.textContent); } catch (e) { return null; }
  }

  // ------------------------------------------------------------------
  // fetchJSON: checks the status, parses errors safely, adds the CSRF header
  // ------------------------------------------------------------------
  async function fetchJSON(url, options) {
    const settings = Object.assign({ method: "GET" }, options || {});
    settings.headers = Object.assign({ Accept: "application/json" }, settings.headers || {});
    settings.credentials = "same-origin";
    if (settings.method.toUpperCase() !== "GET") {
      settings.headers["X-CSRF-Token"] = csrfToken();
    }
    if (settings.body && typeof settings.body === "object" && !(settings.body instanceof FormData)) {
      settings.headers["Content-Type"] = "application/json";
      settings.body = JSON.stringify(settings.body);
    }

    let response;
    try {
      response = await fetch(url, settings);
    } catch (networkError) {
      if (networkError && networkError.name === "AbortError") throw networkError;
      const error = new Error("Could not reach the server. Check your connection and try again.");
      error.network = true;
      throw error;
    }

    let data = null;
    try { data = await response.json(); } catch (parseError) { data = null; }

    if (!response.ok) {
      let message = data && data.error;
      if (!message) {
        message = response.status === 401
          ? "Your session has ended. Please log in again."
          : "Something went wrong (error " + response.status + "). Please try again.";
      }
      if (response.status === 401) {
        window.setTimeout(function () { window.location.assign("/login"); }, 1800);
      }
      const error = new Error(message);
      error.status = response.status;
      error.data = data;
      throw error;
    }
    return data;
  }

  // ------------------------------------------------------------------
  // Toasts
  // ------------------------------------------------------------------
  const toastRegion = doc.getElementById("toast-region");
  const TOAST_TIMEOUT = { success: 6000, info: 6000, warning: 9000, error: 0 }; // 0 = stays until closed

  function toast(message, type) {
    if (!toastRegion) return;
    const kind = ["success", "error", "warning", "info"].indexOf(type) !== -1 ? type : "info";
    const item = el("div", "toast toast-" + kind);
    item.setAttribute("role", kind === "error" ? "alert" : "status");
    item.appendChild(icon(kind, 22));
    item.appendChild(el("p", "toast-message", message));

    const close = el("button", "toast-close");
    close.type = "button";
    close.setAttribute("aria-label", "Dismiss notification");
    close.appendChild(icon("close", 16));
    item.appendChild(close);

    function dismiss() {
      if (!item.parentNode) return;
      if (reduceMotion) { item.remove(); return; }
      item.classList.add("is-leaving");
      window.setTimeout(function () { item.remove(); }, 220);
    }
    close.addEventListener("click", dismiss);
    toastRegion.appendChild(item);
    while (toastRegion.children.length > 4) toastRegion.firstElementChild.remove();
    if (TOAST_TIMEOUT[kind]) window.setTimeout(dismiss, TOAST_TIMEOUT[kind]);
  }

  // Server-side flash messages become toasts (without JavaScript they stay visible as banners).
  doc.querySelectorAll("[data-flash-source] .flash").forEach(function (flash) {
    toast(flash.textContent.trim(), flash.dataset.category);
  });
  doc.querySelectorAll("[data-flash-source]").forEach(function (list) { list.remove(); });

  // ------------------------------------------------------------------
  // Confirmation dialog (native <dialog>: focus trap and Escape come for free)
  // ------------------------------------------------------------------
  const dialog = doc.getElementById("confirm-dialog");

  function confirmDialog(options) {
    if (!dialog || typeof dialog.showModal !== "function") {
      return Promise.resolve(window.confirm(options.message));
    }
    doc.getElementById("confirm-title").textContent = options.title || "Are you sure?";
    doc.getElementById("confirm-message").textContent = options.message;
    doc.getElementById("confirm-accept").textContent = options.confirmLabel || "Confirm";
    return new Promise(function (resolve) {
      function onClose() {
        dialog.removeEventListener("close", onClose);
        resolve(dialog.returnValue === "confirm");
      }
      dialog.addEventListener("close", onClose);
      dialog.returnValue = "";
      dialog.showModal();
      doc.getElementById("confirm-cancel").focus(); // safest default for a destructive action
    });
  }

  // Forms with data-confirm ask first. The capture phase lets us stop the submit before anything else sees it.
  doc.addEventListener("submit", function (event) {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.dataset.confirm) return;
    if (form.dataset.confirmed === "1") { return; } // second pass, after the user agreed
    event.preventDefault();
    confirmDialog({
      title: form.dataset.confirmTitle,
      message: form.dataset.confirm,
      confirmLabel: form.dataset.confirmLabel,
    }).then(function (agreed) {
      if (!agreed) return;
      form.dataset.confirmed = "1";
      if (typeof form.requestSubmit === "function") form.requestSubmit(); else form.submit();
    });
  }, true);

  // ------------------------------------------------------------------
  // Prevent double submission of forms that change data
  // ------------------------------------------------------------------
  doc.addEventListener("submit", function (event) {
    const form = event.target;
    if (event.defaultPrevented || !(form instanceof HTMLFormElement)) return;
    const method = (form.getAttribute("method") || "get").toLowerCase();
    if (method !== "post") return; // searches and filters (GET) and the dialog form are harmless to repeat
    if (form.dataset.submitting === "1") { event.preventDefault(); return; }
    form.dataset.submitting = "1";
    // Disable the buttons after the browser has read the form data.
    window.setTimeout(function () {
      form.querySelectorAll('[type="submit"]').forEach(function (button) {
        button.disabled = true;
        button.classList.add("is-loading");
      });
    }, 0);
  });

  // Coming back with the Back button must not leave buttons disabled.
  window.addEventListener("pageshow", function () {
    doc.querySelectorAll("form[data-submitting], form[data-confirmed]").forEach(function (form) {
      delete form.dataset.submitting;
      delete form.dataset.confirmed;
      form.querySelectorAll('[type="submit"]').forEach(function (button) {
        button.disabled = false;
        button.classList.remove("is-loading");
      });
    });
  });

  // ------------------------------------------------------------------
  // Small generic behaviours
  // ------------------------------------------------------------------
  doc.addEventListener("change", function (event) {
    const target = event.target;
    if (target && target.matches && target.matches("[data-autosubmit]") && target.form) {
      if (typeof target.form.requestSubmit === "function") target.form.requestSubmit(); else target.form.submit();
    }
  });

  doc.addEventListener("click", function (event) {
    if (event.target.closest && event.target.closest("[data-print]")) window.print();
  });

  // ------------------------------------------------------------------
  // Sidebar: collapse on desktop, drawer on mobile
  // ------------------------------------------------------------------
  const collapseButton = doc.getElementById("sidebar-collapse");
  if (collapseButton) {
    const applyCollapsed = function (collapsed) {
      body.classList.toggle("sidebar-collapsed", collapsed);
      collapseButton.setAttribute("aria-pressed", String(collapsed));
      collapseButton.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
    };
    applyCollapsed(store.get("stockpilot.sidebar") === "collapsed");
    collapseButton.addEventListener("click", function () {
      const collapsed = !body.classList.contains("sidebar-collapsed");
      applyCollapsed(collapsed);
      store.set("stockpilot.sidebar", collapsed ? "collapsed" : "expanded");
    });
  }

  const menuToggle = doc.getElementById("menu-toggle");
  const sidebar = doc.getElementById("sidebar");
  const backdrop = doc.getElementById("sidebar-backdrop");
  if (menuToggle && sidebar && backdrop) {
    const setDrawer = function (open, returnFocus) {
      body.classList.toggle("nav-open", open);
      backdrop.hidden = !open;
      menuToggle.setAttribute("aria-expanded", String(open));
      menuToggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
      if (open) {
        const first = sidebar.querySelector(".nav-link");
        if (first) first.focus();
      } else if (returnFocus) {
        menuToggle.focus();
      }
    };
    menuToggle.addEventListener("click", function () { setDrawer(!body.classList.contains("nav-open"), true); });
    backdrop.addEventListener("click", function () { setDrawer(false, true); });
    sidebar.addEventListener("click", function (event) {
      if (event.target.closest("a") && body.classList.contains("nav-open")) setDrawer(false, false);
    });
    window.matchMedia("(min-width: 901px)").addEventListener("change", function (query) {
      if (query.matches && body.classList.contains("nav-open")) setDrawer(false, false);
    });
    doc.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && body.classList.contains("nav-open")) setDrawer(false, true);
    });
  }

  // ------------------------------------------------------------------
  // Profile menu
  // ------------------------------------------------------------------
  const profileButton = doc.getElementById("profile-button");
  const profileMenu = doc.getElementById("profile-menu");
  if (profileButton && profileMenu) {
    const setMenu = function (open, returnFocus) {
      profileMenu.hidden = !open;
      profileButton.setAttribute("aria-expanded", String(open));
      if (!open && returnFocus) profileButton.focus();
    };
    profileButton.addEventListener("click", function () { setMenu(profileMenu.hidden, false); });
    doc.addEventListener("click", function (event) {
      if (!profileMenu.hidden && !event.target.closest("#profile")) setMenu(false, false);
    });
    doc.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !profileMenu.hidden) setMenu(false, true);
    });
    profileMenu.addEventListener("focusout", function (event) {
      if (!profileMenu.hidden && event.relatedTarget && !event.relatedTarget.closest("#profile")) setMenu(false, false);
    });
  }

  // ------------------------------------------------------------------
  // Charts: Chart.js when available, otherwise a readable HTML fallback
  // ------------------------------------------------------------------
  const COLORS = { accent: "#4560e6", success: "#248a62", text: "#5b6678", grid: "#d3d9e2", surface: "#e9edf2" };

  function formatValue(value, format) {
    return format === "money" ? formatMoney(value) : Number(value).toLocaleString("en-US");
  }

  function truncate(text, max) {
    return text.length > max ? text.slice(0, max - 1) + "…" : text;
  }

  // spec = { kind: "line" | "bar-h", labels: [...], series: [{ name, values, color, dashed, format }] }
  function buildChartConfig(spec) {
    const horizontal = spec.kind === "bar-h";
    const isLine = spec.kind === "line";
    const dense = spec.labels.length > 45; // hide point markers on long ranges
    const isMoney = spec.series[0].format === "money";
    const valueAxis = horizontal ? "x" : "y";
    const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

    const datasets = spec.series.map(function (series) {
      const color = series.color || COLORS.accent;
      if (isLine) {
        return {
          label: series.name, data: series.values, borderColor: color, borderWidth: 2,
          borderDash: series.dashed ? [7, 5] : [], backgroundColor: color, tension: 0,
          pointRadius: dense ? 0 : 3.5, pointHoverRadius: 6, pointHitRadius: 12,
          pointBackgroundColor: COLORS.surface, pointBorderColor: color, pointBorderWidth: 2,
        };
      }
      return {
        label: series.name, data: series.values, backgroundColor: color,
        borderRadius: 4, borderSkipped: false, maxBarThickness: 20,
      };
    });

    const valueScale = {
      beginAtZero: true,
      grid: { color: COLORS.grid },
      border: { display: false },
      ticks: {
        precision: 0,
        callback: function (value) { return isMoney ? compact.format(value / 100) : compact.format(value); },
      },
    };
    const categoryScale = {
      grid: { display: false },
      border: { color: COLORS.grid },
      ticks: { autoSkip: true, maxTicksLimit: 10, maxRotation: 0 },
    };
    if (horizontal) {
      categoryScale.ticks = {
        autoSkip: false,
        callback: function (value) { return truncate(String(this.getLabelForValue(value)), 22); },
      };
    }

    return {
      type: isLine ? "line" : "bar",
      data: { labels: spec.labels, datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        indexAxis: horizontal ? "y" : "x",
        interaction: isLine ? { mode: "index", intersect: false } : { mode: "nearest", intersect: true },
        plugins: {
          legend: { display: false }, // the page shows its own legend above the chart
          tooltip: {
            callbacks: {
              label: function (context) {
                const series = spec.series[context.datasetIndex];
                return series.name + ": " + formatValue(context.parsed[valueAxis], series.format);
              },
            },
          },
        },
        scales: horizontal ? { x: valueScale, y: categoryScale } : { x: categoryScale, y: valueScale },
      },
    };
  }

  // Shown when the Chart.js CDN script could not be loaded (offline, blocked, ...).
  function showChartFallback(box, spec) {
    const wrap = el("div", "chart-fallback");
    if (box.dataset.fallback === "table") {
      // A table with the same figures sits right below this chart, so do not repeat it as bars.
      wrap.appendChild(el("p", "fb-note", "The interactive chart could not be loaded. The same figures are in the table below."));
      box.classList.add("is-fallback");
      box.replaceChildren(wrap);
      return;
    }
    wrap.appendChild(el("p", "fb-note", "The interactive chart could not be loaded, so the numbers are shown as bars instead."));
    let max = 1;
    spec.series.forEach(function (series) {
      series.values.forEach(function (value) { max = Math.max(max, Number(value) || 0); });
    });
    spec.labels.forEach(function (label, index) {
      const row = el("div", "fb-row");
      row.appendChild(el("div", "fb-label", label));
      const bars = el("div", "fb-bars");
      spec.series.forEach(function (series, seriesIndex) {
        const value = Number(series.values[index]) || 0;
        const meter = doc.createElement("progress");
        meter.className = "meter " + (seriesIndex === 0 ? "meter-neutral" : "meter-ok");
        meter.max = max;
        meter.value = Math.max(0, value);
        meter.setAttribute("aria-label", series.name + " for " + label + ": " + formatValue(value, series.format));
        bars.appendChild(meter);
        bars.appendChild(el("div", "fb-value", series.name + ": " + formatValue(value, series.format)));
      });
      row.appendChild(bars);
      wrap.appendChild(row);
    });
    box.classList.add("is-fallback");
    box.replaceChildren(wrap);
  }

  function renderChart(boxId, spec) {
    const box = doc.getElementById(boxId);
    if (!box || !spec || !spec.labels || !spec.labels.length) return;
    const canvas = box.querySelector("canvas");
    if (!window.Chart || !canvas) { showChartFallback(box, spec); return; }
    try {
      window.Chart.defaults.font.family = window.getComputedStyle(body).fontFamily;
      window.Chart.defaults.color = COLORS.text;
      if (reduceMotion) window.Chart.defaults.animation = false;
      new window.Chart(canvas, buildChartConfig(spec));
    } catch (error) {
      console.error("Chart could not be drawn:", error);
      showChartFallback(box, spec);
    }
  }

  window.StockPilot = {
    COLORS: COLORS,
    csrfToken: csrfToken,
    el: el,
    icon: icon,
    readJSON: readJSON,
    fetchJSON: fetchJSON,
    formatMoney: formatMoney,
    toast: toast,
    confirm: confirmDialog,
    renderChart: renderChart,
  };
})();
