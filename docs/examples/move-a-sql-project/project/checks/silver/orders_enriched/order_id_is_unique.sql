/* lakana
description: No order number appears twice.
*/
SELECT order_id, COUNT(*) AS copies
FROM silver.orders_enriched
GROUP BY order_id
HAVING COUNT(*) > 1
