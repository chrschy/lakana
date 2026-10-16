/* lakana
description: The sensors whose battery is below 40 percent.
columns:
  sensor_id: The sensor.
  battery_pct: The battery level in percent.
*/
SELECT sensor_id, battery_pct
FROM bronze.sensor_log
WHERE battery_pct < 40
