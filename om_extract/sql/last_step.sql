-- the masked cells of the **last** time step in the slab, for the map layer.
--   {{expr}}  the value expression over the slab alias `s` (e.g. s."CRW_SST", or
--             (s."analysed_sst" - 273.15) to convert Kelvin to Celsius)
--   {{slab}}  the registered griddap file (parquet) or view
--   {{mask}}  the mask table: latitude, longitude, weight (0,1]
-- one row per cell: the mask's own latitude/longitude (so the squares land on the dataset grid), the
-- area weight and the value. cells with no data on that step are kept with a NULL value, so the map
-- draws the hole instead of moving the neighbouring squares. the join is the same exact equality on
-- ROUND(x, 3) as stats_daily.sql (float32 axis vs float64 mask).
WITH joined AS (
  SELECT
    s."time"     AS time,
    m.latitude   AS latitude,
    m.longitude  AS longitude,
    m.weight     AS weight,
    {{expr}}     AS value
  FROM {{slab}} s
  JOIN {{mask}} m
    ON  ROUND(s.latitude::DOUBLE,  3) = ROUND(m.latitude::DOUBLE,  3)
    AND ROUND(s.longitude::DOUBLE, 3) = ROUND(m.longitude::DOUBLE, 3)
)
SELECT
  -- utc day; via epoch_ms because duckdb-wasm ships without icu, so TIMESTAMPTZ::DATE is unimplemented there
  make_timestamp(epoch_ms(time) * 1000)::DATE AS date,
  latitude,
  longitude,
  weight,
  value
FROM joined
WHERE time = (SELECT max(time) FROM joined)
ORDER BY latitude, longitude
