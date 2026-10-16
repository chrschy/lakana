/* lakana
description: A battery level is between 0 and 100 percent.
on_failure: keep
*/
SELECT sensor_id, battery_pct
FROM bronze.sensor_log
WHERE battery_pct < 0 OR battery_pct > 100
