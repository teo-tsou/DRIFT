"""Configuration – loaded from environment variables or .env file."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # DRIFT Framework controller (context-awareness, edge selection, migration control).
    # This is the only downstream service the NBI talks to — no kubeconfigs here.
    drift_controller_url: str = "http://localhost:8011"

    # AMS event notification webhook (external apps subscribe here)
    notification_callback_timeout: int = 5

    # ── O-2: event-driven predictive trigger ─────────────────────────────────
    # The UE whose pQoS degradation events initiate autonomous relocations.
    ue_imsi: str = "999700000000011"
    auto_trigger_enabled: bool = True
    # Minimum spacing between auto-initiated procedures per AMS (the controller
    # keeps its own anti-flap hysteresis + cooldown on top of this).
    auto_trigger_cooldown_s: float = 90.0
    # Admission control. A single UE-level degradation affects every application
    # on that UE, so without a cap one event starts one migration per registered
    # AMS, all at once — which has previously SIGKILLed a workload's container on
    # both clusters. Migrations also compete for the same target headroom, and
    # each procedure checks feasibility independently, so concurrent ones can all
    # be told there is room for them. 1 = strictly serial.
    auto_trigger_max_concurrent: int = 1
    # Where the autonomous trigger listens. "notify" = AF subscription
    # notifications only; "poll" = query the AF's request path every
    # pqos_poll_interval_s; "both". The two are independent pipelines on the AF
    # and have failed independently: on 2026-09-22 the request path returned an
    # empty Report while notifications kept flowing, and on 2026-09-23 the
    # request path tracked a congested cell (69 -> 1628 ms) while every
    # notification carried a frozen 103 ms. Polling lets a live prediction drive
    # relocation even when the notification pipeline is stale.
    pqos_trigger_mode: str = "both"
    pqos_poll_interval_s: float = 3.0
    # Auto-subscribe to the ENVELOPE pQoS notification API at startup when both
    # are set: pqos_sub_url is the AF layer (e.g. http://10.220.2.49:8001) and
    # pqos_sink_base is THIS service's externally reachable base for the
    # /pqos-events route (e.g. http://10.220.3.20:8010/pqos-events).
    pqos_sub_url: str = ""
    pqos_sink_base: str = ""
    # H-PQOS-I predicts the MAXIMUM throughput the device can use (capacity),
    # so BELOW-threshold means "the network cannot deliver what the app needs"
    # — absolute semantics, no idle-UE floor (an idle UE still has capacity).
    pqos_method: str = "H-PQOS-I"
    pqos_threshold_dl_mbps: float = 2.0
    # Latency is the ACTIONABLE degradation signal, and the reason it leads:
    # relocating the application to a closer or less loaded edge genuinely
    # reduces UE->edge latency, whereas it cannot improve the radio capacity
    # H-PQOS-I reports — the UE keeps the same link either way. It is also the
    # only signal that sees cell congestion, since H-PQOS-I predicts
    # per-device-as-if-alone (measured 2026-09-09: a contending UE took real
    # throughput 108 -> 61 Mbps while H-PQOS-I still predicted ~97).
    # H-PQOS-I has no latency support, so this one needs a traffic-following
    # model. NOTE the AF speaks SECONDS for latency; this is milliseconds and is
    # converted at subscribe time.
    pqos_method_latency: str = "SIMPLE-QS-PQOS-I"
    pqos_threshold_lat_ms: float = 200.0
    # Which subscriptions to create per AMS. Both by default: latency triggers,
    # throughput stays as a coverage-collapse backstop.
    pqos_sub_types: str = "latency,throughput"

    class Config:
        env_file = ".env"


settings = Settings()
