/* Dashboard charts. The numbers come from the server as JSON inside the page
 * (<script type="application/json" id="chart-data">), so nothing is invented here.
 * Money values are whole minor units (paisa); app.js formats them.
 */
(function () {
  "use strict";
  const S = window.StockPilot;
  const data = S && S.readJSON("chart-data");
  if (!data) return;

  S.renderChart("chart-week", {
    kind: "line",
    labels: data.week.labels,
    series: [
      { name: "Revenue", values: data.week.revenue, color: S.COLORS.accent, format: "money" },
      { name: "Estimated gross profit", values: data.week.profit, color: S.COLORS.success, dashed: true, format: "money" },
    ],
  });

  S.renderChart("chart-categories", {
    kind: "bar-h",
    labels: data.categories.labels,
    series: [{ name: "Units in stock", values: data.categories.values, format: "int" }],
  });

  S.renderChart("chart-sellers", {
    kind: "bar-h",
    labels: data.best_sellers.labels,
    series: [{ name: "Units sold", values: data.best_sellers.values, format: "int" }],
  });
})();
