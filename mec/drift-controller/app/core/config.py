"""DRIFT Framework controller configuration.

This service holds no kubeconfigs and never talks to Kubernetes directly —
all cluster/node/VM state is read from the DRIFT backend's REST API, which
already owns that access. See drift_backend_client.py.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # DRIFT backend (SBI infrastructure layer) — source of truth for
    # sites, clusters, nodes and VMs.
    drift_backend_url: str = "http://localhost:8000"
    drift_backend_api_prefix: str = "/api/v1"

    # ENVELOPE CAMARA API base URLs (UE-centric signals)
    camara_location_url: str = "http://localhost:9003"
    camara_qod_url: str = "http://localhost:9001"
    # QoD session parameters (CAMARA QoD v1 API, see modules/qod.py). The
    # profile's maximum must exceed the UE's contended rate or the session only
    # throttles it, so QOS_L, never QOS_E. qod_ue_ip overrides the live lookup
    # of the UE's 5G address from the backend.
    qod_profile: str = "QOS_L"
    qod_app_server: str = "10.220.3.30"
    qod_duration_s: int = 300
    qod_ue_ip: str = ""

    # Real ENVELOPE PQoS endpoint (host 10.220.2.49 in the testbed). Left at the
    # mock default here; point it at the real service via env in production.
    #   ENVELOPE PQoS -> POST {pqos_url}/pqos_inf/req
    pqos_url: str = "http://localhost:8001"
    # The UE (5G device) whose QoS drives the decision — networkAccessIdentifier
    # (IMSI). Empty disables the UE-QoS calls (edge selection falls back to
    # energy/distance only).
    ue_imsi: str = ""
    # ENVELOPE PQoS request knobs. H-PQOS-I predicts the MAXIMUM throughput the
    # device can use (link capacity) — an absolute signal, independent of the
    # UE's current traffic, so no idle-UE floor and no traffic overprediction.
    # It does NOT support E2E_LATENCY, so latency keeps the traffic-following
    # method separately.
    pqos_method: str = "H-PQOS-I"
    pqos_method_latency: str = "SIMPLE-QS-PQOS-I"
    pqos_app_server: str = "10.220.3.20"

    # Energy telemetry endpoint (ECB 6G-LORE), keyed by arbitrary cluster id
    energy_telemetry_url: str = "http://localhost:9004"

    # Real ECB DRIFT Public API (see modules/energy.py). Clusters listed in
    # ecb_edge_map take their greenness from it; the others keep the telemetry
    # endpoint above. Map format, comma separated:
    #   <cluster name>=<ECB edge_location>:<model>[:<workload watts>]
    # e.g. "drift-edge-agx=agx-orin:yolo26s:25,drift-vehicle=jetson-nano:yolo26n:10"
    ecb_api_url: str = ""
    ecb_api_user: str = ""
    ecb_api_password: str = ""
    ecb_edge_map: str = ""
    # Power drawn by the workload at an edge when the map gives none (W), and
    # the look-ahead window the greenness is computed over (hours).
    ecb_workload_w: float = 15.0
    ecb_horizon_h: int = 1
    # The P50 forecast changes per 15-minute interval; cache answers this long.
    ecb_cache_s: float = 300.0
    # Vehicle clusters (comma-separated names): their greenness is the green
    # share of the EV battery's last charge and their available energy its
    # residual energy, from GET /v1/getEVGreenness (real Tesla data), instead
    # of the PV forecast of ecb_edge_map. Capacity turns kWh into a fraction.
    ecb_vehicle_clusters: str = ""
    ecb_ev_capacity_kwh: float = 57.5

    # Migration cooldown to avoid oscillation (seconds)
    migration_cooldown_s: int = 60

    # Optimal Edge Selection model.
    #   "constrained" (default): maximise greenness subject to hard constraints
    #     (resources, UE QoS, service area, energy reserve); proximity and energy
    #     only break ties. This is the placement model of DRIFT.
    #   "weighted": the earlier weighted sum below, kept so results measured with
    #     it (e.g. the first KPI-11 run) stay reproducible.
    selection_model: str = "constrained"
    # Constrained model: an edge serves the UE only within this radius of its
    # site (km). 0 disables the service-area constraint.
    service_radius_km: float = 0.0
    # Constrained model: minimum available energy (0..1) a site must keep, e.g.
    # the vehicle's battery reserve for driving.
    min_energy_pct: float = 0.2
    # Constrained model: greenness (0..1) a candidate must gain over the current
    # placement before DRIFT moves, calm vs. current edge failing a constraint.
    green_margin: float = 0.10

    # Weighted model: weights (must sum to 1.0)
    weight_distance: float = 0.5
    weight_greenness: float = 0.3
    weight_energy: float = 0.2

    # Distance normalisation range (km) for proximity scoring
    distance_min_km: float = 0.1
    distance_max_km: float = 300.0

    # Hysteresis: how much better a candidate's score must be than the
    # current placement's before DRIFT bothers moving the VM at all.
    # Raised when no QoS degradation is predicted, lowered when it is —
    # avoids the "increased overhead from frequent migrations" risk
    # (see proposal Section on associated risks).
    stay_margin_calm: float = 0.15
    stay_margin_degrading: float = 0.03

    # ── pQoS numeric thresholds (calibrated 2026-08-28 against the demo app's
    # healthy baseline THROUGH the ICCS model, which predicts the UE's traffic:
    # predicted DL median 7 Mbps / latency median 123 ms while healthy). A
    # prediction beyond these marks degradation, OR-ed with the CI verdict.
    # Set a threshold to 0 to disable that metric's signal.
    pqos_dl_min_mbps: float = 2.0
    pqos_ul_min_mbps: float = 2.0
    pqos_lat_max_ms: float = 400.0

    # After a QoD grant, re-check the numeric pQoS signal before declaring the
    # session sustained (only when the numeric signal raised the degradation —
    # the CI capability verdict does not change fast enough to verify against).
    qod_verify_delay_s: float = 8.0

    # Relocation is not free: subtract a footprint-proportional penalty from
    # every non-current candidate (measured: UE stall grows with checkpoint
    # size). Full weight at >=1 GiB RSS. 0 disables.
    migration_cost_weight: float = 0.05

    # If the caller supplies a preferred target cluster, keep it as long as
    # its score is within this fraction of the best candidate's score.
    preferred_override_tolerance: float = 0.1

    class Config:
        env_file = ".env"


settings = Settings()
