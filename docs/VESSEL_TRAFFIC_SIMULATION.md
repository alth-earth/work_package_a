# Vessel Traffic Simulation Layer

## Purpose

Work Package A now includes a generated `vessel_traffic` data layer. This layer fills the route-traffic gap needed by Work Package B when protected or historical AIS tracks are unavailable.

The model does not redistribute original AIS tracks. It produces route-level traffic-condition fields that match the same A-package data contract used by the other environmental data types.

## Why this layer is necessary

Historical vessel-track access is limited by account permissions, data licensing and personal-data restrictions. For the two Arctic study corridors, complete historical AIS tracks cannot always be obtained in time for repeatable model training. The generated layer therefore provides a stable surrogate of real-time traffic pressure, so downstream risk modelling can still learn how traffic congestion, encounter pressure and traffic uncertainty should influence navigation risk.

## Time range

The standard collection and replay window is now the latest 144 hours. Frames are generated every 3 hours, so one full window contains 49 frames per corridor.

## Output variables

- `traffic_density`: normalized vessel traffic intensity, 0 to 1.
- `traffic_count`: route-level estimated vessel count pressure.
- `traffic_risk`: normalized traffic-related navigation risk, 0 to 1.
- `traffic_confidence`: confidence of the generated traffic condition, 0 to 1.

## Model parameters

The calibrated parameters are stored in `configs/vessel_traffic_model.toml`. Code reads this file at runtime, which keeps model weights auditable and avoids hard-coding trained parameters into the source implementation.

## A-to-B handoff

`vessel_traffic` is registered in the same variable registry and data-source contract as the existing Work Package A data. It can be requested through `WorkPackageA.prepare_window(...)` and appears in the formal bundle/provenance path as a dynamic 3-hour data type. Work Package B can consume it as another dynamic risk factor without changing the interface shape.
