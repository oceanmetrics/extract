-- daily key statistics for one place from an ERDDAP griddap slab.
--   {{expr}}  the value expression over the slab alias `s` (e.g. s."CRW_SST", or
--             (s."analysed_sst" - 273.15) to convert Kelvin to Celsius)
--   {{slab}}  the registered griddap file (parquet) or view
--   {{mask}}  the mask table: latitude, longitude, weight (0,1]
-- the join is an exact equality on ROUND(x, 3): the ERDDAP axis is float32 in the parquet and float64
-- in the JS mask, so 21.375 arrives as 21.374999... on one side; rounding to the grid's 3 decimals
-- (0.05 spacing) makes the two match without a tolerance join.
SELECT
  -- utc day; via epoch_ms because duckdb-wasm ships without icu, so TIMESTAMPTZ::DATE is unimplemented there
  make_timestamp(epoch_ms(s."time") * 1000)::DATE   AS date,
  count({{expr}})                                  AS n,
  avg({{expr}})                                    AS mean,
  sum({{expr}} * m.weight) / sum(m.weight)         AS mean_wt,
  stddev_samp({{expr}})                            AS sd,
  min({{expr}})                                    AS min,
  max({{expr}})                                    AS max,
  quantile_cont({{expr}}, 0.10)                    AS p10,
  quantile_cont({{expr}}, 0.90)                    AS p90,
  sum(m.weight)                                     AS weight_sum
FROM {{slab}} s
JOIN {{mask}} m
  ON  ROUND(s.latitude::DOUBLE,  3) = ROUND(m.latitude::DOUBLE,  3)
  AND ROUND(s.longitude::DOUBLE, 3) = ROUND(m.longitude::DOUBLE, 3)
WHERE {{expr}} IS NOT NULL
GROUP BY 1
ORDER BY 1
