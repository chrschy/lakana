/* lakana
description: How many sensors each station has.
columns:
  station_id: The station.
  sensors: The number of sensors at the station.
*/
SELECT station_id, COUNT(*) AS sensors
FROM bronze.sensors
GROUP BY station_id
