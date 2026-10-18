/* lakana
description: One row per customer order.
columns:
  id: The order's number.
  status: Where the order stands.
*/
SELECT id, status FROM bronze.orders
