from __future__ import annotations

import json
import unittest
from unittest import mock

import viktor as vkt
from munch import munchify
from viktor.testing import mock_View

import app


class CapturingPythonAnalysis:
    """Small local stand-in that verifies the payload sent to the worker."""

    latest: "CapturingPythonAnalysis | None" = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.timeout = None
        type(self).latest = self

    def execute(self, timeout):
        self.timeout = timeout

    def get_output_file(self, filename):
        if filename != "study_results.json":
            return None
        return vkt.File.from_data(
            json.dumps(
                {
                    "results": [
                        {
                            "case": "T16_E200",
                            "thickness_mm": 16.0,
                            "embedment_mm": 200.0,
                            "n_kn": -300.0,
                            "my_knm": 70.0,
                            "mz_knm": 8.0,
                            "status": "PASS",
                            "governing_check": "Welds",
                            "governing_utilization_percent": 98.1,
                            "bp1_max_stress_mpa": 188.4,
                            "anchor_checks": [
                                {
                                    "name": "A1",
                                    "utilization_percent": 86.7,
                                    "check_status": True,
                                },
                                {
                                    "name": "A2",
                                    "utilization_percent": 92.4,
                                    "check_status": True,
                                },
                            ],
                            "concrete_checks": [
                                {
                                    "name": "CB 1",
                                    "utilization_percent": 50.6,
                                    "check_status": True,
                                }
                            ],
                            "steel_checks": [
                                {
                                    "name": "Welds",
                                    "utilization_percent": 98.1,
                                    "check_status": True,
                                }
                            ],
                            "message": "Calculated",
                        }
                    ]
                }
            ).encode("utf-8")
        )


class TestSweepInput(unittest.TestCase):
    def test_parse_option_sweep_sorts_and_removes_duplicate_values(self):
        self.assertEqual(
            app.parse_option_sweep([20, 16, 25, 20], "base-plate thickness"),
            [16.0, 20.0, 25.0],
        )

    def test_parse_option_sweep_requires_at_least_one_positive_value(self):
        with self.assertRaises(vkt.UserError):
            app.parse_option_sweep([], "base-plate thickness")
        with self.assertRaises(vkt.UserError):
            app.parse_option_sweep([16, 0], "base-plate thickness")

    def test_parse_load_accepts_signed_actions_and_rejects_nonfinite_values(self):
        self.assertEqual(app.parse_load(-300, "Axial force N"), -300.0)
        with self.assertRaises(vkt.UserError):
            app.parse_load(float("inf"), "Major moment My")

    @mock_View(app.Controller)
    def test_ifc_view_reads_the_template_ifc(self):
        self.assertEqual(app.IFC_TEMPLATE.name, "rhs_eurocode_parametric_sensitivity.ifc")
        self.assertTrue(app.IFC_TEMPLATE.is_file())
        result = app.Controller().show_template_ifc(params=munchify({}))
        self.assertIsInstance(result, vkt.IFCResult)

    @mock_View(app.Controller)
    def test_data_view_builds_cartesian_sweep_and_reads_worker_checks(self):
        params = munchify(
            {
                "plate_thicknesses_mm": [16, 20],
                "anchor_embedments_mm": [200, 250],
                "axial_force_kn": -450,
                "major_moment_knm": 85,
                "minor_moment_knm": -12,
            }
        )
        with mock.patch("app.PythonAnalysis", CapturingPythonAnalysis):
            result = app.Controller().show_anchor_and_concrete_checks(params=params)

        self.assertIsInstance(result, vkt.DataResult)
        analysis = CapturingPythonAnalysis.latest
        self.assertIsNotNone(analysis)
        self.assertEqual(analysis.timeout, 1800)
        transmitted = json.loads(analysis.kwargs["files"][0][1].getvalue())
        self.assertEqual(
            transmitted["loads"],
            {"n_kn": -450.0, "my_knm": 85.0, "mz_knm": -12.0},
        )
        self.assertEqual(
            [case["case"] for case in transmitted["cases"]],
            ["T16_E200", "T16_E250", "T20_E200", "T20_E250"],
        )

    def test_worst_checks_returns_the_governing_case_for_each_check(self):
        checks = app.worst_checks(
            [
                {
                    "case": "T16_E200",
                    "anchor_checks": [
                        {"name": "A1", "utilization_percent": 65, "check_status": True}
                    ],
                },
                {
                    "case": "T20_E250",
                    "anchor_checks": [
                        {"name": "A1", "utilization_percent": 91, "check_status": True},
                        {"name": "A2", "utilization_percent": 102, "check_status": False},
                    ],
                },
            ],
            "anchor_checks",
        )
        self.assertEqual(
            checks,
            [
                {
                    "name": "A1",
                    "utilization_percent": 91.0,
                    "check_status": True,
                    "case": "T20_E250",
                },
                {
                    "name": "A2",
                    "utilization_percent": 102.0,
                    "check_status": False,
                    "case": "T20_E250",
                },
            ],
        )
        self.assertEqual(app.check_data_status(checks[0]), vkt.DataStatus.SUCCESS)
        self.assertEqual(app.check_data_status(checks[1]), vkt.DataStatus.ERROR)

    @mock_View(app.Controller)
    def test_data_view_limits_case_count_before_contacting_worker(self):
        params = munchify(
            {
                "plate_thicknesses_mm": [12, 16, 20, 25, 30],
                "anchor_embedments_mm": [150, 200, 250, 300, 350],
                "axial_force_kn": -300,
                "major_moment_knm": 70,
                "minor_moment_knm": 8,
            }
        )
        with self.assertRaises(vkt.UserError):
            app.Controller().show_anchor_and_concrete_checks(params=params)


if __name__ == "__main__":
    unittest.main()
