# Predictive QoS triggering of application relocation (O-2)

How DRIFT turns a network-side QoS prediction into an application relocation:
what fires it, what is checked, who decides, and on what evidence. Behaviour
described here was measured against the ENVELOPE testbed AF at
`10.220.2.49:8001`; the measurements are dated so they can be re-verified.

## 1. Overview

```
ENVELOPE AF ──notification──► DRIFT MEC-021 sink ──procedure──► DRIFT controller
 (prediction)                  (does this app care?)            (which edge, or stay)
```

Three decisions, deliberately separated:

| Stage | Question | Owner |
|---|---|---|
| Prediction | is the UE's link about to degrade? | the network (AF/NWDAF) |
| Relevance | does that break *this* application? | the application's declared requirements |
| Placement | is a better edge available, and worth the move? | Optimal Edge Selection |

Degradation does not order a relocation. It changes how readily the selector
accepts one that proximity and energy already favour (§5).

## 2. Subscription

Registering an Application Mobility Service (ETSI MEC-021) opens two pQoS
subscriptions on behalf of that application, armed at **its own** thresholds:

| QosType | Matching | Model | Role |
|---|---|---|---|
| `E2E_LATENCY` | ABOVE | `SIMPLE-QS-PQOS-I` | primary trigger |
| `THROUGHPUT_DL` | BELOW | `H-PQOS-I` | coverage-collapse backstop |

Latency leads because it is the signal relocation can act on: a closer or less
loaded edge reduces UE-to-edge latency, whereas no placement decision improves
the radio capacity `H-PQOS-I` reports. Latency is also the only signal that
observes cell congestion — `H-PQOS-I` predicts per-device-as-if-alone. Measured
2026-09-09: a competing UE reduced real throughput from 108 to 61 Mbps while
`H-PQOS-I` continued to predict ~97 Mbps; only the traffic-following model
registered the contention, at ~1125 ms.

`H-PQOS-I` has no latency support, hence the two models.

Subscriptions are bound to the AMS lifetime: opened on registration, deleted on
deregistration. A traffic-following model reports ~0 Mbps for an idle UE, so an
unregistered application is not monitored at all rather than permanently alarmed.

## 3. Notification format, and a trap worth documenting

The AF delivers the raw NWDAF `QOS_SUSTAINABILITY` event to
`{NotificationAddress}/{CorrelationID}/{UeID}`, one entry per second of horizon:

```json
{"eventNotifications":[{"event":"QOS_SUSTAINABILITY","qosSustainInfos":[
  {"startTs":"…","endTs":"…","ranUeThrouThd":"0.166143 Mbps"}, … ]}]}
```

**The value field is always named `ranUeThrouThd` and always suffixed `Mbps`,
regardless of the metric subscribed to.** Nothing in the body identifies the
metric. Only the `CorrelationID` in the URL does.

Verified 2026-09-22 by delivering two subscriptions side by side for 90 s,
identical except for `QosType`:

| QosType | horizon | mean | actual meaning |
|---|---|---|---|
| `THROUGHPUT_DL` | 44.6 – 52.6 | 47.85 | Mbps |
| `E2E_LATENCY` | 0.088 – 0.342 | 0.1918 | **seconds** (192 ms) |

The AF honours `QosType` correctly; only the envelope is misleading. A consumer
that reads the field at face value will interpret a 192 ms latency horizon as
0.19 Mbps of throughput — an apparent total collapse. DRIFT therefore resolves
the `CorrelationID` to the subscription that produced it and interprets the
values as that metric, converting latency from seconds.

## 4. Admission checks at the sink

In order, an arriving notification is discarded unless it passes all of:

1. **Known subscription.** An unrecognised `CorrelationID` is ignored. Such
   events cannot be interpreted at all — neither their metric nor the
   application they concern is knowable — and the AF continues delivering to
   subscriptions whose identifiers a restart has discarded.
2. **Not idle.** An all-zero horizon indicates a UE with no traffic, not a
   degraded network.
3. **Correct UE.** The event's `UeID` must match the IMSI this AMS serves.
4. **Materially violated.** The **mean** of the horizon is compared against this
   application's requirement. A mean is used rather than an extreme: a single
   zero second in ten is ordinary jitter, and relocating on it produces
   flapping.
5. **Not already acting.** Per-AMS cooldown (90 s) and suppression of a second
   procedure while one is in flight.
6. **Capacity to act.** A global limit on concurrent auto-initiated relocations
   (default 1), stateful services first. Concurrent procedures each evaluate
   target headroom independently and can all be told there is room for them.

A notification that passes opens a standard MEC-021 mobility procedure with **no
target cluster specified**, so the same procedure records, state machine and
notifications serve autonomous and operator-initiated relocation alike.

## 5. Placement decision

Optimal Edge Selection enumerates every active edge as a candidate, **including
the current placement**, then:

1. discards candidates without CPU/memory headroom for the workload;
2. scores the remainder:
   `0.5 × proximity + 0.3 × greenness + 0.2 × energy`;
3. subtracts a relocation cost scaled to the workload's memory footprint;
4. re-reads the pQoS context to confirm the degradation persists.

The current placement is displaced only if the best alternative exceeds it by a
hysteresis margin:

| Condition | Margin |
|---|---|
| normal | 15% |
| QoS degraded **and** the better edge is closer to the UE | 3% |

This is the point of the design: prediction lowers the bar for a relocation that
proximity and energy already justify, rather than commanding one. The outcome is
`stay` or `cross_cluster`, with the reason recorded against the procedure.

## 6. Per-application requirements

A registration may declare `min_dl_mbps` and `max_latency_ms`; unset values
inherit a platform default. These arm that application's own subscriptions and
are the requirements checked in §4.4.

The separation matters because network predictions are **per-UE** and cannot be
otherwise: `H-PQOS-I` derives from the UE's radio signal characteristics, and the
report is keyed by IMSI with no per-application dimension. A UE-level degradation
therefore does not affect every application on that UE equally, and without
per-application requirements a single event relocates all of them.

This mirrors CAMARA Connectivity Insights, where an Application Profile
(`packetDelayBudget`, `targetMinDownstreamRate`, `targetMinUpstreamRate`) is
evaluated against a per-device prediction to yield "meets" or "unable to meet the
application requirements".

## 7. Known constraints

- **Event-driven only.** There is no periodic re-evaluation: placement is
  assessed when a notification arrives, never on a timer. A UE that moves while
  its link remains healthy therefore does not cause relocation, which limits
  follow-me to periods of predicted degradation.
- **Registry is not persistent.** AMS registrations and subscription identifiers
  are held in memory. A restart loses them while the AF continues delivering to
  the subscriptions, which are then unrecoverable — the AF exposes no endpoint to
  enumerate existing subscriptions.
- **No cross-procedure capacity reservation.** Concurrency is bounded by count,
  not by reserving target headroom.
