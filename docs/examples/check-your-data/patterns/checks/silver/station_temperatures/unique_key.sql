/* lakana
description: Each station appears once.
*/
SELECT station_id, COUNT(*) AS copies
FROM silver.station_temperatures
GROUP BY station_id
HAVING COUNT(*) > 1
