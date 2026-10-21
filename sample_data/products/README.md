# Sample product catalogue: SYNTHETIC

Public product-family names (Michelin, Bridgestone) with **invented** sizes,
classifications and test results, chosen to exercise R117 limit selection:
section-width bands, XL / reinforced, severe snow (3PMSF), ice grip, run flat,
low-profile high-speed (aspect ratio ≤ 40, ≥ 300 km/h), C-marked van/truck tyres.

**Not real Michelin or Bridgestone product or test data.** Don't use it for decisions.

Blank cell = unknown (never read as "no"). Booleans: true/false.

test_results.csv: `status` = valid | invalid; `valid_until` (optional) = expiry date. Deliberate
edge cases: S02 expired test, S05 invalid noise test, S11 noise test in the wrong unit, several sizes
with no test for a metric.
Columns are illustrative; align them with regpilot's own `sample_data/products/README` when merging.
