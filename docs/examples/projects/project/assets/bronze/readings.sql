/* lakana
description: A few hand-written temperature readings.
*/
SELECT 'A1' AS station_id, TIMESTAMP '2026-01-01 00:00:00' AS ts, 1.0 AS temp_c
UNION ALL SELECT 'A1', TIMESTAMP '2026-01-05 00:00:00', 3.0
UNION ALL SELECT 'B2', TIMESTAMP '2026-01-06 00:00:00', 2.0
UNION ALL SELECT 'B2', TIMESTAMP '2026-01-07 00:00:00', 6.0
