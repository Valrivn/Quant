# XBRL Sector Concept Chains — Financial & Healthcare

**Status:** Implemented (SEC 4.x)
**Deliverable:** concept chain definitions, taxonomy loader, validators, tests
**Modules:**
| File | Purpose |
|---|---|
| `discovery/sector_xbrl_chains.py` | Frozen concept registries + relationship graph for financial & healthcare sectors |
| `discovery/taxonomy_loader.py` | `SectorTaxonomyLoader` — merges sector chains into the base parser `CONCEPT_CHAINS` |
| `discovery/sector_validators.py` | Sector-specific plausibility checks + derived-ratio computation |
| `tests/test_sector_xbrl_chains.py` | 113 unit tests: chains, graph integrity, filters, provenance, loader |
| `tests/test_sector_xbrl_integration.py` | 23 integration tests with sample filings (bank, pharma, managed care, REIT, insurer) |
| `docs/data/xbrl_sector_concept_chains.json` | Serialized concept graph (100 nodes, 53 relations) |
| `docs/data/xbrl_sector_concept_chains.yaml` | YAML twin for the same graph |

---

## 1. What is a concept chain?

A concept chain is an **ordered fallback list of XBRL tags** that all resolve
to the same economic concept. SEC filers use different GAAP tags across years
and filing regimes; the chain tries candidates in order and coalesces the first
non-null value for the filing. This is the same mechanism that the existing
`CONCEPT_CHAINS` dict in `discovery/xbrl_parser.py` uses — this work *extends*
it with sector awareness, hierarchy, and provenance, without changing the
existing parser contract.

**Example (banking):**
```
interest_income ->
  [InterestIncomeExpense,
   InterestIncome,
   InterestIncomeDepositsWithFinancialInstitutions,
   InterestAndDividendIncomeOperating,
   InvestmentIncomeNet]
```

## 2. Sector taxonomy

| Sector | Sub-sector | Concept count | Relation count |
|---|---|---|---|
| Financial | Banking | 15 | 8 |
| Financial | Insurance | 16 | 6 |
| Financial | Asset management | 11 | 5 |
| Financial | REITs | 18 | 14 |
| Healthcare | Pharma / biotech | 14 | 5 |
| Healthcare | Medical devices | 8 | 0 |
| Healthcare | Healthcare services | 12 | 6 |
| Healthcare | Managed care | 17 | 11 |

Totals: **100 concepts, 53 relations.**

## 3. Mapping logic

### 3.1 Concept chain semantics
- **Order within a chain is precedence, not priority of quality.** The first
  tag present in the filing's companyfacts wins. Generally the most specific /
  most-compliant tag (e.g. `RevenueFromContractWithCustomerExcludingAssessedTax`
  for ASC 606) is listed first, mirroring the base parser convention.
- **Empty chains = computed or supplemental.** Concepts with `chain: ()` are
  either derived (`is_computed: True`, e.g. `ffo`, `noi`) or supplied from
  supplemental/MD&A data (`is_computed: True`, e.g. `total_members`,
  `occupied_sqft`). They are *not* fetchable from SEC companyfacts directly.
- **Units**: money = USD; counts (membership, beds, sqft) = `shares` unit in
  the taxonomy (no `USD` unit in XBRL).

### 3.2 Relationship graph
- `calc_sum` — parent = weighted sum of children (weight +1.0 or −1.0).
  Example: `noi = rental_revenue − property_operations − real_estate_taxes`.
- `calc_weighted` — reserved for weighted aggregates (none used yet).
- `derived` — ratio concepts computed from two sources. Example: `tier1_ratio = tier1_capital / risk_weighted_assets`.
- `def_is_a` / `def_member_of` — hierarchy semantics. Example: `total_loans`
  is a member of `total_assets`.
- `pres_child` — reserved for presentation-tree ordering (not yet exercised).

### 3.3 Derived ratios recognized by the validators
| Ratio | Formula | Sub-sector |
|---|---|---|
| `net_interest_margin` | net_interest_income / earning assets | Banking |
| `tier1_ratio` | tier1_capital / risk-weighted assets | Banking |
| `efficiency_ratio` | noninterest expense / net revenue | Banking |
| `loss_ratio` | losses incurred / premiums earned | Insurance |
| `expense_ratio` | underwriting expense / premiums earned | Insurance |
| `combined_ratio` | loss ratio + expense ratio | Insurance |
| `implied_fee_rate` | management fees / AUM | Asset mgmt |
| `expense_ratio` (fund) | fund operating expenses / AUM | Asset mgmt |
| `noi` | rental revenue − prop ops − prop taxes | REIT |
| `ffo` | net income + depreciation − gains on sale | REIT |
| `occupancy_rate` | occupied sqft / total leasable sqft | REIT |
| `gross_margin` | (revenue − COGS) / revenue | Pharma |
| `rd_intensity` | R&D expense / revenue | Pharma |
| `revenue_per_visit` | patient revenue / discharges | Hc services |
| `medical_loss_ratio` | medical expenses / premium revenue | Managed care |
| `administrative_ratio` | admin expense / premium revenue | Managed care |
| `premium_yield` | premium revenue / membership | Managed care |
| payer mix shares | segment members / total members | Managed care |

## 4. Sources

| Source | Used for | Reference format |
|---|---|---|
| SEC EDGAR companyfacts | All chains (tag names must match `data.sec.gov/api/xbrl/companyfacts` `us-gaap` namespace) | `accn` + `filed` + `end` |
| FASB ASC | Chain selection rationale; calculation semantics | `ASC <topic>-<subtopic>-<section>` (e.g. `ASC 942-20`) |
| NAIC SAP | Insurance statutory concepts (reserves, surplus) | `Schedule P / A` |
| NAREIT | FFO / AFFO definitions | NAREIT white papers |
| CMS / ACA | MLR floors (80% individual/small-group, 85% large-group) | 42 U.S.C. § 300gg-18 |
| Internal | Computed concepts, supplemental KPI provenance | `internal` |

## 5. Provenance

Every `ConceptNode` and `Relation` carries a `ProvenanceRecord`:
```
source: enum(sec_edgar | fasb_asc | iasb_ifrs | naic_sap | cms_hcpcs | fda_510k | internal)
reference: string   # e.g. "ASC 326-20-30" or "NAREIT FFO definition"
version: "2024"     # taxonomy year
```
The loader exposes `get_provenance(concept_key)` and the serialized artifacts
include provenance on every node and edge.

## 6. Integration with the existing pipeline

`SectorTaxonomyLoader.merged_chains_for_parser(sector=..., subsector=...)`
returns a merged `{friendly_name: [tags]}` dict compatible with the base
`CONCEPT_CHAINS` shape. Consumers:

- `discovery/xbrl_parser.py` — base chain resolution unchanged.
- `discovery/sec_pit_scraper.py` — PIT fundamentals; sector chains can be
  passed through via a loader instance without touching the extraction loop.
- `valuation_alpha/pipeline.py` — `build_fields_map()` remains the current
  entry point; the sector loader is additive and does not mutate it.
- Screen workers consume `FilingRecord.facts` (friendly names) + 
  `FilingRecord.concepts_used` (which XBRL tag won). Validator output is
  `ValidationResult.to_dict()`-serializable for JSON logging.

**Backwards-compatibility guarantees (tested):**
1. Base `CONCEPT_CHAINS` is never mutated by importing sector modules.
2. Base keys and sector keys are disjoint (no silent overwrite).
3. `extract_filing_from_companyfacts` signature and record shape unchanged.
4. The `scripts/reparse_10k.py` re-extraction pattern still works.

## 7. Validation rules

Validators run per sub-sector and return levels `error` / `warning` / `info`:
- **errors** — economically impossible (negative royalty, negative deferred
  revenue, gross margin outside [0,1], Tier 1 below Basel floor).
- **warnings** — unusual but not impossible (underwriting loss, MLR below ACA
  floor, payer mix reconciliation failure, reserve ratio outliers).
- **info** — actually *derived* the ratio from components (NIM, MLR, FFO, NOI,
  etc.) so downstream screens can grab the value without re-deriving.

## 8. Running the tests

```
python -m pytest tests/test_sector_xbrl_chains.py -q      # 113 unit tests
python -m pytest tests/test_sector_xbrl_integration.py -q # 23 integration tests
```