# Hospital registry

The static tier of the Destination Advisor: which hospitals exist, where they are, and what they can
do around the clock. Loaded by `advisor.registry.load_registry()`.

**These files are header-only on purpose.** Nobody should invent a hospital or a capability. Rows are
added only after checking with the hospital (phone call) or its official website. Test data lives in
`tests/fixtures/` and is fictional.

## Files

`hospitals.csv` has one row per hospital.

| column | meaning |
|---|---|
| hospital_id | short stable id, unique (e.g. `blr-0001`) |
| name | official name |
| lat, lon | decimal degrees (WGS84) |
| ownership | `govt` or `private` |
| empanelment | schemes, separated by `;` (e.g. `PMJAY;SAST`); blank if unknown |
| icu_beds_total, ventilators_total | whole numbers; blank if unknown (never guess) |
| provenance, source, as_of | tag for this row's location, ownership and counts (see below) |

`capabilities.csv` is long format, with one row per hospital and capability.

| column | meaning |
|---|---|
| hospital_id | must exist in `hospitals.csv` |
| capability | one of `ed_24x7, ct_24x7, pci_24x7, thrombolysis, thrombectomy, trauma_ot, neurosurgery, orthopaedics, blood_bank, obstetric_emergency, nicu, picu, burns_unit` |
| available | `true` or `false` |
| hours | `24x7`, `daytime` or `unknown` |
| provenance, source, as_of | tag for this one capability |

If you don't know about a capability, leave its row out. The advisor treats a missing capability as
**unknown, so absent**. A hospital is never sent a patient on a guess. Add a `false` row only when the
hospital confirms it does not offer the service.

## Provenance tags

| tag | use when | as_of |
|---|---|---|
| `REAL` | confirmed by phone with the hospital, read from its official website, or taken from a named public dataset | **required**: date of the call or retrieval |
| `REAL_DERIVED` | computed from real data; write the method in `source` | **required** |
| `SIMULATED` | no real source yet (demo or test rows only) | optional |
| `PLACEHOLDER` | not used for registry rows (config values only) | optional |

Every `REAL` row needs a `source` that someone else can check, plus the verification date in `as_of`
(`YYYY-MM-DD`). Good sources look like these:

- `phone: casualty desk, spoke to duty CMO, 2026-11-20, verified by <initials>`
- `website: https://example-hospital.in/emergency (cardiology page)`

## Validation

The loader stops and names the file, line and field if it finds any of these problems:

- missing provenance or source
- `REAL`/`REAL_DERIVED` without `as_of`
- an unknown capability key
- a capability row for an unknown hospital
- duplicate hospital ids or duplicate hospital/capability pairs
- lat/lon that is not a number or is out of range
- ownership other than `govt`/`private`
- hours other than `24x7`/`daytime`/`unknown`

## Seeding locations from the National Health Portal

`advisor.registry.load_nhp_directory(path, as_of=...)` reads a local copy of the data.gov.in
"Hospital Directory (National Health Portal)" CSV
(https://data.gov.in/catalog/hospital-directory-national-health-portal). It tags those rows `REAL`
with that URL as the source and the download date as `as_of`. It gives names, locations and, where
the category allows, ownership. It gives **no capabilities**, and it skips and counts rows with no
coordinates. Its column names are assumptions and need checking against the downloaded file. Use it
to find candidates, then verify each one before you add it here.
