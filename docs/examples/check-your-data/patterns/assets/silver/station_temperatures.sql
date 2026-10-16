/* lakana
description: The average temperature of each station, with the station's name.
columns:
  station_id: The station that took the readings.
  station_name: The station's name, empty for a station that is not in bronze.stations.
  avg_temp_c: The average temperature in degrees Celsius.
*/
SELECT r.station_id, s.name AS station_name, AVG(r.temp_c) AS avg_temp_c
FROM bronze.readings AS r
LEFT JOIN bronze.stations AS s ON s.station_id = r.station_id
GROUP BY r.station_id, s.name
