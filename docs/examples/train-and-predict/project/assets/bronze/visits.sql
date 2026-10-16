/* lakana
description: How often four customers visited, and what each spent, written by hand.
columns:
  customer_id: The customer.
  visits: How many times the customer visited.
  spend: What the customer spent, in euros.
*/
SELECT customer_id, visits, spend
FROM (
  VALUES (1, 2, 12.0), (2, 4, 22.0), (3, 6, 32.0), (4, 8, 42.0)
) AS t(customer_id, visits, spend)
