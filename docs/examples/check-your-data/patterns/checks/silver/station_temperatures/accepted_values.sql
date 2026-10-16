/* lakana
description: Only the three known stations report.
*/
SELECT station_id
FROM silver.station_temperatures
WHERE station_id NOT IN ('A1', 'B2', 'C3')
