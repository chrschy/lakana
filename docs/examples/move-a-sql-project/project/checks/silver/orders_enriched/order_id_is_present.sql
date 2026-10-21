/* lakana
description: Every order has a number.
*/
SELECT order_date, amount_cents
FROM silver.orders_enriched
WHERE order_id IS NULL
