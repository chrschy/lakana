"""The sample project `lakana init` writes: generated data, three SQL assets and one check.

The files are literal text, kept equal to the ones the getting-started tutorial shows
(`docs/examples/getting-started/project`; a test compares them). Nothing is downloaded and the
rows are computed by the first query, so the tables are the same on every machine and every run.
"""

from __future__ import annotations

_READINGS = """\
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
"""

_STATION_TEMPERATURES = """\
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
"""

_STATION_RANKING = """\
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
"""

_PLAUSIBLE_RANGE = """\
/* lakana
description: An average below -90 or above 60 degrees is a sensor fault.
*/
SELECT station_id, avg_temp_c
FROM silver.station_temperatures
WHERE avg_temp_c < -90 OR avg_temp_c > 60
"""

SAMPLE: dict[str, str] = {
    "assets/bronze/readings.sql": _READINGS,
    "assets/silver/station_temperatures.sql": _STATION_TEMPERATURES,
    "assets/gold/station_ranking.sql": _STATION_RANKING,
    "checks/silver/station_temperatures/plausible_range.sql": _PLAUSIBLE_RANGE,
}
"""Relative path to text, written verbatim."""

SAMPLE_QUERY = "SELECT * FROM gold.station_ranking ORDER BY warmth_rank"
"""The query the next steps suggest once the sample is built."""
