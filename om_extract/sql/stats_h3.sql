-- key statistics per period for one place from an H3 layer joined with the place's H3 membership.
--   {{expr}}  the value expression over the layer alias `s` (e.g. s."shannon")
--   {{slab}}  the layer's cells: `cell` (the cell id, as the membership holds it), `period` (null when the layer
--             has none) and the variable
--   {{mask}}  the place's membership: `cell`, `share` (0,1], the fraction of the cell's area inside the place,
--             which plays the part of the grid mask's partial-area weight
-- n counts the cells with a value; mean, sd, min, max, p10 and p90 are over those cells, unweighted;
-- mean_wt = sum(value * share) / sum(share) and weight_sum = sum(share), over the same cells.
SELECT
  s.period                                         AS period,
  count({{expr}})                                  AS n,
  avg({{expr}})                                    AS mean,
  sum({{expr}} * m.share) / sum(m.share)           AS mean_wt,
  stddev_samp({{expr}})                            AS sd,
  min({{expr}})                                    AS min,
  max({{expr}})                                    AS max,
  quantile_cont({{expr}}, 0.10)                    AS p10,
  quantile_cont({{expr}}, 0.90)                    AS p90,
  sum(m.share)                                     AS weight_sum
FROM {{slab}} s
JOIN {{mask}} m
  ON s.cell = m.cell
WHERE {{expr}} IS NOT NULL
GROUP BY 1
ORDER BY 1
