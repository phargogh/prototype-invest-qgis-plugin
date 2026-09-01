"""Offline tests for the InVEST spec translation layer.

Every model spec from two InVEST releases is exercised without needing a QGIS
runtime, so a mapping regression shows up immediately.  Uses only the standard
library.

Run with:  python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from invest_qgis import normalize, outputs, paramspec  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
VERSIONS = ["3.16.2", "3.20.0"]

KNOWN_KINDS = {"folder_destination", "raster", "vector", "file", "folder",
               "boolean", "enum", "number", "integer", "string"}
KNOWN_OUTPUT_KINDS = {"raster", "vector", "table", "file"}

_CACHE = {}


def load_specs(version):
    if version not in _CACHE:
        path = os.path.join(HERE, f"specs_{version}.json")
        with open(path, encoding="utf-8") as handle:
            _CACHE[version] = json.load(handle)
    return _CACHE[version]


def iter_models():
    for version in VERSIONS:
        for model_id, raw in sorted(load_specs(version).items()):
            yield version, model_id, raw


class TestEveryModel(unittest.TestCase):
    """Sweeps of all 51 model specs across both InVEST releases."""

    def test_every_model_normalises(self):
        for version, model_id, raw in iter_models():
            with self.subTest(version=version, model=model_id):
                spec = normalize.normalise(raw)
                self.assertEqual(spec["model_id"], model_id)
                self.assertTrue(spec["model_title"])
                self.assertTrue(spec["inputs"], "a model needs inputs to run")

    def test_every_input_maps_to_a_known_parameter(self):
        for version, model_id, raw in iter_models():
            with self.subTest(version=version, model=model_id):
                plans = paramspec.plan_model(normalize.normalise(raw)["inputs"])
                names = [plan["name"] for plan in plans]
                self.assertEqual(len(names), len(set(names)),
                                 "duplicate parameter names")
                self.assertNotIn(paramspec.N_WORKERS, names,
                                 "n_workers must not be exposed")
                for plan in plans:
                    self.assertIn(plan["kind"], KNOWN_KINDS, plan["name"])
                    self.assertTrue(plan["description"], plan["name"])

    def test_exactly_one_workspace_parameter(self):
        for version, model_id, raw in iter_models():
            with self.subTest(version=version, model=model_id):
                plans = paramspec.plan_model(normalize.normalise(raw)["inputs"])
                workspaces = [p for p in plans if p["kind"] == "folder_destination"]
                self.assertEqual(len(workspaces), 1)
                self.assertFalse(workspaces[0]["optional"])

    def test_outputs_resolve_to_relative_paths(self):
        for version, model_id, raw in iter_models():
            with self.subTest(version=version, model=model_id):
                for record in normalize.normalise(raw)["outputs"]:
                    self.assertTrue(record["path"])
                    self.assertFalse(record["path"].startswith("/"))
                    self.assertIn(record["kind"], KNOWN_OUTPUT_KINDS)
                    self.assertNotEqual(record["path"].split("/")[0],
                                        normalize.TASKGRAPH_DIR,
                                        "taskgraph cache must be excluded")

    def test_numeric_bounds_are_consistent(self):
        for version, model_id, raw in iter_models():
            with self.subTest(version=version, model=model_id):
                for plan in paramspec.plan_model(normalize.normalise(raw)["inputs"]):
                    if plan["kind"] in ("number", "integer"):
                        low, high = plan.get("minimum"), plan.get("maximum")
                        if low is not None and high is not None:
                            self.assertLessEqual(low, high, plan["name"])


class TestConditionalInputs(unittest.TestCase):

    def test_conditionally_required_input_is_optional(self):
        """InVEST expresses some requirements as an expression over other
        inputs, which the Processing dialog cannot evaluate."""
        spec = normalize.normalise(load_specs("3.20.0")["carbon"])
        plans = {p["name"]: p for p in paramspec.plan_model(spec["inputs"])}
        self.assertTrue(plans["lulc_alt_path"]["optional"])
        self.assertIn("required if", plans["lulc_alt_path"]["description"])

    def test_dynamic_dropdown_falls_back_to_text(self):
        spec = normalize.normalise(load_specs("3.20.0")["coastal_vulnerability"])
        plans = {p["name"]: p for p in paramspec.plan_model(spec["inputs"])}
        # slr_field's choices are computed from another input at run time.
        self.assertEqual(plans["slr_field"]["kind"], "string")
        # A static dropdown in the same model still becomes an enum.
        self.assertEqual(plans["geomorphology_fill_value"]["kind"], "enum")
        self.assertEqual(plans["geomorphology_fill_value"]["option_keys"],
                         ["1", "2", "3", "4", "5"])


class TestGeometry(unittest.TestCase):

    def test_geometry_types_parse_from_set_repr(self):
        self.assertEqual(
            normalize.parse_geometry_types("{'POLYGON', 'MULTIPOLYGON'}"),
            {"POLYGON", "MULTIPOLYGON"})
        self.assertEqual(normalize.parse_geometry_types(["POINT"]), {"POINT"})
        self.assertEqual(normalize.parse_geometry_types(None), set())

    def test_geometry_tokens_collapse_to_any(self):
        self.assertEqual(paramspec.geometry_tokens({"POLYGON", "MULTIPOLYGON"}),
                         ["polygon"])
        self.assertEqual(
            paramspec.geometry_tokens({"POINT", "LINESTRING", "POLYGON"}), ["any"])
        self.assertEqual(paramspec.geometry_tokens(set()), ["any"])


class TestBounds(unittest.TestCase):

    CASES = [
        ("value >= -1", "number", (-1.0, None, False)),
        ("value > 0", "number", (0.0, None, False)),
        ("2012 <= value <= 2017", "number", (2012.0, 2017.0, False)),
        ("float(value).is_integer()", "number", (None, None, True)),
        ("value > 0 and value.is_integer()", "number", (0.0, None, True)),
        ("0 <= value <= 100", "number", (0.0, 100.0, False)),
        (None, "ratio", (0.0, 1.0, False)),
        (None, "percent", (0.0, 100.0, False)),
    ]

    def test_parse_bounds(self):
        for expression, input_type, expected in self.CASES:
            with self.subTest(expression=expression):
                self.assertEqual(
                    paramspec.parse_bounds(expression, input_type), expected)


class TestOutputPaths(unittest.TestCase):

    def test_suffix_string(self):
        for suffix, expected in [("", ""), (None, ""), ("   ", ""),
                                 ("run1", "_run1"), ("_run1", "_run1")]:
            with self.subTest(suffix=suffix):
                self.assertEqual(outputs.suffix_string(suffix), expected)

    def test_suffix_applies_to_filenames_not_directories(self):
        self.assertEqual(outputs.resolve_path("/ws", "c_storage.tif", "_r1"),
                         "/ws/c_storage_r1.tif")
        self.assertEqual(
            outputs.resolve_path("/ws", "intermediate_outputs/c_above.tif", "_r1"),
            "/ws/intermediate_outputs/c_above_r1.tif")
        self.assertEqual(outputs.resolve_path("/ws", "report.html", ""),
                         "/ws/report.html")


class TestCrossVersionLayouts(unittest.TestCase):
    """InVEST 3.16 nests outputs in directory entries; 3.20 uses explicit
    paths. Both must produce the same user-facing result."""

    def test_both_layouts_produce_the_same_carbon_outputs(self):
        for version in VERSIONS:
            with self.subTest(version=version):
                spec = normalize.normalise(load_specs(version)["carbon"])
                paths = {record["path"] for record in spec["outputs"]}
                self.assertIn("c_storage_bas.tif", paths)
                self.assertIn("intermediate_outputs/c_above_bas.tif", paths)
                self.assertFalse(
                    any(p.startswith(normalize.TASKGRAPH_DIR) for p in paths))

    def test_intermediate_flag_matches_directory_nesting(self):
        for version in VERSIONS:
            with self.subTest(version=version):
                spec = normalize.normalise(load_specs(version)["carbon"])
                by_path = {r["path"]: r for r in spec["outputs"]}
                self.assertFalse(by_path["c_storage_bas.tif"]["is_intermediate"])
                self.assertTrue(
                    by_path["intermediate_outputs/c_above_bas.tif"]["is_intermediate"])

    def test_carbon_rasters_classified_as_rasters(self):
        for version in VERSIONS:
            with self.subTest(version=version):
                spec = normalize.normalise(load_specs(version)["carbon"])
                by_path = {r["path"]: r for r in spec["outputs"]}
                self.assertEqual(by_path["c_storage_bas.tif"]["kind"], "raster")


class TestErrorHandling(unittest.TestCase):

    def test_unsupported_spec_raises(self):
        with self.assertRaises(normalize.UnsupportedSpec):
            normalize.normalise({"model_id": "bogus"})
        with self.assertRaises(normalize.UnsupportedSpec):
            normalize.normalise("not a dict")


if __name__ == "__main__":
    unittest.main()
