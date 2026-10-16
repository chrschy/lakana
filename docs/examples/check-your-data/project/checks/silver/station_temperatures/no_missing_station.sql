/* lakana
description: Every average belongs to a station.
*/
SELECT station_id, avg_temp_c
FROM silver.station_temperatures
WHERE station_id IS NULL
