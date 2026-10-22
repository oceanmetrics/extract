-- per date x class composition for one place from a categorical ERDDAP griddap slab (seascapeR's
-- sum_ss_grds_to_ts(): how much of the place is in each class on each date).
--   {{expr}}  the class expression over the slab alias `s` (e.g. s."CLASS"); cast to BIGINT here
--   {{slab}}  the registered griddap file (parquet) or view
--   {{mask}}  the mask table: latitude, longitude, weight (0,1]
-- `fraction` is area-weighted (partial boundary cells count for their overlap) and sums to exactly 1
-- over the classes of a date; `percent_cells` is the unweighted cell count share.
-- the join is the same exact equality on ROUND(x, 3) as stats_daily.sql (float32 axis vs float64 mask).
WITH cells AS (
  SELECT
    -- utc day; via epoch_ms because duckdb-wasm ships without icu, so TIMESTAMPTZ::DATE is unimplemented there
    make_timestamp(epoch_ms(s."time") * 1000)::DATE AS date,
    CAST({{expr}} AS BIGINT)                        AS class,
    m.weight                                        AS weight
  FROM {{slab}} s
  JOIN {{mask}} m
    ON  ROUND(s.latitude::DOUBLE,  3) = ROUND(m.latitude::DOUBLE,  3)
    AND ROUND(s.longitude::DOUBLE, 3) = ROUND(m.longitude::DOUBLE, 3)
  WHERE {{expr}} IS NOT NULL
),
totals AS (
  SELECT date, count(*) AS n_total, sum(weight) AS weight_total
  FROM cells
  GROUP BY 1
)
SELECT
  c.date                                    AS date,
  c.class                                   AS class,
  count(*)                                  AS n,
  sum(c.weight)                             AS weight,
  sum(c.weight) / any_value(t.weight_total) AS fraction,
  100.0 * count(*) / any_value(t.n_total)   AS percent_cells
FROM cells c
JOIN totals t USING (date)
GROUP BY c.date, c.class
ORDER BY 1, 2
