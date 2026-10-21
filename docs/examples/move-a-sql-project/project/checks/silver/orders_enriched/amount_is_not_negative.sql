/* lakana
description: An order never has a negative amount.
*/
SELECT order_id, amount_cents
FROM silver.orders_enriched
WHERE amount_cents < 0
