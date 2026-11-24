"""Examples for ``ml_model/compare_reports.py`` (the F7a before/after comparison). No I/O except
``main()``, which writes only to ``tmp_path``."""
import pandas as pd
import pytest

from tests.conftest import load_module

compare = load_module("ml_model/compare_reports.py")

pytestmark = pytest.mark.unit


def _report(rows):
    columns = ["Product_ID", "subindustry", "product_type", "volume", "cost_per_unit", "predicted_price",
               "market_p75_found", "competitors_count"]
    return pd.DataFrame(rows, columns=columns)


OLD = _report([
    ("A", "Pets", "P1", "0.7 L", 10.0, 100.0, 80.0, 5),
    ("B", "Pets", "P1", "4.0 L", 10.0, 400.0, 300.0, 5),
    ("C", "Home", "H1", "1.0 L", 50.0, 70.0, 60.0, 5),
    ("D", "Home", "H1", "0 units", 5.0, 6.5, 0.0, 0),        # no size: left out
    ("E", "Home", "H2", "1.0 L", 1.0, 20.0, 10.0, 2),        # p75 from 2 rows: not a yardstick
    ("F", "Home", "H1", "1.0 L", 1.0, 70.0, 60.0, 5),        # above p75 with a low floor
    ("A", "Pets", "P1", "0.7 L", 10.0, 100.0, 80.0, 5),      # repeated SKU: matched by occurrence
])
NEW = OLD.assign(predicted_price=[80.0, 200.0, 70.0, 6.5, 22.0, 70.0, 100.0])


def test_summary_by_subindustry():
    table = compare.summarize(compare.join_reports(OLD, NEW)).set_index("subindustry")
    pets, home, total = table.loc["Pets"], table.loc["Home"], table.loc["All"]
    assert (pets["n"], pets["n_changed"], pets["median_ratio"]) == (3, 2, 0.8)
    assert (pets["n_with_p75"], pets["pct_old_above_p75"], pets["pct_new_above_p75"]) == (3, 100.0, 33.3)
    # Home C: 70 > p75 60 only because the floor (50 x 1.30 = 65) is above the p75; F is above
    # it with a floor of 1.30, so it counts as above but not as "because of the floor".
    assert (home["n"], home["n_with_p75"], home["pct_new_above_p75"], home["pct_new_above_p75_floor"]) == (3, 2, 100.0, 50.0)
    assert total["n"] == 6 and total["n_changed"] == 3


def test_missing_column_is_an_error(tmp_path):
    old = tmp_path / "old.csv"
    OLD.drop(columns="volume").to_csv(old, index=False)
    new = tmp_path / "new.csv"
    NEW.to_csv(new, index=False)
    assert compare.main(["--old", str(old), "--new", str(new), "--out-dir", str(tmp_path / "out")]) == 1


def test_main_writes_csv_and_markdown(tmp_path):
    old, new = tmp_path / "old.csv", tmp_path / "new.csv"
    OLD.to_csv(old, index=False)
    NEW.to_csv(new, index=False)
    out = tmp_path / "out"
    assert compare.main(["--old", str(old), "--new", str(new), "--out-dir", str(out),
                         "--by", "product_type", "--subindustry", "pets"]) == 0
    table = pd.read_csv(out / "comparison_by_product_type.csv")
    assert table["product_type"].tolist() == ["P1", "All"]
    assert (out / "comparison_by_product_type.md").read_text().startswith("| product_type |")
