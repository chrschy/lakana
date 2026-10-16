/* lakana
description: Every station has an average temperature.
*/
SELECT station_id
FROM silver.station_temperatures
WHERE avg_temp_c IS NULL
