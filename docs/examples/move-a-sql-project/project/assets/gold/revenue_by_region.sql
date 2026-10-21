/* lakana
description: The paid orders and their revenue in each region.
columns:
  region_name: The region.
  orders: How many paid orders the region has.
  revenue_cents: The sum of the paid orders in cents.
*/
SELECT
  region_name,
  COUNT(*) AS orders,
  CAST(SUM(amount_cents) AS BIGINT) AS revenue_cents
FROM silver.orders_enriched
WHERE status = 'paid'
GROUP BY region_name
