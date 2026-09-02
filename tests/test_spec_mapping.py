"""Offline tests for the InVEST spec translation layer.

Every model spec from two InVEST releases is exercised without needing a QGIS
runtime, so a mapping regression shows up immediately.  Uses only the standard
library.

Run with:  python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from invest_qgis import datastack, normalize, outputs, paramspec  # noqa: E402
from invest_qgis import harvest, locator, server  # noqa: E402

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
                    self.assertNotIn(record["path"].split("/")[0],
                                     normalize.TASKGRAPH_DIRS,
                                     "taskgraph directories must be excluded")

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
                self.assertFalse(any(p.split("/")[0] in normalize.TASKGRAPH_DIRS
                                     for p in paths))

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


class TestIntermediateClassification(unittest.TestCase):
    """A results subdirectory must not be mistaken for a working directory.

    InVEST groups outputs one directory deep, but "output/" holds results while
    "intermediate/" holds working files. Treating every subdirectory as
    intermediate hid the entire result set of nine models.
    """

    def test_results_directories_are_not_intermediate(self):
        for path in ["output/foo.tif", "outputs/foo.tif",
                     "visualization_outputs/foo.shp",
                     "outputs_preprocessor/foo.csv"]:
            with self.subTest(path=path):
                self.assertFalse(normalize.is_intermediate_path(path))

    def test_working_directories_are_intermediate(self):
        for path in ["intermediate/foo.tif", "intermediate_outputs/foo.tif",
                     "intermediate_output/foo.tif", "intermediate_files/foo.tif",
                     "tmp/foo.tif"]:
            with self.subTest(path=path):
                self.assertTrue(normalize.is_intermediate_path(path))

    def test_bare_filenames_are_not_intermediate(self):
        self.assertFalse(normalize.is_intermediate_path("sed_export.tif"))

    def test_every_model_has_a_loadable_result_by_default(self):
        """With intermediates off, every model must still put something on the
        map, otherwise the plugin looks broken for that model."""
        for version, model_id, raw in iter_models():
            with self.subTest(version=version, model=model_id):
                spec = normalize.normalise(raw)
                loadable = [record for record in spec["outputs"]
                            if not record["is_intermediate"]
                            and record["kind"] in ("raster", "vector")]
                self.assertTrue(
                    loadable,
                    f"{model_id} would add nothing to the map by default")

    def test_taskgraph_directories_are_dropped(self):
        for version, model_id, raw in iter_models():
            with self.subTest(version=version, model=model_id):
                for record in normalize.normalise(raw)["outputs"]:
                    self.assertNotIn(record["path"].split("/")[0],
                                     normalize.TASKGRAPH_DIRS)


class TestDatastack(unittest.TestCase):
    """Loading InVEST parameter-set files onto a model's parameters."""

    SAMPLE_DATA = "/Users/jdouglass/Documents/InVEST 3.13.0 sample data"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write(self, payload, name="params.invest.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def _plans(self, model_id):
        return paramspec.plan_model(
            normalize.normalise(load_specs("3.20.0")[model_id])["inputs"])

    # -- reading ------------------------------------------------------------

    def test_reads_modern_and_legacy_and_bare_forms(self):
        modern = self._write({"model_id": "carbon", "invest_version": "3.20.0",
                              "args": {"results_suffix": "a"}})
        self.assertEqual(datastack.read(modern),
                         ("carbon", {"results_suffix": "a"}, "3.20.0"))

        # Pre-3.14 files carry a Python module path instead of a model id.
        legacy = self._write({"model_name": "natcap.invest.carbon",
                              "args": {"results_suffix": "b"}}, "legacy.json")
        model_id, args, _ = datastack.read(legacy)
        self.assertEqual(model_id, "carbon")
        self.assertEqual(args, {"results_suffix": "b"})

        bare = self._write({"results_suffix": "c"}, "bare.json")
        self.assertEqual(datastack.read(bare)[1], {"results_suffix": "c"})

    def test_unreadable_files_raise_datastack_error(self):
        broken = os.path.join(self.tmp, "broken.json")
        with open(broken, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(datastack.DatastackError):
            datastack.read(broken)
        with self.assertRaises(datastack.DatastackError):
            datastack.read(os.path.join(self.tmp, "absent.json"))
        with self.assertRaises(datastack.DatastackError):
            datastack.read(self._write({"model_id": "carbon"}, "noargs.json"))

    # -- value translation --------------------------------------------------

    def test_relative_paths_resolve_against_the_datastack(self):
        raster = os.path.join(self.tmp, "lulc.tif")
        open(raster, "w").close()
        result = datastack.to_parameter_values(
            {"lulc_bas_path": "lulc.tif"}, self._plans("carbon"), self.tmp)
        self.assertEqual(result["values"]["lulc_bas_path"], raster)

    def test_unresolvable_relative_path_is_kept_verbatim(self):
        """The user should see what the file asked for, not a rewritten guess."""
        result = datastack.to_parameter_values(
            {"lulc_bas_path": "missing.tif"}, self._plans("carbon"), self.tmp)
        self.assertEqual(result["values"]["lulc_bas_path"], "missing.tif")

    def test_quoted_numbers_are_coerced(self):
        plans = self._plans("sdr")
        result = datastack.to_parameter_values(
            {"sdr_max": "0.8", "threshold_flow_accumulation": "1000"},
            plans, self.tmp)
        self.assertEqual(result["values"]["sdr_max"], 0.8)
        self.assertEqual(result["values"]["threshold_flow_accumulation"], 1000.0)

    def test_boolean_strings_are_coerced(self):
        plans = self._plans("carbon")
        for raw, expected in [("true", True), ("False", False), (True, True),
                              ("yes", True), ("0", False)]:
            with self.subTest(raw=raw):
                result = datastack.to_parameter_values(
                    {"do_valuation": raw}, plans, self.tmp)
                self.assertEqual(result["values"]["do_valuation"], expected)

    def test_nonsense_values_are_reported_not_applied(self):
        plans = self._plans("carbon")
        result = datastack.to_parameter_values(
            {"do_valuation": "banana", "discount_rate": "not-a-number"},
            plans, self.tmp)
        self.assertEqual(result["values"], {})
        self.assertEqual(len(result["problems"]), 2)

    def test_enum_matches_case_insensitively(self):
        plans = self._plans("sdr")
        result = datastack.to_parameter_values(
            {"flow_dir_algorithm": "mfd"}, plans, self.tmp)
        self.assertEqual(result["values"]["flow_dir_algorithm"], "MFD")

        bad = datastack.to_parameter_values(
            {"flow_dir_algorithm": "D9"}, plans, self.tmp)
        self.assertNotIn("flow_dir_algorithm", bad["values"])
        self.assertTrue(bad["problems"])

    def test_workspace_is_never_restored_from_a_datastack(self):
        """A datastack records the workspace it was authored with, which would
        point at another machine or clobber a previous run's results."""
        result = datastack.to_parameter_values(
            {"workspace_dir": "/somewhere/else", "sdr_max": "0.8"},
            self._plans("sdr"), self.tmp)
        self.assertNotIn("workspace_dir", result["values"])
        self.assertEqual(result["skipped"], ["workspace_dir"])
        # ...and it is not misreported as an argument the model lacks.
        self.assertNotIn("workspace_dir", result["unknown"])

    def test_blank_unknown_and_ignored_keys_are_classified(self):
        result = datastack.to_parameter_values(
            {"drainage_path": "", "not_a_real_arg": "x", "n_workers": 4,
             "sdr_max": "0.8"},
            self._plans("sdr"), self.tmp)
        self.assertEqual(result["empty"], ["drainage_path"])
        self.assertEqual(result["unknown"], ["not_a_real_arg"])
        # n_workers is managed by the plugin and must never be loaded.
        self.assertNotIn("n_workers", result["values"])
        self.assertIn("sdr_max", result["values"])

    # -- against the real sample datastacks ---------------------------------

    @unittest.skipUnless(os.path.isdir(SAMPLE_DATA), "InVEST sample data absent")
    def test_real_sdr_datastack_maps_completely(self):
        path = os.path.join(self.SAMPLE_DATA, "SDR", "sdr_gura.invs.json")
        result = datastack.load_for_plans(path, self._plans("sdr"))
        self.assertEqual(result["unknown"], [])
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["empty"], ["drainage_path"])
        self.assertTrue(os.path.isabs(result["values"]["dem_path"]))
        self.assertTrue(os.path.exists(result["values"]["dem_path"]))
        self.assertIsInstance(result["values"]["sdr_max"], float)

    @unittest.skipUnless(os.path.isdir(SAMPLE_DATA), "InVEST sample data absent")
    def test_renamed_legacy_args_are_reported_not_silently_dropped(self):
        """The 3.7-era Carbon datastack uses lulc_cur_path etc., which today's
        spec calls lulc_bas_path. The user must be told."""
        path = os.path.join(self.SAMPLE_DATA, "Carbon", "carbon_willamette.invs.json")
        result = datastack.load_for_plans(path, self._plans("carbon"))
        self.assertIn("lulc_cur_path", result["unknown"])
        self.assertIn("carbon_pools_path", result["values"])


class TestValidationMessages(unittest.TestCase):
    """Validation warnings must name inputs the way the dialog labels them."""

    def _plans(self, model_id):
        return paramspec.plan_model(
            normalize.normalise(load_specs("3.20.0")[model_id])["inputs"])

    def test_keys_are_translated_to_dialog_labels(self):
        plans = self._plans("carbon")
        text = paramspec.format_validation_warnings(
            [[["lulc_bas_path"], "File not found"]], plans)
        self.assertIn("Baseline LULC", text)
        self.assertIn("File not found", text)
        # The raw argument id is not what the user sees in the form.
        self.assertNotIn("lulc_bas_path", text)

    def test_conditional_annotation_is_stripped_from_labels(self):
        """'Alternate LULC (required if: calc_sequestration)' is a useful form
        label but noise inside an error message."""
        plans = self._plans("carbon")
        label = paramspec.label_for(plans, "lulc_alt_path")
        self.assertNotIn("required if", label)
        self.assertTrue(label)

    def test_multiple_keys_are_all_named(self):
        plans = self._plans("carbon")
        text = paramspec.format_validation_warnings(
            [[["carbon_pools_path", "workspace_dir"], "Input is required"]], plans)
        self.assertIn("Carbon pools", text)
        self.assertIn("Workspace", text)

    def test_unknown_key_falls_back_to_the_raw_id(self):
        self.assertEqual(paramspec.label_for([], "mystery_arg"), "mystery_arg")

    def test_malformed_warning_entries_do_not_crash(self):
        text = paramspec.format_validation_warnings(
            ["just a string"], self._plans("carbon"))
        self.assertIn("just a string", text)


class TestServerClient(unittest.TestCase):
    """Behaviour of the warm-server client that does not need InVEST."""

    def test_reports_not_ready_before_starting(self):
        invest = server.InvestServer("/nonexistent/invest")
        self.assertFalse(invest.is_alive())
        self.assertFalse(invest.is_ready())

    def test_failure_to_launch_raises_with_a_reason(self):
        invest = server.InvestServer("/nonexistent/invest")
        with self.assertRaises(server.ServerError):
            invest.ensure_running(timeout=5)
        self.assertTrue(invest.last_error)

    def test_stop_is_safe_when_never_started(self):
        server.InvestServer("/nonexistent/invest").stop()

    def test_shared_instance_is_reused_and_swapped_by_path(self):
        first = server.get("/path/one/invest")
        self.assertIs(server.get("/path/one/invest"), first)
        second = server.get("/path/two/invest")
        self.assertIsNot(second, first)
        self.assertEqual(second.binary_path, "/path/two/invest")
        server.shutdown()


class TestLiveFeedbackLogic(unittest.TestCase):
    """The pure decision logic behind live validation, without a QGIS dialog.

    Mirrors what InvestAlgorithmDialog._apply_validation_marks decides, so the
    rules are pinned even though the widget plumbing needs a GUI to exercise.
    """

    @staticmethod
    def _marks(warnings, enabled):
        messages = {}
        for entry in warnings or []:
            try:
                keys, message = entry
            except (TypeError, ValueError):
                continue
            for key in keys:
                if enabled.get(key, True):
                    messages.setdefault(key, message)
        return messages

    def test_problems_are_reported_per_input(self):
        marks = self._marks(
            [[["a", "b"], "Input is required but has no value"],
             [["c"], "File not found"]], {})
        self.assertEqual(set(marks), {"a", "b", "c"})
        self.assertEqual(marks["b"], "Input is required but has no value")

    def test_inapplicable_inputs_are_not_flagged(self):
        """An input InVEST has switched off is not something the user can fix,
        so complaining about it would be noise."""
        marks = self._marks([[["lulc_alt_path"], "Input is required"]],
                            {"lulc_alt_path": False})
        self.assertEqual(marks, {})

    def test_applicable_inputs_are_still_flagged(self):
        marks = self._marks([[["lulc_alt_path"], "Input is required"]],
                            {"lulc_alt_path": True})
        self.assertIn("lulc_alt_path", marks)

    def test_first_message_wins_for_a_repeated_key(self):
        marks = self._marks(
            [[["a"], "first"], [["a"], "second"]], {"a": True})
        self.assertEqual(marks["a"], "first")

    def test_malformed_entries_are_ignored(self):
        self.assertEqual(self._marks(["nonsense", None], {}), {})

    def test_no_warnings_means_no_marks(self):
        self.assertEqual(self._marks([], {"a": True}), {})
        self.assertEqual(self._marks(None, {}), {})


class TestServerReadinessGate(unittest.TestCase):
    """A cold server must gate live checking without stranding the dialog.

    Every live check during startup is a silent no-op, so something has to
    notice when the server becomes usable; otherwise a form filled in during
    that first minute stays unchecked until the user presses Validate.
    """

    class _FakeServer:
        def __init__(self, ready_after):
            self.calls = 0
            self._ready_after = ready_after

        def is_ready(self):
            self.calls += 1
            return self.calls > self._ready_after

    def test_polling_stops_once_the_server_answers(self):
        fake = self._FakeServer(ready_after=3)
        polls = 0
        while not fake.is_ready() and polls < 10:
            polls += 1
        self.assertEqual(polls, 3)
        self.assertTrue(fake.is_ready())

    def test_a_cold_server_reports_not_ready(self):
        invest = server.InvestServer("/nonexistent/invest")
        self.assertFalse(invest.is_ready())


class TestInstallationDetection(unittest.TestCase):
    """Finding InVEST installations without launching anything."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _make_install(self, name, version, executable=True):
        """Build a fake macOS-style Workbench bundle."""
        app = os.path.join(self.tmp, name)
        internal = os.path.join(app, "Contents", "Resources", "invest")
        os.makedirs(os.path.join(internal, "_internal",
                                 f"natcap_invest-{version}.dist-info"))
        binary = os.path.join(internal, "invest")
        with open(binary, "w") as handle:
            handle.write("#!/bin/sh\n")
        if executable:
            os.chmod(binary, 0o755)
        return app

    def test_finds_installs_and_reads_their_versions(self):
        self._make_install("InVEST 3.20.0 Workbench.app", "3.20.0")
        found = locator.detect_installations(
            search_globs=[os.path.join(self.tmp, "InVEST*.app")])
        self.assertEqual(len(found), 1)
        app_path, binary_path, version = found[0]
        self.assertEqual(version, "3.20.0")
        self.assertTrue(os.path.exists(binary_path))

    def test_newest_release_comes_first(self):
        for name, version in [("InVEST 3.9.0 Workbench.app", "3.9.0"),
                              ("InVEST 3.20.0 Workbench.app", "3.20.0"),
                              ("InVEST 3.16.2 Workbench.app", "3.16.2")]:
            self._make_install(name, version)
        found = locator.detect_installations(
            search_globs=[os.path.join(self.tmp, "InVEST*.app")])
        self.assertEqual([version for _a, _b, version in found],
                         ["3.20.0", "3.16.2", "3.9.0"])

    def test_prerelease_sorts_below_the_matching_release(self):
        self._make_install("InVEST 3.20.0 Workbench.app", "3.20.0")
        self._make_install("InVEST dev Workbench.app", "3.20.0a1.dev3+gabc")
        found = locator.detect_installations(
            search_globs=[os.path.join(self.tmp, "InVEST*.app")])
        self.assertEqual(found[0][2], "3.20.0")

    def test_candidate_without_an_executable_is_skipped(self):
        self._make_install("InVEST broken Workbench.app", "3.20.0",
                           executable=False)
        self.assertEqual(
            locator.detect_installations(
                search_globs=[os.path.join(self.tmp, "InVEST*.app")]),
            [])

    def test_no_matches_is_not_an_error(self):
        self.assertEqual(
            locator.detect_installations(
                search_globs=[os.path.join(self.tmp, "nothing*")]),
            [])

    def test_duplicate_patterns_do_not_duplicate_results(self):
        self._make_install("InVEST 3.20.0 Workbench.app", "3.20.0")
        pattern = os.path.join(self.tmp, "InVEST*.app")
        self.assertEqual(
            len(locator.detect_installations(search_globs=[pattern, pattern])), 1)

    def test_unconfigured_error_points_at_the_menu_action(self):
        """The message is the only guidance a new user gets in the toolbox."""
        with self.assertRaises(locator.InvestNotFound) as caught:
            locator.find_binary("")
        self.assertIn("Configure InVEST", str(caught.exception))


class _FakeInvest:
    """Stands in for a running InVEST server."""

    def __init__(self, wait_ticks=4, models=("carbon", "sdr", "ndr")):
        self._wait_ticks = wait_ticks
        self._models = list(models)

    def is_ready(self):
        return False

    def ensure_running(self, on_wait=None):
        for tick in range(self._wait_ticks):
            if on_wait is not None:
                on_wait(tick * 25.0)

    def models(self):
        return list(self._models)

    def getspec(self, model_id):
        return {"model_id": model_id, "args": {}}


class TestHarvestProgress(unittest.TestCase):
    """Reading the model list takes about a minute, so it has to report
    progress or the toolbox just sits there looking broken."""

    def _harvest(self, fake):
        seen = []
        with unittest.mock.patch.object(harvest.server, "get", return_value=fake):
            specs = harvest.harvest_specs(
                "/fake/invest",
                progress=lambda message, fraction=None: seen.append(
                    (message, fraction)))
        return specs, seen

    def test_progress_is_reported_throughout(self):
        specs, seen = self._harvest(_FakeInvest())
        self.assertEqual(len(specs), 3)
        fractions = [f for _m, f in seen if f is not None]
        self.assertTrue(fractions, "no progress was reported")
        self.assertAlmostEqual(fractions[-1], 1.0)

    def test_progress_never_goes_backwards(self):
        _specs, seen = self._harvest(_FakeInvest())
        fractions = [f for _m, f in seen if f is not None]
        for earlier, later in zip(fractions, fractions[1:]):
            self.assertLessEqual(earlier, later)

    def test_startup_cannot_appear_finished(self):
        """The startup fraction is a time estimate, so a slow machine must not
        show 100% while it is still waiting."""
        fake = _FakeInvest(wait_ticks=40)   # far longer than expected
        _specs, seen = self._harvest(fake)
        during_startup = [f for message, f in seen
                          if f is not None and "Starting InVEST" in message]
        self.assertTrue(during_startup)
        self.assertLessEqual(max(during_startup), harvest._START_SHARE)

    def test_every_step_has_a_message(self):
        _specs, seen = self._harvest(_FakeInvest())
        self.assertTrue(all(message for message, _f in seen))

    def test_each_model_is_named_as_it_is_read(self):
        _specs, seen = self._harvest(_FakeInvest(models=("carbon", "sdr")))
        messages = " ".join(message for message, _f in seen)
        self.assertIn("carbon", messages)
        self.assertIn("sdr (2/2)", messages)


class TestEnumKeyTranslation(unittest.TestCase):
    """Dropdowns show a label but InVEST expects a key.

    A QgsProcessingParameterEnum with usesStaticStrings stores the option
    strings themselves, so the label has to be translated back before the
    value reaches InVEST. Sending the label instead leaves conditional inputs
    greyed out and puts an invalid option string into the model run.
    """

    def _plan(self, model_id, name):
        spec = normalize.normalise(load_specs("3.20.0")[model_id])
        return {p["name"]: p for p in paramspec.plan_model(spec["inputs"])}[name]

    def test_label_translates_to_the_invest_key(self):
        plan = self._plan("urban_nature_access", "search_radius_mode")
        self.assertEqual(
            paramspec.enum_key_for_value(plan, "Uniform radius"),
            "uniform radius")

    def test_a_key_passes_through_unchanged(self):
        """Values typed in the modeler or given to qgis_process are keys."""
        plan = self._plan("urban_nature_access", "search_radius_mode")
        self.assertEqual(
            paramspec.enum_key_for_value(plan, "uniform radius"),
            "uniform radius")

    def test_translation_round_trips(self):
        plan = self._plan("coastal_vulnerability", "geomorphology_fill_value")
        for key in plan["option_keys"]:
            label = paramspec.enum_value_for_key(plan, key)
            self.assertEqual(paramspec.enum_key_for_value(plan, label), key)

    def test_default_is_a_real_option_not_a_key(self):
        """A default that is not one of the option strings matches nothing."""
        for model_id, name in [("urban_nature_access", "search_radius_mode"),
                               ("habitat_risk_assessment", "risk_eq"),
                               ("sdr", "flow_dir_algorithm")]:
            with self.subTest(model=model_id):
                plan = self._plan(model_id, name)
                self.assertIn(plan["default"], plan["options"])

    def test_every_enum_in_every_model_round_trips(self):
        for version, model_id, raw in iter_models():
            spec = normalize.normalise(raw)
            for plan in paramspec.plan_model(spec["inputs"]):
                if plan["kind"] != "enum":
                    continue
                with self.subTest(version=version, model=model_id,
                                  param=plan["name"]):
                    self.assertEqual(len(plan["options"]),
                                     len(plan["option_keys"]))
                    for key in plan["option_keys"]:
                        label = paramspec.enum_value_for_key(plan, key)
                        self.assertEqual(
                            paramspec.enum_key_for_value(plan, label), key)

    def test_datastack_gives_the_widget_a_label(self):
        """to_parameter_values feeds widgets, so an enum must arrive as the
        label the dropdown actually contains."""
        plans = paramspec.plan_model(
            normalize.normalise(load_specs("3.20.0")["urban_nature_access"])["inputs"])
        result = datastack.to_parameter_values(
            {"search_radius_mode": "uniform radius"}, plans, "/tmp")
        self.assertEqual(result["values"]["search_radius_mode"], "Uniform radius")

    def test_unknown_value_is_left_alone(self):
        plan = self._plan("sdr", "flow_dir_algorithm")
        self.assertEqual(paramspec.enum_key_for_value(plan, "nonsense"),
                         "nonsense")


class TestErrorHandling(unittest.TestCase):

    def test_unsupported_spec_raises(self):
        with self.assertRaises(normalize.UnsupportedSpec):
            normalize.normalise({"model_id": "bogus"})
        with self.assertRaises(normalize.UnsupportedSpec):
            normalize.normalise("not a dict")


if __name__ == "__main__":
    unittest.main()
