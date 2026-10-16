/* lakana
description: Every station is in the table of stations.
*/
SELECT t.station_id
FROM silver.station_temperatures AS t
LEFT JOIN bronze.stations AS s ON s.station_id = t.station_id
WHERE s.station_id IS NULL
