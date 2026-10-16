/* lakana
description: The battery level of three sensors, written by hand.
columns:
  sensor_id: The sensor.
  battery_pct: The battery level in percent.
*/
SELECT 'S1' AS sensor_id, 80 AS battery_pct
UNION ALL SELECT 'S2', 35
UNION ALL SELECT 'S3', 140
