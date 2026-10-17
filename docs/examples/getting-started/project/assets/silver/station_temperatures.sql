/* lakana
description: The lowest, highest and average temperature of each station.
columns:
  station_id: The station that took the readings.
  readings: How many readings the station took.
  min_temp_c: The lowest temperature in degrees Celsius.
  max_temp_c: The highest temperature in degrees Celsius.
  avg_temp_c: The average temperature in degrees Celsius.
*/
SELECT
  station_id,
  COUNT(*) AS readings,
  MIN(temp_c) AS min_temp_c,
  MAX(temp_c) AS max_temp_c,
  AVG(temp_c) AS avg_temp_c
FROM bronze.readings
GROUP BY station_id
