# F5 · Pets diagnosis

Phase F5 of [plan.md](plan.md), done on 2026-11-22. It explains why the suggested prices for Pets look too high and what F7 has to change. It changes no pipeline code and no price.

This file is public (the GitHub repo is public). It only holds competitor-side statistics (public market data), counts, aggregated ratios and conclusions. Per-SKU detail (Wayakit costs, suggested prices, quotes and margins per product) is in the local, git-ignored `data/f5_pets/pets_report.md`. See [How to reproduce](#how-to-reproduce).

## Purpose and data used

The question: Pets suggestions look too high. The suspected cause was imported US brands, sold through resellers at 140–720 SAR, against Wayakit 0.7 L products priced far lower.

Data: the frozen F1 snapshot `data/f1_before_2026-11-15/` (git-ignored; never copy it into tracked files), which is what produced the suggestions now in Odoo:

- `ml_model/competitor_products_from_odoo.csv`: Odoo `competitor.product`. **All 201 Pets competitor rows come from here.** The 13–15 Nov 2026 scrape hit the 30 h timeout and brought no Pets rows (plan.md, finding 11; `scraper/competitors_complete.csv` has 0 Pets rows).
- `ml_model/competitor_volumetric_processed.csv` and `competitor_unit_processed.csv`: price per liter or per piece, as training reads them.
- `ml_model/wayakit_products_to_predict_odoo.csv` (32 Pets SKUs), `wayakit_products.csv` (cost), `wayakit_cotizations.csv` (approved quotes) and `wayakit_prediction_report.csv` (the production predictions of that run).
- For a few comparisons, public wayakit.sa prices collected on 2026-11-22 (local file `data/f5_pets/wayakit_sa_prices_2026-11-22.csv`).

Pipeline steps involved: `ml_model/1a_preprocess_data.py` (price per liter or per piece), `ml_model/2_train_models.py` (`filter_outliers_with_percentiles`, `:46-60`; market stats from the trimmed competitors, `:82-111` and `:177`; one volumetric and one per-piece RandomForest with `company` as a feature and `min_samples_leaf=1`, `:66`, `:160-167`) and `ml_model/3_predicted_prices.py` (`volume_outside_market` computed at `:103` and `:120` and only reported; clamp to `market_min`–`market_max` with 3 or more competitors, `:125-128`; cost × 1.30 floor, `:131-133`).

## How to reproduce

From the repo root, in the conda environment `wayakit-price-prediction`:

```bash
conda run --no-capture-output -n wayakit-price-prediction \
  python ml_model/pets_diagnosis.py --data-dir data/f1_before_2026-11-15
```

It is read-only: it reads no secrets, does not touch Odoo or the network, and writes nothing in `ml_model/` or `scraper/`. It reuses the pipeline's own `load_and_clean_raw_data()` (1a) and `filter_outliers_with_percentiles()` (2), so "after trimming" is exactly what training and the market stats saw. It writes to `data/f5_pets/` (git-ignored, may contain costs): `pets_by_type.csv`, `pets_wayakit_products.csv` and `pets_report.md`. The column definitions are in the script's docstring and at the top of `pets_report.md`. F7 can rerun it before and after its changes (`--data-dir` takes any folder laid out like the repo; `--subindustry` and `--out-dir` diagnose another subindustry).

The counterfactual retrains quoted below (rows removed, no trimming, other seeds) were scratch runs of the unchanged `2_train_models.py` and `3_predicted_prices.py` on copies of the snapshot. They are not in the repo. They compare local runs with local runs (finding 13).

## Competitor side by type

Prices are SAR per liter (volumetric model) or SAR per piece (units model), from the processed files **before** trimming. `max after trim` is the `market_max` the clamp uses. `% US size` is the share of mL rows whose size is a common US bottle size (16 fl oz = 473 mL, 32 fl oz = 946 mL, 1 gal = 3785 mL...). It is a heuristic hint, not an origin field. Multipacks cannot be measured: Odoo rows have no product title (every type is "n/a"). No Pets row has price 0.

| Type | Model | n raw | n before trim | n after trim | Median | p75 | Max | Max after trim | % fl oz | % US size | Brands | ≤ 5 mL | Wayakit SKUs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| P1-Coat conditioner and detanglers | vol | 2 | 2 | **0 (emptied)** | 70.83 | 98.98 | 127.12 | – | 0 | 100 | 1 | 0 | 0 |
| P1-Deodorizing wipes | vol | 1 | 1 | 1 | 440 | 440 | 440 | 440 | 0 | 0 | 1 | 0 | 0 |
| P1-Waterless shampoo | vol | 26 | 26 | 21 | 224.6 | 356.02 | 885 | 595 | 0 | 19.2 | 19 | 0 | 2 |
| P2-Air freshener | vol | 4 | 4 | 2 | 370.68 | 404.34 | 473.76 | 381.2 | 0 | 50 | 3 | 0 | 0 |
| P2-Litter box deodorizer | vol | 6 | 6 | 4 | 135.62 | 245.54 | 464.18 | 280 | 0 | 33.3 | 6 | 0 | 2 |
| P2-Pet-specific odor eliminator for home | vol | 8 | 8 | 6 | 206.67 | 243.88 | 632.91 | 298.73 | 0 | 25 | 5 | 0 | 2 |
| P3-Carpet cleaner for pet accidents | vol | 14 | 14 | 10 | 130.33 | 187.15 | 1257.14 | 385.62 | 0 | 42.9 | 12 | 0 | 2 |
| P3-Enzymatic cleaner for pet stains | vol | 9 | 9 | 7 | 209.3 | 361.9 | 515.49 | 400.63 | 0 | 66.7 | 7 | 0 | 0 |
| P3-Pet urine odor remover | vol | 24 | 24 | 18 | 169.48 | 410.36 | 1457.63 | 487.83 | 0 | 50 | 18 | 0 | 4 |
| P4-Flea and tick home spray | vol | 27 | 25 | 19 | 314.59 | 699 | 59980 | 1047 | 0 | 48 | 14 | 1 | 2 |
| P5-Antiseptic spray | vol | 11 | 11 | 9 | 527.08 | 699.4 | 999.83 | 886.67 | 0 | 0 | 9 | 0 | 2 |
| P5-Pet-safe wound washes | vol | 4 | 4 | 2 | 1300 | 1495.82 | 1966.33 | 1338.98 | 0 | 50 | 4 | 0 | 0 |
| P6-Aquarium cleaner | vol | 9 | 9 | 7 | 437.5 | 559.32 | 686.68 | 628.69 | 0 | 66.7 | 6 | 0 | 0 |
| P6-Cage and habitat cleaner | vol | 12 | 12 | 8 | 164.64 | 746.25 | 2568 | 1185 | 0 | 33.3 | 11 | 0 | 2 |
| P6-Pet-safe disinfectant and cleaner | vol | 10 | 10 | 8 | 77.49 | 238.88 | 1730 | 550 | 0 | 0 | 9 | 0 | 2 |
| P7-General purpose pet shampoo | vol | 9 | 9 | 7 | 304.05 | 11428.6 | 14285.7 | 13333.3 | 0 | 22.2 | 8 | 0 | 0 |
| P7-Medicated pet shampoo | vol | 1 | 1 | 1 | 14285.7 | 14285.7 | 14285.7 | 14285.7 | 0 | 0 | 1 | 0 | 0 |
| P8-Ear cleaning solution | vol | 4 | 4 | 2 | 616.49 | 654.02 | 666.1 | 650 | 0 | 0 | 3 | 0 | 0 |
| P8-Eye washes and lubricant eye drops | vol | 1 | 1 | 1 | 460 | 460 | 460 | 460 | 0 | 0 | 1 | 0 | 0 |
| P1-Deodorizing wipes | units | 14 | 14 | 10 | 0.78 | 3.67 | 22.65 | 4.99 | – | – | 13 | – | 2 |
| P8-Ear cleaning wipes | units | 2 | 2 | **0 (emptied)** | 0.46 | 0.55 | 0.64 | – | – | – | 2 | – | 0 |
| P8-Tear stain remover | units | 3 | 3 | 1 | 0.25 | 2.5 | 4.74 | 0.25 | – | – | 2 | – | 0 |

Totals: 201 raw rows (0 scraper, 201 Odoo), 199 before trimming (1a drops the 2 P4 rows in `g`, and logs it), 144 after trimming. 21 distinct types with competitors: 19 volumetric and 3 units, which makes 22 model groups because P1-Deodorizing wipes is in both. 0 rows in fl oz, 63 rows at a US size in mL, 1 row at 5 mL or less.

**Bug 6 (finding 12) in Pets:** the p10–p90 trimming empties **2 Pets types**: 1 of 19 volumetric (P1-Coat conditioner, 2 different prices) and 1 of 3 unit (P8-Ear cleaning wipes, 2 different prices). Neither has a Wayakit SKU, so no Pets suggestion loses its market range or clamp because of it. Pets accounts for 2 of the 15 types emptied across all subindustries (14 volumetric + 1 unit).

## Wayakit side (aggregate only)

32 Pets SKUs: 22 with a size (20 volumetric, 2 per-piece wipe packs) and 10 parent templates with no size.

| Measure | Value |
|---|---|
| Sized SKUs below / inside / above the trimmed competitor size range of their type | 0 / 20 / 2 (the two 4 L SKUs are above the 3.788 L maximum) |
| SKUs without competitors (no size, model not used) | 10 |
| Clamped to the market range | 6, **all upward**, 0 downward (3 types: waterless shampoo, urine odor remover 0.7 L, cage and habitat cleaner) |
| Price set by the cost × 1.30 floor | 10, exactly the 10 no-size SKUs; it binds on no sized SKU |
| Median predicted / cost, 22 sized SKUs | 14.16 (8 SKUs with a quote: 9.83; 14 without: 20.90) |
| Median predicted / cost, all 32 SKUs | 10.39 (pulled down by the 10 floor-priced templates) |
| Median predicted / median approved quote, 8 sized SKUs with a quote | 0.99 (range 0.85–1.12) |
| Sized SKUs with no approved quote | 14 of 22 (whole types without a quote: flea and tick, cage and habitat cleaner; no quote for the 4 L size) |

The quoted SKUs come out close to their quote by construction: `company='Wayakit'` is a feature, `min_samples_leaf=1` and the size matches, so the forest echoes Wayakit's own quote rows. That shows the suggestion repeats the current price; it is not independent evidence that the price is right against the market. The high suggestions are in SKUs with no quote anchor.

## Root causes, ranked by impact

Each cause was checked against the snapshot by two independent verifications. "Partially verified" means the original statement was corrected, and the corrected version is the one given here.

1. **The 4 L Pet urine odor remover (2 SKUs) is priced from a few large, mostly US-size competitor rows, and nothing stops it** (partially verified). These are the highest Pets suggestions, more than double the public wayakit.sa price for the same size and about 2x the per-liter price of the same product's 0.7 L. There is no Wayakit quote at 4 L, `model_confidence` is 0, and the price is the raw RandomForest output. Leaf-membership weights for the 4 L input in a local retrain: Febreze pet odor eliminator 2 L and 4 L rows (P2, another type; the 4000 mL row at 225.6 SAR/L alone carries much of the value) 36 %, Rocco & Roxie 3788 mL and 946 mL (1 gal and 32 fl oz) about 14–20 %, Simple Solution about 3.6 %, and Wayakit's 0.7 L quote about 20 %. The forest reads one shared `volume_liters` feature, so it pools the few Pets rows of 2 L or more across types. `volume_outside_market` is True (trimmed size maximum 3.788 L) but the code only reports it. The clamp range for this type is 66.67–487.83 SAR/L, so the clamp does not bind. Trimming does not help: the p10 cut removed the cheapest gallon (Stink Free, 62.5 SAR/L), and retraining without trimming still leaves the 4 L high. Even with a corrected model, the clamp floor (`market_min` 66.67 SAR/L × 4 L) is above the public wayakit.sa price for the 4 L.
2. **Flea and tick home spray (2 SKUs, 0.7 L) has no Wayakit quote anchor and is priced from the P4 competitor set of US-size vet and insecticide brands** (partially verified). The suggestion is 2.3–3.8x the other Wayakit 0.7 L Pets suggestions and 1.9–4.1x their median approved quotes, with `model_confidence` 0. Hartz (473/651/946 mL), Adams (710 mL), Vet's Best (945 mL) and wondercide (946 mL) carry about 71 % of the leaf weight. The same brand appears at very different prices: Hartz from 74 to 377 SAR/L, and Vet's Best 945 mL at both 165 and 520 SAR. P4 has 25 rows, 19 after trimming (minimum 140, median about 315, maximum 1047 SAR/L). The p10 cut removed the three cheapest (58.1, 74.0 and 105.7 SAR/L); in a local retrain that adds about 26 %. Even without trimming, the suggestion would stay at about 2.3x the median quote of the other 0.7 L Pets SKUs. The clamp floor (140 SAR/L × 0.7 L = 98 SAR) does not bind, and it is itself about 1.7x that median quote.
3. **p10–p90 trimming plus the ≥ 3-competitor clamp only push Pets prices up** (verified). Trimming raises `market_min`: waterless shampoo 18.26 → 70.42 SAR/L, urine odor remover 60.00 → 66.67, cage and habitat cleaner 64.00 → 95.80. The clamp then lifts 6 SKUs (3 types) to the trimmed minimum × 0.7 L: +10.7 %, +9.9 % and +19.1 % over the model price. In 2 of them the model price equals the current approved quote, so they end about 10 % above it. The other 4 have no quote. Without trimming, none of the 6 would be clamped. The effect is small in SAR and does not explain causes 1 and 2, which are not clamped. Single-row types keep their minimum; the 2-row types are emptied (bug 6).
4. **Small wipe packs keep the per-piece clamp from binding on the 20-piece wipes (2 SKUs)** (verified). The P1-Deodorizing wipes `market_max` is 4.99 SAR/piece, set by two small packs that survive the trim (Mumoo Bear 10 pcs at 4.99/pc, MAKINGTEC 9 pcs at 4.44/pc). YEGBONG 2 pcs (22.65/pc) and Generic 9 pcs (6.34/pc) are trimmed out. Within P1 every pack of 40 pieces or more is at 1.36 SAR/pc or less. If the 4 Pets packs under 20 pieces are excluded, the trimmed group becomes n=8 (max 1.07/pc), the clamp binds and cuts both SKUs by about 29 %, which is below the current approved quote. The model's own per-piece price barely moves. So the small packs do not inflate the model; they only switch off the cap. The 2/9/10 piece counts may be pack counts recorded as piece counts; the snapshot has no titles to confirm it.
5. **The Pets competitor data carries no seller, reseller, origin, fl oz or channel signal** (partially verified; structural, underlies causes 1 and 2). All 201 rows come from Odoo `competitor.product`: B2C 201, B2B 0 (Pets is the only subindustry with no B2B rows); units mL 180, Units 19, g 2; none in fl oz. `FIELDS_TO_GET` (`ml_model/odoo_api_competitor_products.py:24-36`) requests 11 fields with no name, URL, seller or country. `CHANNEL_MAP` (`:38-42`) folds `retail` and `ecommerce` into B2C, and `UOM_MAP` (`:44-48`) only maps ml, g and units, so fl oz cannot appear. `Company` is the brand: 120 raw labels (112 after case and punctuation normalization), none a marketplace or reseller. A reseller such as Desertcart cannot be seen in the data, and a data-only `origin` column would be `unknown` for 201 of 201 rows, in all 22 model groups. "Imported" in causes 1 and 2 is an inference from US sizes and brand names, not a data field.
6. **10 Pets parent templates with no size get the cost × 1.30 floor** (verified; downward, a data defect). `Volume_Liters` and `Pack_quantity_Units` are 0, so neither model branch runs (`3_predicted_prices.py:76-77`), the clamp does not apply (0 competitors) and the floor sets the price. The upload does not filter them, so they are written to Odoo as suggestions. Most are about a tenth of their sized children's suggestions. They have no quotes. They are not part of the "too high" problem, but they pull the Pets median predicted/cost from 14.16 down to 10.39.
7. **Implausible small sizes distort the per-type statistics but do not measurably move any Wayakit suggestion** (partially verified). Azonee 1 mL at 59.98 SAR (59,980 SAR/L; the same brand lists 50 mL and 100 mL at normal prices) is trimmed out, but it raises the P4 p90 bound, so a 1047 SAR/L row survives. That only changes the reported P4 `market_max`, which never binds. Five P7 shampoo rows at 25–35 mL priced 250–500 SAR (10,000–14,286 SAR/L) make the P7 median and p75 absurd; no Wayakit SKU is in P7. In 3-seed retrains, removing either group moves the flea and tick SKUs within seed noise (about +3 % to −7 %, no consistent sign).

## What F7 should do

Within the F7 rules in [plan.md](plan.md#f7--pets-and-ml-tuning-2-days): `origin` only from data, no hand-maintained brand lists, robust trimming for bug 6, a `p75 × volume` ceiling and the 1.30 floor.

- **Do not expect `local` rows in Pets from today's data.** "At least N local rows per type" is false for every Pets type until rows with seller or origin arrive (F6, Apify). Two cheap data-only steps, not verified here (no Odoo access in F5): keep the raw `product_channel` (retail vs ecommerce) instead of folding it into B2C, and check in Odoo KSA (company confirmed first) whether `competitor.product` has a seller, URL or country field the reader could add. Do not infer origin from `Company` (that would be a brand list). US-standard sizes (473/946/3785 mL) can be reported as a hint, but they are not an origin.
- **Robust trimming (bug 6) before any bound is computed.** Drop sizes of 5 mL or less, and sizes inconsistent with the same brand's other rows, *before* the quantiles (today they move the p10/p90 bounds even when they are trimmed out). For n small, do not apply p10–p90 (it empties 2-price groups). Add a plausibility rule from the data itself (for example SAR/L above k × the type median when n ≥ 5), not a brand list.
- **Market range and clamp.** Do not use the p10 bound as the clamp floor: compute `market_min`/`market_max` from the robust set (price > 0, no ≤ 5 mL, no multipacks, local rows first once origin exists). Let the clamp raise a price only up to a robust low statistic, and never above an existing approved quote. Take `market_max` from a robust percentile, not the raw maximum, so a single 2-piece pack cannot set the ceiling.
- **Size matching.** Compare against rows of similar size (about 0.5x–2x of the SKU's liters or pieces), with a fallback to all rows when there are fewer than N matches. The `p75 × volume` ceiling only bites when p75 comes from size-matched rows: over all sizes it is above every current sized Pets suggestion, because small packs inflate per-liter p75.
- **Act on `volume_outside_market`.** Stop extrapolating price per liter beyond the competitor size range (today it is computed and ignored). For example, cap a larger Wayakit size at the per-liter price of Wayakit's own smaller size of the same product, and make sure the clamp floor cannot lift it back.
- **No-anchor types** (flea and tick, cage and habitat cleaner, the 4 L size): use the robust statistic with the size-matched `p75 × volume` ceiling, keep the 1.30 floor, and send them to the F8 review flagged as having no quote anchor.
- **No-size templates:** leave them out of the Pets before/after metrics, and either skip them in the upload or mark them as having no suggestion instead of posting the floor. Their sizes, and any placeholder-looking costs (listed in the local report), are fixed in Odoo product master data, not in a list in the code.
- **Before writing to Odoo,** compare the new Pets suggestions with the median approved quote where one exists, and rerun `ml_model/pets_diagnosis.py` on the new outputs for the before/after comparison.

## Limitations

- **No live Odoo KSA price.** The current selling price is approximated by the median approved quote per SKU (`wayakit_cotizations.csv`), which exists for only 8 of the 22 sized Pets SKUs. Public wayakit.sa prices were used for a few comparisons; they include VAT and do not list every Pets product.
- **The snapshot predates F2.** It is the output of the 13–15 Nov 2026 run on e25e2c9. F2 did not change the ML (its differential showed no price change), but rows and models will differ in the first monthly run.
- **Float noise (finding 13).** Local retrains differ from the production report by up to about 4.6 % on Pets SKUs, and seed changes move single SKUs by about 3–4 %. Counterfactual effects of that size are not distinguishable from noise; only the larger effects above are claimed.
- **No Pets scrape rows in that run.** Every Pets competitor row comes from Odoo, without titles, so multipacks cannot be measured, fl oz is 0 by construction, and "imported" can only be inferred, not observed.
- **Not established.** Some hypotheses were checked and rejected or narrowed:
  - US-size rows do raise the descriptive per-liter level in the 300–1100 mL band (median about 1.8x the metric rows), but removing them does not lower the 0.7 L Pets suggestions consistently. The effect is mixed, and it mostly works through the clamp floor and the trimming bounds rather than through the model.
  - The low clamp floors are set by cheap rows, but expensive rows also move the p10 cut point (for the cage and habitat cleaner, removing the expensive US-size rows removes the clamp).
  - The g rows dropped by 1a and the one wipes row recorded in mL do slightly change P4 and other predictions through the shared RandomForest; they are not zero-effect, but they are not a main cause.
  - Bug 6 has no direct effect on any Pets SKU. A possible indirect effect through the shared RandomForest (the emptied rows change the training set) was seen in one local experiment only and is not part of the verified findings.
