/* lakana
description: The stations from the warmest to the coldest.
columns:
  station_id: The station.
  avg_temp_c: The station's average temperature in degrees Celsius.
  warmth_rank: 1 for the warmest station, 2 for the next, and so on.
*/
SELECT
  station_id,
  avg_temp_c,
  RANK() OVER (ORDER BY avg_temp_c DESC) AS warmth_rank
FROM silver.station_temperatures
