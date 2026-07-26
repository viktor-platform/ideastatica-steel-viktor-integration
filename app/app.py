"""VIKTOR editor for an IDEA StatiCa RHS base-plate sensitivity study."""

from __future__ import annotations

import json
import math
from io import BytesIO
from itertools import product
from pathlib import Path

import viktor as vkt
from viktor.core import File
from viktor.external.python import PythonAnalysis


APP_ROOT = Path(__file__).resolve().parent
TEMPLATES = APP_ROOT / "templates"
IFC_TEMPLATE = TEMPLATES / "rhs_eurocode_parametric_sensitivity.ifc"
# This IDEA project exposes the Developer links bp_t and anchor_embed.
SENSITIVITY_TEMPLATE = TEMPLATES / "rhs_eurocode_parametric_sensitivity.ideaCon"
WORKER_SCRIPT = APP_ROOT / "run_idea_statica.py"
MAX_CASES = 24


def parse_option_sweep(values: list[float] | None, label: str) -> list[float]:
    """Validate a multi-select sweep and return it in ascending display order."""
    if not values:
        raise vkt.UserError(f"Select at least one {label} value.")
    result = sorted({float(value) for value in values})
    if any(value <= 0 for value in result):
        raise vkt.UserError(f"Every {label} value must be greater than zero.")
    return result


def parse_load(value: float, label: str) -> float:
    """Accept a finite connection action; compression is negative axial force."""
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise vkt.UserError(f"{label} must be a number.") from error
    if not math.isfinite(result):
        raise vkt.UserError(f"{label} must be finite.")
    return result


def worst_checks(results: list[dict], check_key: str) -> list[dict]:
    """Return the governing utilization of each named check across the sweep."""
    governing: dict[str, dict] = {}
    for result in results:
        for check in result.get(check_key, []):
            try:
                utilization = float(check["utilization_percent"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(utilization):
                continue
            name = str(check.get("name") or "Unnamed check")
            candidate = {
                "name": name,
                "utilization_percent": utilization,
                "check_status": bool(check.get("check_status", False)),
                "case": str(result.get("case", "-")),
            }
            if name not in governing or utilization > governing[name]["utilization_percent"]:
                governing[name] = candidate
    return sorted(governing.values(), key=lambda check: check["name"])


def check_data_status(check: dict) -> vkt.DataStatus:
    if not check["check_status"] or check["utilization_percent"] > 100:
        return vkt.DataStatus.ERROR
    return vkt.DataStatus.SUCCESS


def case_check_status(checks: list[dict]) -> vkt.DataStatus:
    """Return one visible status for an iteration, based on all its checks."""
    if any(check_data_status(check) == vkt.DataStatus.ERROR for check in checks):
        return vkt.DataStatus.ERROR
    return vkt.DataStatus.SUCCESS


def check_data_item(label: str, check: dict, subgroup=None) -> vkt.DataItem:
    """Format one IDEA utilization with a consistent pass/fail indicator."""
    return vkt.DataItem(
        label,
        float(check["utilization_percent"]),
        suffix="%",
        number_of_decimals=1,
        subgroup=subgroup,
        status=check_data_status(check),
        status_message="Pass" if check_data_status(check) == vkt.DataStatus.SUCCESS else "Fail",
    )


def iteration_check_items(
    anchors: list[dict], concrete: list[dict], steel: list[dict]
) -> list[vkt.DataItem]:
    """Create a concise hierarchy: iteration -> anchor -> verification modes."""
    items = []
    for anchor in anchors:
        details = anchor.get("details", [])
        detail_group = (
            vkt.DataGroup(
                *[
                    check_data_item(
                        str(detail.get("name") or "Unnamed verification"), detail
                    )
                    for detail in details
                    if "utilization_percent" in detail
                ]
            )
            if details
            else None
        )
        items.append(
            check_data_item(
                f"Anchor {anchor.get('name') or 'Unnamed'}", anchor, subgroup=detail_group
            )
        )
    items.extend(
        check_data_item(
            f"Concrete {check.get('name') or 'Unnamed'}", check
        )
        for check in concrete
        if "utilization_percent" in check
    )
    items.extend(
        check_data_item(
            f"Steel {check.get('name') or 'Unnamed'}", check
        )
        for check in steel
        if "utilization_percent" in check
    )
    return items


class Parametrization(vkt.Parametrization):
    intro = vkt.Text(
        "# IDEA StatiCa RHS base-plate sensitivity\n"
        "The IFC tab shows the prepared template. Select the plate thicknesses and "
        "anchor depths to include; the app calculates every selected combination."
    )
    plate_thicknesses_mm = vkt.MultiSelectField(
        "Base-plate thicknesses",
        options=[
            vkt.OptionListElement(12, "12 mm"),
            vkt.OptionListElement(16, "16 mm"),
            vkt.OptionListElement(20, "20 mm"),
            vkt.OptionListElement(25, "25 mm"),
            vkt.OptionListElement(30, "30 mm"),
        ],
        default=[16, 20, 25],
        flex=100,
        description="Every selected thickness is combined with every selected anchor depth.",
    )
    anchor_embedments_mm = vkt.MultiSelectField(
        "Anchor embedment depths",
        options=[
            vkt.OptionListElement(150, "150 mm"),
            vkt.OptionListElement(200, "200 mm"),
            vkt.OptionListElement(250, "250 mm"),
            vkt.OptionListElement(300, "300 mm"),
            vkt.OptionListElement(350, "350 mm"),
        ],
        default=[200, 250, 300],
        flex=100,
        description="Every selected depth is combined with every selected plate thickness.",
    )
    axial_force_kn = vkt.NumberField("Axial force N", default=-300, suffix="kN", flex=100)
    major_moment_knm = vkt.NumberField("Major moment My", default=70, suffix="kNm", flex=100)
    minor_moment_knm = vkt.NumberField("Minor moment Mz", default=8, suffix="kNm", flex=100)


class Controller(vkt.Controller):
    parametrization = Parametrization

    @vkt.IFCView("Template IFC", duration_guess=1)
    def show_template_ifc(self, params, **kwargs):
        if not IFC_TEMPLATE.is_file():
            raise vkt.UserError(f"The template IFC was not found: {IFC_TEMPLATE}")
        return vkt.IFCResult(File.from_path(IFC_TEMPLATE))

    @vkt.DataView(
        "Anchor and concrete checks",
        duration_guess=900,
        update_label="Run IDEA StatiCa sweep",
    )
    def show_anchor_and_concrete_checks(self, params, **kwargs):
        """Run the selected sweep and return the governing detailed checks."""
        thicknesses_mm = parse_option_sweep(
            params.plate_thicknesses_mm, "base-plate thickness"
        )
        embedments_mm = parse_option_sweep(
            params.anchor_embedments_mm, "anchor embedment"
        )
        loads = {
            "n_kn": parse_load(params.axial_force_kn, "Axial force N"),
            "my_knm": parse_load(params.major_moment_knm, "Major moment My"),
            "mz_knm": parse_load(params.minor_moment_knm, "Minor moment Mz"),
        }
        case_count = len(thicknesses_mm) * len(embedments_mm)
        if case_count > MAX_CASES:
            raise vkt.UserError(
                f"This request has {case_count} cases. Limit it to {MAX_CASES} cases per run."
            )
        if not SENSITIVITY_TEMPLATE.is_file():
            raise vkt.UserError(
                "The sensitivity project is missing. In IDEA StatiCa, open "
                "rhs_eurocode_parametric_expanded.contemp and save a project copy as "
                "app/templates/rhs_eurocode_parametric_sensitivity.ideaCon."
            )

        study_input = {
            "loads": loads,
            "cases": [
                {
                    "case": f"T{thickness:g}_E{embedment:g}",
                    "bp_t_m": thickness / 1000,
                    "anchor_embed_m": embedment / 1000,
                }
                for thickness, embedment in product(thicknesses_mm, embedments_mm)
            ]
        }
        analysis = PythonAnalysis(
            script=File.from_path(WORKER_SCRIPT),
            files=[
                ("study_input.json", BytesIO(json.dumps(study_input).encode("utf-8"))),
                ("template.ideaCon", File.from_path(SENSITIVITY_TEMPLATE)),
            ],
            output_filenames=["study_results.json"],
        )
        analysis.execute(timeout=1800)
        output_file = analysis.get_output_file("study_results.json")
        if output_file is None:
            raise vkt.UserError("The IDEA StatiCa worker did not return study_results.json.")
        output = json.loads(output_file.getvalue())

        results = output.get("results", [])
        if not any(
            result.get("anchor_checks") or result.get("concrete_checks")
            for result in results
        ):
            raise vkt.UserError(
                "The IDEA StatiCa sweep did not return anchor or concrete check details."
            )

        iterations = []
        for result in results:
            anchors = result.get("anchor_checks", [])
            concrete = result.get("concrete_checks", [])
            steel = result.get("steel_checks", [])
            all_checks = anchors + concrete + steel
            items = iteration_check_items(anchors, concrete, steel)
            if not items:
                continue
            case_name = str(result.get("case") or "Unnamed iteration")
            thickness = result.get("thickness_mm")
            embedment = result.get("embedment_mm")
            dimensions = []
            if thickness is not None:
                dimensions.append(f"plate {float(thickness):g} mm")
            if embedment is not None:
                dimensions.append(f"embedment {float(embedment):g} mm")
            iterations.append(
                vkt.DataItem(
                    case_name,
                    explanation_label=" · ".join(dimensions),
                    subgroup=vkt.DataGroup(*items),
                    status=case_check_status(all_checks),
                    status_message=(
                        "All returned checks pass"
                        if case_check_status(all_checks) == vkt.DataStatus.SUCCESS
                        else "One or more returned checks fail"
                    ),
                )
            )
        return vkt.DataResult(vkt.DataGroup(*iterations))
