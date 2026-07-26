# IDEA StatiCa templates

Place reusable IDEA StatiCa Connection templates in this folder.

`rhs_eurocode_parametric_sensitivity.ideaCon` is the RHS base-plate template used by the VIKTOR sensitivity app. It exposes the Developer parameters `bp_t` (base-plate thickness) and `anchor_embed` (anchor embedment).

`rhs_eurocode_parametric_sensitivity.ifc` is the matching VIKTOR-friendly geometry displayed in the app's **Template IFC** view.

Keep templates as source files. The worker copies the selected template into its job directory before changing parameters or calculating it.
