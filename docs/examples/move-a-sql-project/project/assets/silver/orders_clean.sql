/* lakana
description: The loaded orders with the status written one way.
materialize: view
columns:
  order_id: The order's number.
  region_code: The code of the region the order was placed in.
  status: paid, refunded or pending, in lower case.
  amount_cents: The order's amount in cents.
  order_date: The day of the order.
*/
SELECT
  order_id,
  region_code,
  LOWER(TRIM(status)) AS status,
  amount_cents,
  order_date
FROM bronze.orders
