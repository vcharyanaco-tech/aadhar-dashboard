"""Tests for parsers.py.

These pin the rules that decide the numbers on the dashboard, especially the
`upd` derivation in parse_tx. That rule is easy to change by accident and
nothing else in the app would notice, because a wrong figure still renders as a
plausible number.

Run with:

    .venv/Scripts/python -m pytest tests/ -q
"""
import sqlite3

import pandas as pd
import pytest

import parsers


def tx_frame(**overrides):
    """A minimal daily transaction sheet with the canonical headers."""
    data = {
        "station_number": ["1001", "1002"],
        "machine_address": ["A", "B"],
        "machine_district": ["Hisar", "Karnal"],
        "Division": ["Hisar", "Karnal"],
        "Sub Division Name": ["Hisar Sadar", "Karnal City"],
        "Count_N": ["10", "4"],
        "Count_U_plus_N_plus_Z": ["30", "9"],
        "IS_MBU": ["5", "1"],
        "DEMO_UPDATE": ["7", "2"],
        "NON_MBU": ["8", "2"],
        "Session Operator ID": ["0012", "34"],
    }
    data.update(overrides)
    return pd.DataFrame(data)


def master_frame(**overrides):
    data = {
        "station_number": ["1001", "1002"],
        "Office Id": ["OFF1", "OFF2"],
        "Sub Division Name": ["Hisar Sadar", "Karnal City"],
        "Divison": ["Hisar", "Karnal"],   # note the source file's misspelling
        "machine_address": ["A", "B"],
        "Machine District": ["Hisar", "Karnal"],
    }
    data.update(overrides)
    # Pad any column the caller did not resize, so overriding one column with a
    # different row count cannot produce a ragged frame.
    width = max(len(v) for v in data.values())
    padded = {}
    for name, values in data.items():
        values = list(values)
        values += [values[-1]] * (width - len(values)) if values else []
        padded[name] = values
    return pd.DataFrame(padded)


# ---------------------------------------------------------------- norm_key
class TestNormKey:
    @pytest.mark.parametrize("raw,expected", [
        ("123", "123"),
        ("00123", "123"),
        ("123.0", "123"),
        ("0123.0", "123"),
        ("  123  ", "123"),
        ("000", "0"),
        ("", ""),
    ])
    def test_normalises_to_a_shared_join_key(self, raw, expected):
        assert parsers.norm_key(raw) == expected

    def test_none_is_treated_as_text_not_a_crash(self):
        """str(None) is "none" - matches the pre-existing behaviour, and station
        numbers are never null in a real sheet, so this only guards the crash."""
        assert parsers.norm_key(None) == "none"
        assert parsers.norm_key(float("nan")) == "nan"

    def test_numeric_and_padded_spellings_agree(self):
        """The master<->transaction join depends on these being identical."""
        assert parsers.norm_key(123) == parsers.norm_key("00123") == parsers.norm_key("123.0")


# ---------------------------------------------------------------- parse_tx
class TestParseTx:
    def test_columns_match_the_contract_exactly(self):
        out = parsers.parse_tx(tx_frame())
        assert list(out.columns) == parsers.TX_COLS

    def test_total_column_is_the_source_of_truth(self):
        """upd = total - new, not the sum of the component columns.

        The sheet says 30 total and 10 new, so updates must be 20 - even though
        IS_MBU + DEMO_UPDATE + NON_MBU would give 5 + 7 + 8 = 20 here. The test
        uses values where the two rules disagree.
        """
        df = tx_frame(**{"Count_N": ["10", "4"],
                         "Count_U_plus_N_plus_Z": ["30", "9"],
                         "IS_MBU": ["1", "1"],
                         "DEMO_UPDATE": ["1", "1"],
                         "NON_MBU": ["1", "1"]})
        out = parsers.parse_tx(df)
        assert out["upd"].tolist() == [20.0, 5.0]

    def test_falls_back_to_summing_components_without_a_total(self):
        df = tx_frame().drop(columns=["Count_U_plus_N_plus_Z"])
        out = parsers.parse_tx(df)
        assert out["upd"].tolist() == [20.0, 5.0]  # 5+7+8, 1+2+2

    def test_negative_updates_are_clipped_to_zero(self):
        """A total below the new-enrolment count must not produce a negative figure."""
        df = tx_frame(**{"Count_N": ["50", "0"], "Count_U_plus_N_plus_Z": ["10", "0"]})
        out = parsers.parse_tx(df)
        assert out["upd"].tolist() == [0.0, 0.0]

    def test_thousands_separators_are_understood(self):
        df = tx_frame(**{"Count_N": ["1,200", "0"], "Count_U_plus_N_plus_Z": ["3,400", "0"]})
        out = parsers.parse_tx(df)
        assert out["enr"].tolist() == [1200.0, 0.0]
        assert out["upd"].tolist() == [2200.0, 0.0]

    def test_blank_numeric_cells_become_zero(self):
        df = tx_frame(**{"Count_N": ["", "abc"], "Count_U_plus_N_plus_Z": ["", ""]})
        out = parsers.parse_tx(df)
        assert out["enr"].tolist() == [0.0, 0.0]
        assert out["upd"].tolist() == [0.0, 0.0]

    def test_operator_is_carried_through(self):
        out = parsers.parse_tx(tx_frame())
        assert out["operator"].tolist() == ["0012", "34"]

    def test_rows_without_a_usable_station_are_dropped(self):
        df = tx_frame(**{"station_number": ["1001", ""]})
        out = parsers.parse_tx(df)
        assert len(out) == 1

    def test_missing_station_column_names_the_problem(self):
        df = tx_frame().drop(columns=["station_number"])
        with pytest.raises(ValueError, match="station_number"):
            parsers.parse_tx(df)

    def test_missing_count_column_names_the_problem(self):
        df = tx_frame().drop(columns=["Count_N"])
        with pytest.raises(ValueError, match="Count_N"):
            parsers.parse_tx(df)

    def test_no_usable_metric_column_is_rejected(self):
        df = tx_frame().drop(columns=["Count_U_plus_N_plus_Z", "IS_MBU", "DEMO_UPDATE", "NON_MBU"])
        with pytest.raises(ValueError, match="Required columns"):
            parsers.parse_tx(df)

    def test_column_order_does_not_matter(self):
        df = tx_frame()
        out = parsers.parse_tx(df[list(reversed(df.columns))])
        assert out["upd"].tolist() == [20.0, 5.0]

    def test_non_mbu_is_not_mistaken_for_mbu(self):
        """'nonmbu' contains 'mbu', so IS_MBU must exclude it."""
        df = tx_frame().drop(columns=["Count_U_plus_N_plus_Z"])
        out = parsers.parse_tx(df)
        assert out["mbu"].tolist() == [5.0, 1.0]
        assert out["nonmbu"].tolist() == [8.0, 2.0]


# ---------------------------------------------------------------- parse_master
class TestParseMaster:
    def test_columns_match_the_contract(self):
        out = parsers.parse_master(master_frame())
        assert list(out.columns) == parsers.MASTER_COLS

    def test_accepts_the_misspelt_divison_header(self):
        out = parsers.parse_master(master_frame())
        assert out["division"].tolist() == ["Hisar", "Karnal"]

    def test_accepts_the_correctly_spelt_header(self):
        df = master_frame().rename(columns={"Divison": "Division"})
        out = parsers.parse_master(df)
        assert out["division"].tolist() == ["Hisar", "Karnal"]

    def test_duplicate_stations_are_collapsed(self):
        df = master_frame()
        dup = df.iloc[[0]].assign(Office_Id="OFF1-NEW")
        out = parsers.parse_master(pd.concat([df, dup], ignore_index=True))
        assert len(out) == 2

    def test_spellings_are_unified_to_the_most_common(self):
        df = master_frame(
            station_number=["1001", "1002", "1003", "1004"],
            **{"Office Id": ["O1", "O2", "O3", "O4"],
               "Sub Division Name": ["a", "b", "c", "d"],
               "Divison": ["HIsar", "HIsar", "Hisar", "Karnal"]})
        out = parsers.parse_master(df)
        hisar = out[out["division"].str.lower() == "hisar"]
        assert len(hisar) == 3
        # Grouping is case-insensitive, but the stored value is the most
        # frequently seen original spelling - "HIsar" wins 2 rows to "Hisar".
        assert set(hisar["division"]) == {"HIsar"}

    def test_station_keys_match_parse_tx_keys(self):
        """This is the join the whole report depends on."""
        m = parsers.parse_master(master_frame())
        t = parsers.parse_tx(tx_frame())
        assert set(m["key"]) == set(t["key"])

    def test_leading_zeros_survive_the_join(self):
        m = parsers.parse_master(master_frame(station_number=["001001", "001002"]))
        t = parsers.parse_tx(tx_frame(station_number=["1001", "1002"]))
        assert set(m["key"]) == set(t["key"]) == {"1001", "1002"}

    def test_missing_station_column_raises(self):
        with pytest.raises(ValueError, match="Station Number"):
            parsers.parse_master(master_frame().drop(columns=["station_number"]))


# ---------------------------------------------------------------- fuzzy hints
class TestFuzzyHints:
    def test_near_miss_header_gets_a_suggestion(self):
        hint = parsers.fuzzy_hint(["Divission Name", "station_number"], ["division"])
        assert hint == "Divission Name"

    def test_transposed_letters_are_still_suggested(self):
        # 'divsion' is a transposition of 'division' and is almost certainly a
        # typo worth pointing at.
        assert parsers.fuzzy_hint(["Divsion"], ["division"]) == "Divsion"

    def test_unrelated_headers_are_not_suggested(self):
        assert parsers.fuzzy_hint(["Session Operator ID", "machine_address"],
                                  ["division", "subdivision"]) is None

    def test_unrelated_header_gets_no_suggestion(self):
        assert parsers.fuzzy_hint(["alpha", "beta"], ["division"]) is None

    def test_short_overlap_does_not_trigger_a_suggestion(self):
        """Two shared letters out of a long name is noise, not a near miss."""
        assert parsers.fuzzy_hint(["ab"], ["abcdefgh"]) is None

    def test_parse_error_lists_the_headers_actually_present(self):
        df = master_frame().drop(columns=["station_number"])
        with pytest.raises(ValueError) as exc:
            parsers.parse_master(df)
        message = str(exc.value)
        assert "Headers in your file" in message
        assert "Office Id" in message

    def test_near_miss_office_header_is_suggested(self):
        df = master_frame().rename(columns={"Office Id": "Ofice Id"})
        with pytest.raises(ValueError) as exc:
            parsers.parse_master(df)
        assert "Ofice Id" in str(exc.value)


# ---------------------------------------------------------------- parse_operator_master
class TestParseOperatorMaster:
    def test_columns_match_the_contract(self):
        df = pd.DataFrame({"Session Operator ID": ["12", "34"], "Operator Name": ["Asha", "Bim"]})
        out = parsers.parse_operator_master(df)
        assert list(out.columns) == parsers.OPERATOR_COLS
        assert out["operator_name"].tolist() == ["Asha", "Bim"]

    def test_duplicate_operators_collapse(self):
        df = pd.DataFrame({"Operator ID": ["12", "12", "34"], "Name": ["Asha", "Asha", "Bim"]})
        out = parsers.parse_operator_master(df)
        assert len(out) == 2

    def test_requires_both_columns(self):
        with pytest.raises(ValueError, match="Operator ID and Operator Name"):
            parsers.parse_operator_master(pd.DataFrame({"only": ["x"]}))


# ---------------------------------------------------------------- dates
class TestDates:
    @pytest.mark.parametrize("name,expected", [
        ("22.09.2026.xlsx", "22-09-2026"),
        ("22-09-2026.xlsx", "22-09-2026"),
        ("2026-09-22.xlsx", "22-09-2026"),
        ("22092026.xlsx", "22-09-2026"),
        ("no-date-here.xlsx", None),
        ("32.13.2026.xlsx", None),   # invalid date
    ])
    def test_date_from_filename(self, name, expected):
        assert parsers.date_from_filename(name) == expected

    def test_label_date_round_trips(self):
        assert str(parsers.parse_label_date("22-09-2026")) == "2026-09-22"

    def test_unrecognised_label_is_none(self):
        assert parsers.parse_label_date("Week 3") is None

    def test_daily_target_matches_ignoring_case_and_spacing(self):
        assert parsers.daily_target_for("HIsar") == 640
        assert parsers.daily_target_for("  karnal ") == 880
        assert parsers.daily_target_for("Nowhere") is None


# ---------------------------------------------------------------- SQL safety
class TestQuoteIdent:
    @pytest.mark.parametrize("name", ["tx", "master", "idx_tx_key", "_x1"])
    def test_accepts_plain_identifiers(self, name):
        assert parsers.quote_ident(name) == f'"{name}"'

    @pytest.mark.parametrize("evil", [
        'tx; DROP TABLE users',
        'tx" ; --',
        "tx users",
        "",
        "1tx",
        None,
        123,
    ])
    def test_rejects_anything_that_is_not_a_bare_identifier(self, evil):
        with pytest.raises(ValueError):
            parsers.quote_ident(evil)


# ---------------------------------------------------------------- persistence
def fresh_conn():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE uploads(id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT,"
                " uploaded_by TEXT, uploaded_at TEXT)")
    return con


class TestSaveTx:
    def test_round_trips_through_sqlite(self):
        con = fresh_conn()
        frame = parsers.parse_tx(tx_frame())
        upload_id = parsers.save_tx(con, frame, "22-09-2026", "admin")
        rows = con.execute("SELECT key, enr, upd, operator FROM tx").fetchall()
        assert upload_id == 1
        assert len(rows) == 2
        assert rows[0]["key"] == "1001"
        assert rows[0]["upd"] == 20.0
        con.close()

    def test_appends_rather_than_replacing(self):
        con = fresh_conn()
        parsers.save_tx(con, parsers.parse_tx(tx_frame()), "22-09-2026", "admin")
        parsers.save_tx(con, parsers.parse_tx(tx_frame()), "23-09-2026", "admin")
        assert con.execute("SELECT COUNT(*) FROM tx").fetchone()[0] == 4
        assert con.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 2
        con.close()

    def test_rejects_a_frame_that_breaks_the_column_contract(self):
        con = fresh_conn()
        bad = parsers.parse_tx(tx_frame()).rename(columns={"upd": "updates"})
        with pytest.raises(ValueError, match="expected"):
            parsers.save_tx(con, bad, "22-09-2026", "admin")
        con.close()

    def test_rejects_a_missing_column(self):
        con = fresh_conn()
        bad = parsers.parse_tx(tx_frame()).drop(columns=["operator"])
        with pytest.raises(ValueError, match="expected"):
            parsers.save_tx(con, bad, "22-09-2026", "admin")
        con.close()


class TestIndexes:
    def test_indexes_are_created_on_the_tx_table(self):
        con = fresh_conn()
        parsers.save_tx(con, parsers.parse_tx(tx_frame()), "22-09-2026", "admin")
        parsers.ensure_indexes(con)
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_tx_upload_id" in names
        assert "idx_tx_key" in names
        con.close()

    def test_is_a_noop_when_tx_does_not_exist(self):
        con = fresh_conn()
        assert parsers.ensure_indexes(con) == []
        con.close()

    def test_query_plan_uses_the_index(self):
        """A full scan here is the whole point of #8, so assert on the plan."""
        con = fresh_conn()
        parsers.save_tx(con, parsers.parse_tx(tx_frame()), "22-09-2026", "admin")
        parsers.save_tx(con, parsers.parse_tx(tx_frame()), "23-09-2026", "admin")
        parsers.ensure_indexes(con)
        plan = " ".join(r[3] for r in con.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM tx WHERE upload_id IN (1)"))
        assert "idx_tx_upload_id" in plan
        con.close()


class TestSaveMaster:
    def test_replace_reports_orphaned_keys(self):
        con = fresh_conn()
        parsers.save_master(con, parsers.parse_master(master_frame()))
        # A replacement that drops station 1002.
        smaller = parsers.parse_master(master_frame(station_number=["1001"]))
        summary = parsers.save_master(con, smaller)
        assert summary["orphaned"] == 1
        con.close()

    def test_veto_hook_blocks_the_write(self):
        con = fresh_conn()
        parsers.save_master(con, parsers.parse_master(master_frame()))
        before = con.execute("SELECT COUNT(*) FROM master").fetchone()[0]
        smaller = parsers.parse_master(master_frame(station_number=["1001"]))

        def veto(summary):
            raise ValueError(f"would orphan {summary['orphaned_keys']}")

        with pytest.raises(ValueError, match="would orphan 1"):
            parsers.save_master(con, smaller, on_replace=veto)
        # The old master must still be intact.
        assert con.execute("SELECT COUNT(*) FROM master").fetchone()[0] == before
        con.close()

    def test_no_veto_needed_when_nothing_is_orphaned(self):
        con = fresh_conn()
        parsers.save_master(con, parsers.parse_master(master_frame()))
        called = []

        def veto(summary):
            called.append(summary)
            raise AssertionError("should not be called")

        parsers.save_master(con, parsers.parse_master(master_frame()), on_replace=veto)
        assert called == []
        con.close()

    def test_growing_the_master_is_not_an_orphan(self):
        con = fresh_conn()
        parsers.save_master(con, parsers.parse_master(master_frame()))
        bigger = parsers.parse_master(master_frame(
            station_number=["1001", "1002", "1003"],
            **{"Office Id": ["O1", "O2", "O3"],
               "Sub Division Name": ["a", "b", "c"],
               "Divison": ["Hisar", "Karnal", "Rohtak"]}))
        assert parsers.save_master(con, bigger)["orphaned"] == 0
        con.close()


class TestOperatorNames:
    def test_tie_break_is_deterministic_regardless_of_insert_order(self):
        """Two spellings of one id must always resolve the same way."""
        forward = fresh_conn()
        parsers.save_operator_master(forward, pd.DataFrame(
            {"op_key": ["12", "12"], "operator_id": ["012", "12"], "operator_name": ["Zed", "Asha"]}))
        reverse = fresh_conn()
        parsers.save_operator_master(reverse, pd.DataFrame(
            {"op_key": ["12", "12"], "operator_id": ["12", "012"], "operator_name": ["Asha", "Zed"]}))
        assert parsers.operator_names(forward).loc["12"] == parsers.operator_names(reverse).loc["12"]
        forward.close()
        reverse.close()

    def test_empty_series_when_the_table_is_absent(self):
        con = fresh_conn()
        assert parsers.operator_names(con).empty
        con.close()

    def test_maps_keys_to_names(self):
        con = fresh_conn()
        parsers.save_operator_master(con, pd.DataFrame(
            {"op_key": ["12", "34"], "operator_id": ["12", "34"], "operator_name": ["Asha", "Bim"]}))
        names = parsers.operator_names(con)
        assert names.loc["12"] == "Asha"
        assert names.loc["34"] == "Bim"
        con.close()


class TestEnsureCols:
    def test_adds_missing_columns(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE t(a TEXT)")
        added = parsers.ensure_cols(con, "t", {"b": "REAL"})
        assert added == ["b"]
        cols = {r[1] for r in con.execute("PRAGMA table_info('t')")}
        assert cols == {"a", "b"}
        con.close()

    def test_is_a_noop_for_a_table_that_does_not_exist(self):
        con = sqlite3.connect(":memory:")
        assert parsers.ensure_cols(con, "nope", {"b": "REAL"}) == []
        con.close()

    def test_rejects_an_injected_table_name(self):
        con = sqlite3.connect(":memory:")
        with pytest.raises(ValueError):
            parsers.ensure_cols(con, "t; DROP TABLE users", {"b": "REAL"})
        con.close()
