/* Reports page charts. Data is embedded by the server as JSON (see reports.html). */
(function () {
  "use strict";
  const S = window.StockPilot;
  const data = S && S.readJSON("chart-data");
  if (!data) return;

  S.renderChart("chart-daily", {
    kind: "line",
    labels: data.daily.labels,
    series: [
      { name: "Revenue", values: data.daily.revenue, color: S.COLORS.accent, format: "money" },
      { name: "Estimated gross profit", values: data.daily.profit, color: S.COLORS.success, dashed: true, format: "money" },
    ],
  });

  S.renderChart("chart-sellers", {
    kind: "bar-h",
    labels: data.best_sellers.labels,
    series: [{ name: "Units sold", values: data.best_sellers.values, format: "int" }],
  });
})();
