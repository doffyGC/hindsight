from pathlib import Path

import pytest

from hindsight.mining.interface import gold_test_sources, required_new_names

PARENT_TEST = """from pkg import exp


class TestSpark(Validator):
    def test_old(self):
        self.validate_all("SELECT 1")
"""

GOLD_TEST = """from pkg import exp


class TestSpark(Validator):
    def test_old(self):
        self.validate_all("SELECT 1")

    def test_options(self):
        sql = "CREATE TABLE t USING CSV OPTIONS (maxColumns = 10)"
        expression = self.validate_identity(sql)
        self.assertIsInstance(expression, exp.Create)
        with self.subTest(key="x"):
            options = expression.find(exp.OptionsProperty)
            self.assertIsNotNone(options)

    def test_after(self):
        pass
"""


@pytest.fixture
def task_dir(tmp_path: Path) -> Path:
    for sub in ("work/pkg", "work/tests", "gold/tests"):
        (tmp_path / sub).mkdir(parents=True)
    (tmp_path / "work/pkg/exp.py").write_text("class Create: ...\nclass Property: ...\n")
    (tmp_path / "work/tests/helpers.py").write_text("def validate_identity(sql): ...\n")
    (tmp_path / "work/tests/test_spark.py").write_text(PARENT_TEST)
    (tmp_path / "gold/tests/test_spark.py").write_text(GOLD_TEST)
    return tmp_path


def test_reports_only_names_the_parent_lacks(task_dir: Path) -> None:
    # not reported: strings (maxColumns), framework API (assertIsInstance, subTest),
    # names the parent already has (Create, validate_identity in a test helper)
    assert required_new_names(task_dir, ["tests/test_spark.py"], ["tests"]) == ["OptionsProperty"]


def test_new_test_files_count_entirely_as_added(task_dir: Path) -> None:
    (task_dir / "gold/tests/test_new.py").write_text("def test_x():\n    run_new_thing()\n")
    assert required_new_names(task_dir, ["tests/test_new.py"], ["tests"]) == ["run_new_thing"]


def test_gold_test_sources_extracts_each_f2p_function(task_dir: Path) -> None:
    text = gold_test_sources(
        task_dir,
        ["tests.test_spark.TestSpark::test_options", "tests.test_spark.TestSpark::test_gone"],
        ["tests/test_spark.py"],
    )
    assert text.startswith("# tests/test_spark.py\n    def test_options(self):")
    assert "exp.OptionsProperty" in text
    assert "def test_after" not in text
    assert "test_gone: source not found" in text


def test_parametrized_ids_are_matched_by_function_name(task_dir: Path) -> None:
    text = gold_test_sources(
        task_dir, ["tests/test_spark.py::test_options[x-1]"], ["tests/test_spark.py"]
    )
    assert "def test_options(self):" in text
