"""Tests for the report aggregation in parsers.aggregate_period and target_table.

This is the code that produces every figure on the dashboard and in the reports.
It was untested while it lived in app.py, because app.py runs its whole auth
bootstrap at import and cannot be imported outside a Streamlit runtime. Moving it
into parsers.py is what made these tests possible.

Run with:

    .venv/Scripts/python -m pytest tests/test_aggregation.py -q
"""
import sqlite3

import pandas as pd
import pytest

import parsers


@pytest.fixture()
def db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE master(key TEXT, station TEXT, office_id TEXT, division TEXT,"
                " sub_division TEXT, address TEXT, district TEXT)")
    con.execute("CREATE TABLE operator_master(op_key TEXT, operator_id TEXT, operator_name TEXT)")
    con.execute("CREATE TABLE tx(station TEXT, address TEXT, district TEXT, t_div TEXT,"
                " t_sub TEXT, operator TEXT, enr REAL, mbu REAL, demo REAL, nonmbu REAL,"
                " upd REAL, key TEXT, upload_id INTEGER)")
    return con


def station(key, division="", sub_division="", office_id="O", address="a", district="d"):
    return (key, key, office_id, division, sub_division, address, district)


def row(key, upload_id, enr, upd, operator="", t_div="", t_sub=""):
    return (key, key, upload_id, float(enr), float(upd), operator, t_div, t_sub)


def seed(con, master_rows=(), tx_rows=(), operators=()):
    for r in master_rows:
        con.execute("INSERT INTO master VALUES (?,?,?,?,?,?,?)", r)
    for key, st, upload_id, enr, upd, operator, t_div, t_sub in tx_rows:
        con.execute("INSERT INTO tx(station, address, district, t_div, t_sub, operator,"
                    " enr, mbu, demo, nonmbu, upd, key, upload_id)"
                    " VALUES (?,'','',?,?,?,?,0,0,0,?,?,?)",
                    (st, t_div, t_sub, operator, enr, upd, key, upload_id))
    for oid, name in operators:
        con.execute("INSERT INTO operator_master VALUES (?,?,?)", (oid, oid, name))
    con.commit()


# ---------------------------------------------------------------- station totals
class TestStationTotals:
    def test_sums_across_the_selected_uploads(self, db):
        seed(db,
             master_rows=[station("1001", "Hisar", "HS")],
             tx_rows=[row("1001", 1, 10, 20, "12"), row("1001", 2, 5, 7, "12")])
        agg, missing, m, ops = parsers.aggregate_period(db, (1, 2))
        r = agg.iloc[0]
        assert r["enr"] == 15
        assert r["upd"] == 27
        assert r["total"] == 42
        assert r["days_reported"] == 2
        assert r["machines"] == 2
        assert r["division"] == "Hisar"

    def test_excludes_uploads_not_selected(self, db):
        seed(db,
             master_rows=[station("1001", "Hisar", "HS")],
             tx_rows=[row("1001", 1, 10, 20, "12"), row("1001", 2, 999, 999, "12")])
        agg, _, _, _ = parsers.aggregate_period(db, (1,))
        assert agg.iloc[0]["enr"] == 10
        assert agg.iloc[0]["upd"] == 20

    def test_empty_selection_returns_empty_frames(self, db):
        seed(db, master_rows=[station("1001", "Hisar", "HS")],
             tx_rows=[row("1001", 1, 10, 20, "12")])
        agg, missing, m, ops = parsers.aggregate_period(db, ())
        assert agg.empty and ops.empty
        assert len(m) == 1

    def test_master_station_with_no_data_is_reported_as_missing(self, db):
        seed(db,
             master_rows=[station("1001", "Hisar", "HS"), station("1002", "Karnal", "KC")],
             tx_rows=[row("1001", 1, 10, 20, "12")])
        _, missing, _, _ = parsers.aggregate_period(db, (1,))
        assert list(missing["key"]) == ["1002"]

    def test_division_falls_back_to_the_daily_file(self, db):
        seed(db,
             master_rows=[station("1001", "", "")],
             tx_rows=[row("1001", 1, 10, 20, "12", t_div="Hisar", t_sub="HS")])
        agg, _, _, _ = parsers.aggregate_period(db, (1,))
        assert agg.iloc[0]["division"] == "Hisar"

    def test_station_absent_from_master_is_labelled(self, db):
        seed(db,
             master_rows=[station("1001", "Hisar", "HS")],
             tx_rows=[row("9999", 1, 10, 20, "12")])
        agg, _, _, _ = parsers.aggregate_period(db, (1,))
        assert agg.iloc[0]["division"] == "Not in master"
        assert agg.iloc[0]["sub_division"] == "Not mapped"

    def test_master_division_wins_over_the_daily_file(self, db):
        seed(db,
             master_rows=[station("1001", "Hisar", "HS")],
             tx_rows=[row("1001", 1, 10, 20, "12", t_div="Karnal", t_sub="KC")])
        agg, _, _, _ = parsers.aggregate_period(db, (1,))
        assert agg.iloc[0]["division"] == "Hisar"


# ---------------------------------------------------------------- operator totals
class TestOperatorTotals:
    def test_maps_an_operator_to_the_division_it_reports_from(self, db):
        seed(db,
             master_rows=[station("1001", "Hisar", "HS"), station("1002", "Karnal", "KC")],
             tx_rows=[row("1001", 1, 10, 20, "12"), row("1002", 1, 4, 5, "12")],
             operators=[("12", "Asha")])
        _, _, _, ops = parsers.aggregate_period(db, (1,))
        assert len(ops) == 1
        assert ops.iloc[0]["total"] == 39
        assert ops.iloc[0]["stations"] == 2
        assert ops.iloc[0]["operator_name"] == "Asha"

    def test_operator_name_is_blank_when_not_in_the_master(self, db):
        seed(db, master_rows=[station("1001", "Hisar", "HS")],
             tx_rows=[row("1001", 1, 10, 20, "77")])
        _, _, _, ops = parsers.aggregate_period(db, (1,))
        assert ops.iloc[0]["operator_name"] == ""

    def test_rows_with_no_operator_are_dropped_from_operator_totals_only(self, db):
        seed(db, master_rows=[station("1001", "Hisar", "HS")],
             tx_rows=[row("1001", 1, 10, 20, ""), row("1001", 1, 1, 1, "   ")])
        agg, _, _, ops = parsers.aggregate_period(db, (1,))
        assert len(agg) == 1
        assert ops.empty

    def test_most_frequent_division_wins(self, db):
        seed(db,
             master_rows=[station("1001", "Hisar", "HS"), station("1002", "Karnal", "KC")],
             tx_rows=[row("1001", 1, 1, 1, "12"), row("1001", 2, 1, 1, "12"),
                      row("1002", 1, 1, 1, "12")])
        _, _, _, ops = parsers.aggregate_period(db, (1, 2))
        assert ops.iloc[0]["division"] == "Hisar"
        assert ops.iloc[0]["days_worked"] == 2

    def test_operator_ids_are_matched_through_norm_key(self, db):
        """'0012' in the daily sheet must match operator 12 in the master."""
        seed(db, master_rows=[station("1001", "Hisar", "HS")],
             tx_rows=[row("1001", 1, 10, 20, "0012")], operators=[("12", "Asha")])
        _, _, _, ops = parsers.aggregate_period(db, (1,))
        assert ops.iloc[0]["operator_name"] == "Asha"


# ---------------------------------------------------------------- target table
class TestTargetTable:
    LOOKUP = {"hisar": 640, "karnal": 880}

    def frame(self):
        return pd.DataFrame({"Division": ["Hisar", "Karnal", "Nowhere"],
                             "Total": [3302, 3438, 100]})

    def test_uses_working_days_not_upload_count(self):
        out, _ = parsers.target_table(self.frame(), 6, self.LOOKUP)
        hisar = out[out["Division"] == "Hisar"].iloc[0]
        assert hisar["Target"] == 640 * 6
        assert hisar["% Achieved"] == round(3302 / (640 * 6) * 100, 1)

    def test_matches_divisions_ignoring_case(self):
        out, _ = parsers.target_table(self.frame(), 6, self.LOOKUP)
        assert set(out["Division"]) == {"Hisar", "Karnal"}

    def test_reports_divisions_with_no_configured_target(self):
        out, no_target = parsers.target_table(self.frame(), 6, self.LOOKUP)
        assert no_target == ["Nowhere"]
        assert "Nowhere" not in list(out["Division"])

    def test_zero_working_days_yields_no_figures(self):
        """Divisions are still named, but nothing is measured: a target of zero
        would make the percentage meaningless, so it is left undefined rather
        than divided by."""
        out, _ = parsers.target_table(self.frame(), 0, self.LOOKUP)
        assert out["Target"].isna().all()
        assert out["% Achieved"].isna().all()
        assert set(out["Division"]) == {"Hisar", "Karnal"}

    def test_shortfall_is_achievement_minus_target(self):
        out, _ = parsers.target_table(self.frame(), 6, self.LOOKUP)
        r = out[out["Division"] == "Hisar"].iloc[0]
        assert r["Shortfall / Surplus"] == r["Achievement"] - r["Target"]

    def test_regression_against_the_real_september_figures(self):
        """Hisar over 18-24 Sept 2026: 7 uploads but 6 working days."""
        out, _ = parsers.target_table(
            pd.DataFrame({"Division": ["Hisar"], "Total": [3302]}), 6, self.LOOKUP)
        assert out.iloc[0]["% Achieved"] == 86.0
        assert round(3302 / (640 * 7) * 100, 1) == 73.7   # the old, wrong figure
