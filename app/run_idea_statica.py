import json
import math
import subprocess
import time
from pathlib import Path

import requests
from ideastatica_connection_api import IdeaParameterUpdate
from ideastatica_connection_api.connection_api_service_attacher import (
    ConnectionApiServiceAttacher,
)

ANCHOR_VERIFICATION_FIELDS = (
    ("Anchor tension", "unityCheckTension"),
    ("Anchor shear", "unityCheckShearRes"),
    ("Tension/shear interaction", "interactionTensionShear"),
    ("Concrete cone breakout in tension", "unityCheckConeTensionRes"),
    ("Concrete pull-out", "unityCheckConcretePullOutRes"),
    ("Concrete shear breakout", "unityCheckConcreteShearBreakOutRes"),
    ("Concrete pry-out", "unityCheckPryOutfailureResistanceRes"),
    ("Concrete edge failure", "unityCheckConcreteEdgefailureRes"),
)
STEEL_SUMMARY_CHECK_NAMES = {"Plates", "Welds", "Shear"}


def update_template_load(api, project_id, connection_id, loads: dict[str, float]) -> None:
    load_effects = api.load_effect.get_load_effects(project_id, connection_id)
    active_effects = [effect for effect in load_effects if effect.active]
    if len(active_effects) != 1:
        raise RuntimeError(
            "The template must have exactly one active load effect for this simple sweep. "
            f"Found {len(active_effects)}."
        )

    load_id = active_effects[0].id
    if load_id is None:
        raise RuntimeError("The active IDEA load effect does not have an ID.")
    effect = api.load_effect.get_load_effect(project_id, connection_id, load_id)
    if len(effect.member_loadings or []) != 1:
        raise RuntimeError(
            "The template must have exactly one member loading for this base-plate sweep."
        )

    section_load = effect.member_loadings[0].section_load
    if section_load is None:
        raise RuntimeError("The active IDEA load effect has no section load to update.")
    section_load.n = loads["n_kn"] * 1_000
    section_load.my = loads["my_knm"] * 1_000
    section_load.mz = loads["mz_knm"] * 1_000
    api.load_effect.update_load_effect(
        project_id, connection_id, con_load_effect=effect, _request_timeout=60
    )


def extract_utilization_checks(detail: dict, key: str) -> list[dict[str, object]]:
    """Return named IDEA check utilizations in the percentage convention used by the API."""
    checks: list[dict[str, object]] = []
    for check in detail.get(key, []):
        try:
            utilization = float(check["unityCheck"])
        except (KeyError, TypeError, ValueError):
            continue
        checks.append(
            {
                "name": str(check.get("name") or "Unnamed check"),
                "utilization_percent": round(utilization, 4),
                "check_status": bool(check.get("checkStatus", False)),
            }
        )
    return checks


def extract_anchor_verification_details(raw_result: dict) -> dict[str, list[dict[str, object]]]:
    """Return the meaningful raw verification modes for every named anchor.
    """
    details_by_anchor: dict[str, list[dict[str, object]]] = {}
    for raw_anchor in (raw_result.get("boltsAnchor") or {}).values():
        anchor_name = str(raw_anchor.get("name") or "Unnamed anchor")
        checks = []
        for label, field in ANCHOR_VERIFICATION_FIELDS:
            try:
                ratio = float(raw_anchor.get(field, 0.0))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(ratio) or ratio <= 0:
                continue
            checks.append(
                {
                    "name": label,
                    "utilization_percent": round(ratio * 100, 4),
                    "check_status": ratio <= 1.0,
                }
            )
        if checks:
            details_by_anchor[anchor_name] = checks
    return details_by_anchor


def add_anchor_verification_details(
    anchor_checks: list[dict[str, object]], raw_result: dict
) -> list[dict[str, object]]:
    """Attach raw verification details to the matching compact anchor result."""
    details_by_anchor = extract_anchor_verification_details(raw_result)
    for anchor in anchor_checks:
        anchor["details"] = details_by_anchor.get(str(anchor["name"]), [])
    return anchor_checks


def extract_steel_summary_checks(checks: list[dict]) -> list[dict[str, object]]:
    """Keep the readable, calculated steel checks from IDEA's result summary."""
    result = []
    for check in checks:
        if check.get("name") not in STEEL_SUMMARY_CHECK_NAMES:
            continue
        try:
            utilization = float(check["checkValue"])
        except (KeyError, TypeError, ValueError):
            continue
        result.append(
            {
                "name": str(check["name"]),
                "utilization_percent": round(utilization, 4),
                "check_status": bool(check.get("checkStatus", False)),
            }
        )
    return result


def run_idea_statica() -> None:
    """Calculate all requested Developer-parameter pairs and write one JSON table."""
    idea_install = Path(r"C:\Program Files\IDEA StatiCa\StatiCa 26.0")
    idea_api_url = "http://127.0.0.1:5193"
    service_executable = idea_install / "IdeaStatiCa.ConnectionRestApi.exe"
    heartbeat_url = f"{idea_api_url}/heartbeat"
    template_path = Path.cwd() / "template.ideaCon"
    input_path = Path.cwd() / "study_input.json"
    output_path = Path.cwd() / "study_results.json"

    if not template_path.is_file():
        raise FileNotFoundError(f"IDEA StatiCa project was not transferred: {template_path}")
    if not input_path.is_file():
        raise FileNotFoundError(f"Sweep input was not transferred: {input_path}")

    try:
        service_is_running = requests.get(heartbeat_url, timeout=2).ok
    except requests.RequestException:
        service_is_running = False
    if not service_is_running:
        if not service_executable.is_file():
            raise FileNotFoundError(
                "IDEA StatiCa Connection REST service was not found at: "
                f"{service_executable}"
            )
        subprocess.Popen(
            [str(service_executable), "-port:5193"],
            cwd=idea_install,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if requests.get(heartbeat_url, timeout=2).ok:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise RuntimeError(f"IDEA StatiCa REST service did not respond at {heartbeat_url}")

    study_input = json.loads(input_path.read_text(encoding="utf-8"))
    loads = study_input.get("loads")
    if not isinstance(loads, dict) or set(loads) != {"n_kn", "my_knm", "mz_knm"}:
        raise ValueError("Study input must contain the N, My, and Mz load values.")
    results: list[dict[str, object]] = []
    with ConnectionApiServiceAttacher(idea_api_url).create_api_client() as api:
        project = api.project.open_project_from_filepath(str(template_path))
        connections = api.connection.get_connections(project.project_id)
        if not connections:
            raise RuntimeError("The IDEA StatiCa project contains no Connection items.")
        connection_id = connections[0].id
        parameters = api.parameter.get_parameters(
            project.project_id, connection_id, include_hidden=True
        )
        parameter_keys = {parameter.key for parameter in parameters}
        required_keys = {"bp_t", "anchor_embed"}
        missing_keys = sorted(required_keys - parameter_keys)
        if missing_keys:
            raise RuntimeError(
                "The project does not expose the required Developer parameter(s): "
                f"{', '.join(missing_keys)}. Open the expanded .contemp in IDEA StatiCa, "
                "then save it as rhs_eurocode_parametric_sensitivity.ideaCon."
            )

        for item in study_input["cases"]:
            thickness_m = float(item["bp_t_m"])
            embedment_m = float(item["anchor_embed_m"])
            row: dict[str, object] = {
                "case": str(item["case"]),
                "thickness_mm": round(thickness_m * 1000, 3),
                "embedment_mm": round(embedment_m * 1000, 3),
                "n_kn": float(loads["n_kn"]),
                "my_knm": float(loads["my_knm"]),
                "mz_knm": float(loads["mz_knm"]),
                "status": "ERROR",
                "governing_check": "-",
                "governing_utilization_percent": None,
                "bp1_max_stress_mpa": None,
                "anchor_checks": [],
                "concrete_checks": [],
                "steel_checks": [],
                "message": "",
            }
            try:
                update = api.parameter.update(
                    project.project_id,
                    connection_id,
                    [
                        IdeaParameterUpdate(key="bp_t", expression=f"{thickness_m:.6f}"),
                        IdeaParameterUpdate(
                            key="anchor_embed", expression=f"{embedment_m:.6f}"
                        ),
                    ],
                    _request_timeout=60,
                )
                if not update.set_to_model or update.failed_validations:
                    raise RuntimeError(
                        "IDEA StatiCa rejected the parameter update: "
                        f"{update.failed_validations}"
                    )

                update_template_load(api, project.project_id, connection_id, loads)

                summary = api.calculation.calculate(
                    project.project_id, [connection_id], _request_timeout=600
                )
                detailed = api.calculation.get_results(
                    project.project_id, [connection_id], _request_timeout=600
                )
                raw_result_text = api.calculation.get_raw_json_results(
                    project.project_id, [connection_id], _request_timeout=600
                )
                detail = detailed[0]
                detail_dict = (
                    detail.model_dump(by_alias=True)
                    if hasattr(detail, "model_dump")
                    else detail.to_dict()
                    if hasattr(detail, "to_dict")
                    else detail
                )
                checks = [
                    check
                    for check in detail_dict.get("checkResSummary", [])
                    if not check.get("skipped", False) and check.get("name") != "Analysis"
                ]
                governing = max(checks, key=lambda check: check.get("checkValue", 0.0), default=None)
                bp1_plate = next(
                    (
                        plate
                        for plate in detail_dict.get("checkResPlate", [])
                        if plate.get("name") == "BP1"
                    ),
                    None,
                )
                row["status"] = (
                    "PASS"
                    if all(check.get("checkStatus", False) for check in checks)
                    and bool(summary[0].passed)
                    else "FAIL"
                )
                row["governing_check"] = governing.get("name", "-") if governing else "-"
                row["governing_utilization_percent"] = (
                    round(float(governing.get("checkValue", 0.0)), 2)
                    if governing
                    else None
                )
                row["bp1_max_stress_mpa"] = (
                    round(float(bp1_plate.get("maxStress", 0.0)), 2)
                    if bp1_plate
                    else None
                )
                row["anchor_checks"] = extract_utilization_checks(
                    detail_dict, "checkResAnchor"
                )
                row["steel_checks"] = extract_steel_summary_checks(checks)
                try:
                    raw_result = json.loads(raw_result_text[0])
                    row["anchor_checks"] = add_anchor_verification_details(
                        row["anchor_checks"], raw_result
                    )
                except (IndexError, TypeError, json.JSONDecodeError):
                    # The compact anchor results remain available if a future
                    # IDEA version changes the raw result payload.
                    pass
                row["concrete_checks"] = extract_utilization_checks(
                    detail_dict, "checkResConcreteBlock"
                )
                row["message"] = "Calculated"
            except Exception as error:  # Report a failed point without discarding the whole sweep.
                row["message"] = str(error)
            results.append(row)

    output_path.write_text(json.dumps({"results": results}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    run_idea_statica()
