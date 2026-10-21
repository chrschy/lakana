/* lakana
description: The status is paid, refunded or pending.
*/
SELECT order_id, status
FROM silver.orders_enriched
WHERE status IS NULL OR status NOT IN ('paid', 'refunded', 'pending')
