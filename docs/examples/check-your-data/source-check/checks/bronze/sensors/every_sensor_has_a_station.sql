/* lakana
description: Every sensor says which station it belongs to.
*/
SELECT sensor_id
FROM bronze.sensors
WHERE station_id IS NULL
