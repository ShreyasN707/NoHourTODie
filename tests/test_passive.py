"""Passive-tier tests: gate-dwell loader, rolling dwell index, outcome store.

All dwell records and outcomes here are synthetic test data tagged SIMULATED ("test fixture").
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from advisor.config import default_config, parse_config
from advisor.interfaces import DwellIndex
from advisor.models import DwellRecord, OutcomeEvent
from advisor.passive import (
    DwellColumns,
    OutcomeStore,
    RollingDwellIndex,
    load_dwell_csv,
    load_outcomes_csv,
)
from advisor.provenance import Provenance, ProvenanceError, Tagged

FIX = Path(__file__).parent / "fixtures"
SIM = Tagged(provenance=Provenance.SIMULATED, source="test fixture")
COLS = DwellColumns(hospital_id="hosp", arrived="in_ts", departed="out_ts", timestamp_format="%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- dwell loader

def test_dwell_loader_drops_and_counts_invalid_rows():
    res = load_dwell_csv(FIX / "dwell_sample.csv", SIM, max_minutes=240, columns=COLS,
                         known_hospital_ids={"fx-a", "fx-b"})
    assert res.rows_read == 8
    assert [r.minutes for r in res.records] == [20.0, 45.0]
    assert res.dropped["departed_before_arrived"] == 1
    assert res.dropped["zero_duration"] == 1
    assert res.dropped["over_max_duration"] == 1
    assert res.dropped["missing_field"] == 1
    assert res.dropped["bad_timestamp"] == 1
    assert res.dropped["unknown_hospital"] == 1
    assert res.dropped_total + len(res.records) == res.rows_read
    assert res.tag.provenance is Provenance.SIMULATED


def test_dwell_loader_without_cutoff_keeps_long_dwell():
    res = load_dwell_csv(FIX / "dwell_sample.csv", SIM, max_minutes=None, columns=COLS)
    assert res.dropped["over_max_duration"] == 0
    assert 1440.0 in [r.minutes for r in res.records]
    assert res.dropped["unknown_hospital"] == 0  # no id filter given


def test_dwell_loader_requires_provenance():
    with pytest.raises(ProvenanceError):
        load_dwell_csv(FIX / "dwell_sample.csv", None, max_minutes=None, columns=COLS)  # type: ignore[arg-type]
    # Tagged's validator raises ProvenanceError, which pydantic wraps in ValidationError (a ValueError)
    with pytest.raises(ValueError, match="as_of"):
        Tagged(provenance=Provenance.REAL, source="108 GPS export")  # REAL without as_of
    with pytest.raises(ProvenanceError):
        load_dwell_csv(FIX / "dwell_sample.csv", Tagged(provenance=Provenance.PLACEHOLDER, source="x"),
                       max_minutes=None, columns=COLS)


def test_dwell_loader_column_mismatch_fails():
    with pytest.raises(ValueError, match="missing"):
        load_dwell_csv(FIX / "dwell_sample.csv", SIM, max_minutes=None)  # default column names


# ---------------------------------------------------------------- rolling index

def _records(hid: str, start: datetime, n: int, minutes: float, step: timedelta) -> list[DwellRecord]:
    return [DwellRecord(hospital_id=hid, arrived=start + i * step,
                        departed=start + i * step + timedelta(minutes=minutes)) for i in range(n)]


def test_index_satisfies_protocol_and_reads_config():
    cfg = default_config()
    idx = RollingDwellIndex([], SIM, cfg)
    assert isinstance(idx, DwellIndex)
    assert idx.band_hours == cfg.v("passive.hour_band_hours")
    assert idx.min_samples == cfg.v("passive.min_dwell_samples")
    assert idx.window_days == cfg.v("passive.dwell_window_days")


def test_index_mean_band_window_and_min_samples():
    cfg = default_config()
    band, need, window = (cfg.v(f"passive.{k}") for k in ("hour_band_hours", "min_dwell_samples",
                                                            "dwell_window_days"))
    when = datetime(2026, 3, 1, 9, 30)
    base = when.replace(hour=(when.hour // band) * band, minute=0) - timedelta(days=1)
    recs = _records("fx-a", base, need, 20.0, timedelta(minutes=1))  # inside window and band
    recs += _records("fx-a", base - timedelta(days=window + 2), need, 90.0, timedelta(hours=0))  # too old
    other_band = base.replace(hour=(base.hour + band) % 24)
    recs += _records("fx-a", other_band, need, 60.0, timedelta(minutes=1))  # other hour band
    idx = RollingDwellIndex(recs, SIM, cfg)
    out = idx.mean_offload_min("fx-a", when)
    assert out is not None
    assert out.value == pytest.approx(20.0)
    assert out.provenance is Provenance.SIMULATED
    assert f"n={need}" in out.tag.source and "test fixture" in out.tag.source
    assert idx.mean_offload_min("fx-b", when) is None  # no data for that hospital


def test_index_returns_none_below_min_samples_and_ignores_future():
    cfg = default_config()
    need = cfg.v("passive.min_dwell_samples")
    when = datetime(2026, 3, 1, 9, 30)
    recs = _records("fx-a", when - timedelta(days=2), need - 1, 15.0, timedelta(minutes=5))
    assert RollingDwellIndex(recs, SIM, cfg).mean_offload_min("fx-a", when) is None
    # a dwell that has not ended by `when` must not count
    late = DwellRecord(hospital_id="fx-a", arrived=when - timedelta(minutes=10), departed=when + timedelta(hours=1))
    assert len(RollingDwellIndex([*recs, late], SIM, cfg).samples("fx-a", when)) == need - 1


def test_index_carries_real_tag_only_from_real_logs():
    cfg = default_config()
    need = cfg.v("passive.min_dwell_samples")
    when = datetime(2026, 3, 1, 9, 30)
    recs = _records("fx-a", when - timedelta(days=1), need, 25.0, timedelta(minutes=1))
    real = Tagged(provenance=Provenance.REAL, source="108 GPS export (test stand-in)", as_of=date(2026, 3, 1))
    out = RollingDwellIndex(recs, real, cfg).mean_offload_min("fx-a", when)
    assert out is not None and out.provenance is Provenance.REAL and out.tag.as_of == date(2026, 3, 1)


def test_index_rejects_null_config():
    raw = {
        "scoring": {"lambda_by_category": {"value": {}, "provenance": "PLACEHOLDER", "basis": "test"}},
        "passive": {
            "hour_band_hours": {"value": None, "provenance": "PLACEHOLDER", "basis": "test"},
            "min_dwell_samples": {"value": 5, "provenance": "PLACEHOLDER", "basis": "test"},
            "dwell_window_days": {"value": 28, "provenance": "PLACEHOLDER", "basis": "test"},
        },
    }
    with pytest.raises(ValueError, match="hour_band_hours"):
        RollingDwellIndex([], SIM, parse_config(raw))


# ---------------------------------------------------------------- outcomes

def test_outcome_store_roundtrip(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    store = OutcomeStore(path)
    t0 = datetime(2026, 3, 1, 9, 0)
    store.append(OutcomeEvent(hospital_id="fx-a", timestamp=t0 + timedelta(hours=1), accepted=False,
                              reason_code="cath_lab_busy", source=Provenance.SIMULATED))
    store.append(OutcomeEvent(hospital_id="fx-a", timestamp=t0, accepted=True, reason_code="ok",
                              source=Provenance.SIMULATED))
    store.append(OutcomeEvent(hospital_id="fx-b", timestamp=t0, accepted=True, reason_code="ok",
                              source=Provenance.SIMULATED))
    again = OutcomeStore(path)
    assert len(again) == 3
    evs = again.events("fx-a")
    assert [e.accepted for e in evs] == [True, False]  # time order
    assert len(again.events(since=t0 + timedelta(minutes=30))) == 1
    assert again.provenance_counts()["SIMULATED"] == 3


def test_outcome_store_requires_explicit_source(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    path.write_text('{"hospital_id": "fx-a", "timestamp": "2026-03-01T09:00:00", "accepted": true, '
                    '"reason_code": "ok"}\n')
    with pytest.raises(ProvenanceError, match="line 1"):
        OutcomeStore(path)
    path.write_text("{not json\n")
    with pytest.raises(ValueError, match="line 1"):
        OutcomeStore(path)


def test_outcomes_csv(tmp_path):
    p = tmp_path / "outcomes.csv"
    p.write_text("hospital_id,timestamp,accepted,reason_code,source\n"
                 "fx-a,2026-03-01T09:00:00,false,no_icu_bed,SIMULATED\n")
    evs = load_outcomes_csv(p)
    assert evs[0].accepted is False and evs[0].source is Provenance.SIMULATED
    p.write_text("hospital_id,timestamp,accepted,reason_code,source\nfx-a,2026-03-01T09:00:00,true,ok,\n")
    with pytest.raises(ProvenanceError, match="line 2"):
        load_outcomes_csv(p)
