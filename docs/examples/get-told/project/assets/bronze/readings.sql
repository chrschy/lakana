/* lakana
description: A reading that cannot be parsed, so that a run of the job fails.
columns:
  temp_c: The temperature in degrees Celsius.
*/
SELECT CAST('n/a' AS DOUBLE) AS temp_c
