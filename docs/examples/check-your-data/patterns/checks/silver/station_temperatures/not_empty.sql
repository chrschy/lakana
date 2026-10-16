/* lakana
description: The table is not empty.
*/
SELECT 'silver.station_temperatures has no rows' AS problem
WHERE NOT EXISTS (SELECT 1 FROM silver.station_temperatures)
