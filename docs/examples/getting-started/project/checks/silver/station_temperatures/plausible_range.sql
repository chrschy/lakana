/* lakana
description: An average below -90 or above 60 degrees is a sensor fault.
*/
SELECT station_id, avg_temp_c
FROM silver.station_temperatures
WHERE avg_temp_c < -90 OR avg_temp_c > 60
