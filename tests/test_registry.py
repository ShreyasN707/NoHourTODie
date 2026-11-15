"""Registry loader tests. All hospitals here are fictional fixtures tagged SIMULATED ("test fixture")."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from advisor.provenance import Provenance, ProvenanceError
from advisor.registry import (
    CAPABILITIES_CSV,
    HOSPITALS_CSV,
    NHP_DATASET_URL,
    NhpColumns,
    load_nhp_directory,
    load_registry,
)
from advisor.registry.loader import CAPABILITY_COLUMNS, HOSPITAL_COLUMNS

FIX = Path(__file__).parent / "fixtures"
H_HEADER = ",".join(HOSPITAL_COLUMNS)
C_HEADER = ",".join(CAPABILITY_COLUMNS)
H_OK = "fx-a,Hospital A (fictional),12.97,77.59,govt,,,,SIMULATED,test fixture,"


def write(tmp_path: Path, hospitals: list[str], caps: list[str] | None = None) -> tuple[Path, Path]:
    h, c = tmp_path / "hospitals.csv", tmp_path / "capabilities.csv"
    h.write_text("\n".join([H_HEADER, *hospitals]) + "\n")
    c.write_text("\n".join([C_HEADER, *(caps or [])]) + "\n")
    return h, c


# ---------------------------------------------------------------- happy path

def test_fixture_registry_loads_with_tags_and_summary():
    reg = load_registry(FIX / "registry" / "hospitals.csv", FIX / "registry" / "capabilities.csv")
    by_id = {h.hospital_id: h for h in reg.hospitals}
    assert set(by_id) == {"fx-a", "fx-b", "fx-c"}
    a = by_id["fx-a"]
    assert a.location_tag.provenance is Provenance.SIMULATED and a.location_tag.source == "test fixture"
    assert a.empanelment == ["PMJAY", "SAST"] and a.icu_beds_total == 20
    assert a.has("pci_24x7") and not a.has("ct_24x7")  # explicit false
    assert not a.has("nicu")  # not listed -> unknown -> absent
    assert "nicu" not in a.capabilities  # no defaults filled
    assert by_id["fx-b"].icu_beds_total is None
    assert by_id["fx-b"].capabilities["trauma_ot"].hours == "daytime"
    s = reg.summary
    assert s.hospitals == 3 and s.capability_rows == 5
    assert s.capability_provenance == {"REAL": 0, "REAL_DERIVED": 0, "SIMULATED": 5, "PLACEHOLDER": 0}
    assert s.location_provenance["SIMULATED"] == 3
    assert s.hospitals_without_capabilities == 1


def test_committed_templates_are_header_only():
    for path, cols in ((HOSPITALS_CSV, HOSPITAL_COLUMNS), (CAPABILITIES_CSV, CAPABILITY_COLUMNS)):
        lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
        assert lines == [",".join(cols)], f"{path} must stay header-only until verified rows are added"
    reg = load_registry()
    assert reg.hospitals == [] and reg.summary.capability_rows == 0


def test_real_row_with_as_of_is_accepted(tmp_path):
    h, c = write(tmp_path, ["x1,Hospital X (fictional),12.9,77.6,govt,,,,REAL,phone: test,2026-11-01"],
                 ["x1,ed_24x7,true,24x7,REAL,phone: test,2026-11-01"])
    reg = load_registry(h, c)
    assert reg.hospitals[0].location_tag.as_of == date(2026, 11, 1)
    assert reg.summary.capability_provenance["REAL"] == 1


# ---------------------------------------------------------------- fail loudly

@pytest.mark.parametrize(
    "row, field, exc",
    [
        ("fx-a,Hospital A (fictional),12.97,77.59,govt,,,,,test fixture,", "provenance", ProvenanceError),
        ("fx-a,Hospital A (fictional),12.97,77.59,govt,,,,SIMULATED,,", "source", ProvenanceError),
        ("fx-a,Hospital A (fictional),12.97,77.59,govt,,,,REAL,phone,", "as_of", ProvenanceError),
        ("fx-a,Hospital A (fictional),12.97,77.59,govt,,,,REAL_DERIVED,method,", "as_of", ProvenanceError),
        ("fx-a,Hospital A (fictional),12.97,77.59,govt,,,,GUESS,x,", "provenance", ProvenanceError),
        ("fx-a,Hospital A (fictional),97.0,77.59,govt,,,,SIMULATED,test fixture,", "lat", ValueError),
        ("fx-a,Hospital A (fictional),abc,77.59,govt,,,,SIMULATED,test fixture,", "lat", ValueError),
        ("fx-a,Hospital A (fictional),12.97,,govt,,,,SIMULATED,test fixture,", "lon", ValueError),
        ("fx-a,Hospital A (fictional),12.97,77.59,trust,,,,SIMULATED,test fixture,", "ownership", ValueError),
        ("fx-a,Hospital A (fictional),12.97,77.59,govt,,-1,,SIMULATED,test fixture,", "icu_beds_total",
         ValueError),
    ],
)
def test_bad_hospital_rows_fail_with_file_line_field(tmp_path, row, field, exc):
    h, c = write(tmp_path, [row])
    with pytest.raises(exc) as ei:
        load_registry(h, c)
    msg = str(ei.value)
    assert "hospitals.csv" in msg and "line 2" in msg and f"'{field}'" in msg


def test_duplicate_hospital_id(tmp_path):
    h, c = write(tmp_path, [H_OK, H_OK])
    with pytest.raises(ValueError, match=r"line 3.*duplicate id 'fx-a'"):
        load_registry(h, c)


@pytest.mark.parametrize(
    "row, field, exc",
    [
        ("fx-a,teleportation,true,24x7,SIMULATED,test fixture,", "capability", ValueError),
        ("fx-zz,ed_24x7,true,24x7,SIMULATED,test fixture,", "hospital_id", ValueError),
        ("fx-a,ed_24x7,true,always,SIMULATED,test fixture,", "hours", ValueError),
        ("fx-a,ed_24x7,maybe,24x7,SIMULATED,test fixture,", "available", ValueError),
        ("fx-a,ed_24x7,true,24x7,,test fixture,", "provenance", ProvenanceError),
        ("fx-a,ed_24x7,true,24x7,SIMULATED,,", "source", ProvenanceError),
        ("fx-a,ed_24x7,true,24x7,REAL,phone,", "as_of", ProvenanceError),
    ],
)
def test_bad_capability_rows_fail_with_file_line_field(tmp_path, row, field, exc):
    h, c = write(tmp_path, [H_OK], [row])
    with pytest.raises(exc) as ei:
        load_registry(h, c)
    msg = str(ei.value)
    assert "capabilities.csv" in msg and "line 2" in msg and f"'{field}'" in msg


def test_duplicate_capability_row(tmp_path):
    cap = "fx-a,ed_24x7,true,24x7,SIMULATED,test fixture,"
    h, c = write(tmp_path, [H_OK], [cap, cap])
    with pytest.raises(ValueError, match="duplicate 'ed_24x7'"):
        load_registry(h, c)


def test_missing_header_column(tmp_path):
    h = tmp_path / "hospitals.csv"
    h.write_text("hospital_id,name,lat,lon,ownership\n")
    c = tmp_path / "capabilities.csv"
    c.write_text(C_HEADER + "\n")
    with pytest.raises(ValueError, match="missing columns"):
        load_registry(h, c)


# ---------------------------------------------------------------- data.gov.in NHP directory

def test_nhp_loader_tags_real_and_counts_skips():
    # nhp_sample.csv is a fictional file in the ASSUMED NHP column layout, not real NHP data.
    res = load_nhp_directory(FIX / "nhp_sample.csv", as_of=date(2026, 11, 15), state="karnataka")
    assert res.rows_read == 7
    assert res.skipped_other_state == 1
    assert res.skipped_no_coordinates == 2  # blank, and the 0,0 sentinel
    assert res.skipped_bad_coordinates == 1
    by_id = {h.hospital_id: h for h in res.hospitals}
    assert set(by_id) == {"nhp-1", "nhp-2", "nhp-6"}
    assert by_id["nhp-1"].ownership == "govt" and by_id["nhp-2"].ownership == "private"
    assert by_id["nhp-6"].ownership == "unknown" and res.ownership_unknown == 1
    for h in res.hospitals:
        assert h.location_tag.provenance is Provenance.REAL
        assert h.location_tag.source == NHP_DATASET_URL
        assert h.location_tag.as_of == date(2026, 11, 15)
        assert h.capabilities == {}  # no capabilities from this source
    assert by_id["nhp-1"].lat == pytest.approx(12.97)


def test_nhp_loader_requires_as_of_and_matching_columns():
    with pytest.raises(TypeError):
        load_nhp_directory(FIX / "nhp_sample.csv", as_of=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="missing"):
        load_nhp_directory(FIX / "nhp_sample.csv", as_of=date(2026, 11, 15),
                           columns=NhpColumns(name="hospital_name"))


def test_nhp_loader_separate_lat_lon_columns(tmp_path):
    p = tmp_path / "nhp.csv"
    p.write_text("id,name,latitude,longitude,category\n"
                 "9,Hospital W (fictional),12.9,77.6,Government\n"
                 "10,Hospital Y (fictional),,,Private\n")
    cols = NhpColumns(name="name", coordinates=None, lat="latitude", lon="longitude",
                      ownership="category", state=None, record_id="id")
    res = load_nhp_directory(p, as_of=date(2026, 11, 15), columns=cols)
    assert [h.hospital_id for h in res.hospitals] == ["nhp-9"]
    assert res.skipped_no_coordinates == 1
