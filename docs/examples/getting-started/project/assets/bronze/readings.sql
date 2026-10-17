/* lakana
description: Eighteen generated readings from three weather stations, six days each.
columns:
  station_id: The station that took the reading.
  day: The day of the reading.
  temp_c: The temperature in degrees Celsius.
*/
SELECT
  stations.station_id,
  DATE '2026-01-01' + CAST(days.n AS INTEGER) AS day,
  CAST(stations.base_c + (days.n % 3) - 1 AS DOUBLE) AS temp_c
FROM (VALUES ('A1', 4.0), ('B2', 1.0), ('C3', 9.0)) AS stations(station_id, base_c)
CROSS JOIN (VALUES (0), (1), (2), (3), (4), (5)) AS days(n)
