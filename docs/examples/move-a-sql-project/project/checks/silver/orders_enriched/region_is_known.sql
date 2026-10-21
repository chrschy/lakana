/* lakana
description: Every order belongs to a region in the regions table.
*/
SELECT order_id
FROM silver.orders_enriched
WHERE region_name IS NULL
