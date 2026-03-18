import argparse
import datetime as dt
import hashlib
import json
import time
from pathlib import Path

EPOCH = dt.datetime(2000, 1, 1, 12, 0, 0)

CYCLE_PERIOD_DAYS = {
    "second": 1.0 / 86400.0,
    "minute": 60.0 / 86400.0,
    "hour": 3600.0 / 86400.0,
    "day": 1.0,
    "week": 7.0,
    "month": 30.436875,
    "year": 365.2425,
}

CYCLE_WEIGHT = {
    "second": 1.50,
    "minute": 1.20,
    "hour": 1.00,
    "day": 1.00,
    "week": 0.80,
    "month": 0.60,
    "year": 0.50,
}

CRITICAL_CYCLES = {"second", "minute", "hour", "day"}
GOVERNANCE_CYCLES = ["second", "minute", "hour", "day", "week"]
SIGNATURE_CYCLES = ["month", "year"]
CAPSULE_VERSION = "4.1"


def parse_time(text):
    return dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


def days_since_epoch(t):
    return (t - EPOCH).total_seconds() / 86400.0


def phase_of_cycle(t_days, period_days):
    return (t_days / period_days) % 1.0


def angdiff(a, b):
    return (a - b + 0.5) % 1.0 - 0.5


def circular_ms_from_cycle(obs_phase, model_phase, period_days):
    d_days = angdiff(obs_phase, model_phase) * period_days
    return d_days * 86400.0 * 1000.0


def cycle_phase_vector(t):
    t_days = days_since_epoch(t)
    return {name: phase_of_cycle(t_days, period) for name, period in CYCLE_PERIOD_DAYS.items()}


def cycle_residual_ms(observed_time, internal_time, cycle_name):
    t_obs = days_since_epoch(observed_time)
    t_int = days_since_epoch(internal_time)
    obs_phase = phase_of_cycle(t_obs, CYCLE_PERIOD_DAYS[cycle_name])
    int_phase = phase_of_cycle(t_int, CYCLE_PERIOD_DAYS[cycle_name])
    return circular_ms_from_cycle(obs_phase, int_phase, CYCLE_PERIOD_DAYS[cycle_name])


def weighted_residual_ms(observed_time, internal_time):
    total_weight = 0.0
    total_value = 0.0
    residuals = {}

    for cycle_name in CYCLE_PERIOD_DAYS:
        r = cycle_residual_ms(observed_time, internal_time, cycle_name)
        w = CYCLE_WEIGHT[cycle_name]
        residuals[cycle_name] = r
        total_value += w * r
        total_weight += w

    mean_residual = total_value / total_weight if total_weight > 0 else 0.0
    return mean_residual, residuals


def governance_residual_ms(observed_time, internal_time):
    total_weight = 0.0
    total_value = 0.0
    residuals = {}

    for cycle_name in GOVERNANCE_CYCLES:
        r = cycle_residual_ms(observed_time, internal_time, cycle_name)
        w = CYCLE_WEIGHT[cycle_name]
        residuals[cycle_name] = r
        total_value += w * r
        total_weight += w

    mean_residual = total_value / total_weight if total_weight > 0 else 0.0
    return mean_residual, residuals


def cycle_score(observed_time, candidate_time, include_signature_cycles=True):
    _, residuals = weighted_residual_ms(observed_time, candidate_time)
    score = 0.0
    for cycle_name, residual in residuals.items():
        if not include_signature_cycles and cycle_name in SIGNATURE_CYCLES:
            continue
        score += CYCLE_WEIGHT[cycle_name] * (residual ** 2)
    return score


def day_of_year_distance(a, b):
    diff = abs(a - b)
    return min(diff, 366 - diff)


def calendar_penalty(observed_time, candidate_time, weekday_weight, month_weight, yearday_weight, year_weight):
    penalty = 0.0

    if observed_time.weekday() != candidate_time.weekday():
        penalty += weekday_weight

    if observed_time.month != candidate_time.month:
        penalty += month_weight

    yd_obs = observed_time.timetuple().tm_yday
    yd_cand = candidate_time.timetuple().tm_yday
    penalty += yearday_weight * float(day_of_year_distance(yd_obs, yd_cand))

    if observed_time.year != candidate_time.year:
        penalty += year_weight * float(abs(observed_time.year - candidate_time.year))

    return penalty


def combined_score(
    observed_time,
    candidate_time,
    weekday_weight,
    month_weight,
    yearday_weight,
    year_weight,
):
    return cycle_score(observed_time, candidate_time, include_signature_cycles=True) + calendar_penalty(
        observed_time=observed_time,
        candidate_time=candidate_time,
        weekday_weight=weekday_weight,
        month_weight=month_weight,
        yearday_weight=yearday_weight,
        year_weight=year_weight,
    )


def classify_state(abs_mean_residual_ms, residuals, soft_limit_ms, hard_limit_ms):
    hard_critical = []
    soft_critical = []

    for cycle_name, residual in residuals.items():
        abs_r = abs(residual)
        if cycle_name in CRITICAL_CYCLES:
            if abs_r > hard_limit_ms:
                hard_critical.append(cycle_name)
            elif abs_r > soft_limit_ms:
                soft_critical.append(cycle_name)

    if hard_critical:
        return "ABSTAIN", soft_critical, hard_critical

    if soft_critical:
        return "HOLDOVER", soft_critical, hard_critical

    if abs_mean_residual_ms <= 10.0:
        return "FULL_LOCK", soft_critical, hard_critical
    if abs_mean_residual_ms <= 100.0:
        return "GENERAL_TIME", soft_critical, hard_critical
    if abs_mean_residual_ms <= 1000.0:
        return "HOLDOVER", soft_critical, hard_critical
    return "ABSTAIN", soft_critical, hard_critical


def apply_hysteresis(previous_state, raw_state, healthy_streak, recovery_streak, promotion_threshold):
    promoted = False
    recovery_event = False

    if raw_state == "ABSTAIN":
        return "ABSTAIN", 0, 0, promoted, recovery_event

    if raw_state == "HOLDOVER":
        return "HOLDOVER", 0, 0, promoted, recovery_event

    if raw_state == "GENERAL_TIME":
        if previous_state == "HOLDOVER":
            recovery_streak += 1
            if recovery_streak >= promotion_threshold:
                recovery_event = True
                return "GENERAL_TIME", 0, 0, promoted, recovery_event
            return "HOLDOVER", 0, recovery_streak, promoted, recovery_event
        return "GENERAL_TIME", 0, 0, promoted, recovery_event

    if raw_state == "FULL_LOCK":
        if previous_state == "FULL_LOCK":
            return "FULL_LOCK", healthy_streak + 1, 0, promoted, recovery_event

        if previous_state in {"GENERAL_TIME", "HOLDOVER"}:
            healthy_streak += 1
            if healthy_streak >= promotion_threshold:
                promoted = True
                recovery_event = previous_state == "HOLDOVER"
                return "FULL_LOCK", 0, 0, promoted, recovery_event

            if previous_state == "HOLDOVER":
                return "HOLDOVER", healthy_streak, healthy_streak, promoted, recovery_event
            return "GENERAL_TIME", healthy_streak, 0, promoted, recovery_event

    return raw_state, healthy_streak, recovery_streak, promoted, recovery_event


def format_residuals(residuals):
    ordered = [
        ("second", "sec"),
        ("minute", "min"),
        ("hour", "hr"),
        ("day", "day"),
        ("week", "wk"),
        ("month", "mon"),
        ("year", "yr"),
    ]
    parts = []
    for key, label in ordered:
        if key in residuals:
            parts.append(f"{label}={residuals[key]:.1f}ms")
    return " ".join(parts)


def append_log(log_file, line):
    if not log_file:
        return
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def write_json_state(json_state_file, payload):
    if not json_state_file:
        return
    path = Path(json_state_file)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    tmp.replace(path)


def uptime_string(seconds_total):
    seconds_total = int(seconds_total)
    hours = seconds_total // 3600
    minutes = (seconds_total % 3600) // 60
    seconds = seconds_total % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def canonical_sha256(payload):
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_capsule(capsule_file):
    path = Path(capsule_file)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    capsule_hash = data.get("capsule_sha256", "")
    payload = dict(data)
    payload.pop("capsule_sha256", None)

    expected_hash = canonical_sha256(payload)
    if capsule_hash != expected_hash:
        raise ValueError("Capsule hash verification failed")

    return data


def write_capsule(capsule_file, payload):
    path = Path(capsule_file)
    tmp = path.with_suffix(path.suffix + ".tmp")

    data = dict(payload)
    data["capsule_sha256"] = canonical_sha256(data)

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def build_capsule_payload(
    internal_time,
    display_state,
    post_residual_ms,
    residuals,
    iteration,
    uptime_sec,
    acquire_count,
    holdover_count,
    abstain_count,
    promote_count,
    recovery_event_count,
    healthy_streak,
    recovery_streak,
    startup_source,
    observation_mode,
    free_run_active,
    drift_ppm,
):
    wall_unix = time.time()
    phase_vector = cycle_phase_vector(internal_time)

    payload = {
        "capsule_version": CAPSULE_VERSION,
        "saved_at_wall_unix": round(wall_unix, 6),
        "saved_at_wall_time": dt.datetime.fromtimestamp(wall_unix).strftime("%Y-%m-%d %H:%M:%S"),
        "last_verified_time": internal_time.strftime("%Y-%m-%d %H:%M:%S"),
        "display_state": display_state,
        "post_residual_ms": round(post_residual_ms, 3),
        "residuals_ms": {k: round(v, 3) for k, v in residuals.items()},
        "phase_vector": {k: round(v, 12) for k, v in phase_vector.items()},
        "day_of_week": internal_time.weekday(),
        "day_of_year": internal_time.timetuple().tm_yday,
        "month": internal_time.month,
        "year": internal_time.year,
        "iteration": iteration,
        "uptime": uptime_string(uptime_sec),
        "acquire_count": acquire_count,
        "holdover_count": holdover_count,
        "abstain_count": abstain_count,
        "promote_count": promote_count,
        "recovery_event_count": recovery_event_count,
        "healthy_streak": healthy_streak,
        "recovery_streak": recovery_streak,
        "startup_source": startup_source,
        "observation_mode": observation_mode,
        "free_run_active": int(bool(free_run_active)),
        "simulate_drift_ppm": round(float(drift_ppm), 6),
    }
    return payload


def should_save_capsule(display_state, post_residual_ms, capsule_save_states, capsule_max_residual_ms):
    if display_state not in capsule_save_states:
        return False
    if abs(post_residual_ms) > capsule_max_residual_ms:
        return False
    return True


def acquire_time_local(observed_time, internal_time, search_window_sec, search_step_ms):
    best_time = internal_time
    best_score = None

    total_steps = int((2 * search_window_sec * 1000) / search_step_ms) + 1
    start_ms = -search_window_sec * 1000

    for i in range(total_steps):
        offset_ms = start_ms + i * search_step_ms
        candidate = internal_time + dt.timedelta(milliseconds=offset_ms)
        score = cycle_score(observed_time, candidate, include_signature_cycles=False)

        if best_score is None or score < best_score:
            best_score = score
            best_time = candidate

    return best_time


def acquire_time_long_horizon(
    observed_time,
    internal_time,
    day_window,
    coarse_day_step,
    hour_radius,
    minute_radius,
    minute_step_sec,
    second_radius_ms,
    second_step_ms,
    weekday_weight,
    month_weight,
    yearday_weight,
    year_weight,
):
    best_time = internal_time
    best_score = combined_score(
        observed_time=observed_time,
        candidate_time=best_time,
        weekday_weight=weekday_weight,
        month_weight=month_weight,
        yearday_weight=yearday_weight,
        year_weight=year_weight,
    )

    best_day_offset = 0

    for d in range(-int(day_window), int(day_window) + 1, max(1, int(coarse_day_step))):
        candidate = internal_time + dt.timedelta(days=d)
        score = combined_score(
            observed_time=observed_time,
            candidate_time=candidate,
            weekday_weight=weekday_weight,
            month_weight=month_weight,
            yearday_weight=yearday_weight,
            year_weight=year_weight,
        )
        if score < best_score:
            best_score = score
            best_time = candidate
            best_day_offset = d

    center = best_time
    for h in range(-int(hour_radius), int(hour_radius) + 1):
        candidate = center + dt.timedelta(hours=h)
        score = combined_score(
            observed_time=observed_time,
            candidate_time=candidate,
            weekday_weight=weekday_weight,
            month_weight=month_weight,
            yearday_weight=yearday_weight,
            year_weight=year_weight,
        )
        if score < best_score:
            best_score = score
            best_time = candidate

    center = best_time
    step_sec = max(1, int(minute_step_sec))
    minute_span_sec = int(minute_radius) * 60
    for s in range(-minute_span_sec, minute_span_sec + 1, step_sec):
        candidate = center + dt.timedelta(seconds=s)
        score = combined_score(
            observed_time=observed_time,
            candidate_time=candidate,
            weekday_weight=weekday_weight,
            month_weight=month_weight,
            yearday_weight=yearday_weight,
            year_weight=year_weight,
        )
        if score < best_score:
            best_score = score
            best_time = candidate

    center = best_time
    step_ms = max(1, int(second_step_ms))
    ms_radius = int(second_radius_ms)
    for ms in range(-ms_radius, ms_radius + 1, step_ms):
        candidate = center + dt.timedelta(milliseconds=ms)
        score = combined_score(
            observed_time=observed_time,
            candidate_time=candidate,
            weekday_weight=weekday_weight,
            month_weight=month_weight,
            yearday_weight=yearday_weight,
            year_weight=year_weight,
        )
        if score < best_score:
            best_score = score
            best_time = candidate

    return best_time, best_score, best_day_offset


def resolve_start_time(args):
    if args.anchor.strip():
        return parse_time(args.anchor.strip()), "anchor", 0.0, None

    if args.capsule_file.strip():
        try:
            capsule = load_capsule(args.capsule_file.strip())
        except Exception:
            capsule = None

        if capsule is not None:
            last_verified_time = parse_time(capsule["last_verified_time"])

            if args.capsule_replay_elapsed_sec is not None:
                elapsed_sec = max(0.0, float(args.capsule_replay_elapsed_sec))
                source = "capsule_replay"
            else:
                saved_wall_unix = float(capsule.get("saved_at_wall_unix", 0.0))
                elapsed_sec = max(0.0, time.time() - saved_wall_unix)
                source = "capsule"

            resumed_time = last_verified_time + dt.timedelta(seconds=elapsed_sec)
            return resumed_time, source, elapsed_sec, capsule

    return dt.datetime.now(), "system_now", 0.0, None


def build_observed_time_provider(args, startup_elapsed_sec):
    shifted_delta = dt.timedelta(seconds=startup_elapsed_sec)

    def observed_time_host():
        return dt.datetime.now()

    def observed_time_shifted():
        return dt.datetime.now() + shifted_delta

    def observed_time_reentry():
        if args.host_hint_time.strip():
            return parse_time(args.host_hint_time.strip())
        if args.capsule_replay_elapsed_sec is not None:
            return dt.datetime.now() + shifted_delta
        return dt.datetime.now()

    if args.observation_mode == "host_assist":
        return observed_time_host
    if args.observation_mode == "shifted_sim":
        return observed_time_shifted
    if args.observation_mode == "continuity_reentry":
        return observed_time_reentry
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--anchor", default="")
    parser.add_argument("--tick-ms", type=int, default=1000)
    parser.add_argument("--soft-limit-ms", type=float, default=100.0)
    parser.add_argument("--hard-limit-ms", type=float, default=2500.0)
    parser.add_argument("--max-correction-ms", type=float, default=250.0)
    parser.add_argument("--promotion-threshold", type=int, default=3)
    parser.add_argument("--acquire-threshold-ms", type=float, default=5000.0)
    parser.add_argument("--acquire-window-sec", type=float, default=600.0)
    parser.add_argument("--acquire-step-ms", type=float, default=500.0)

    parser.add_argument("--mode", choices=["compact", "detailed"], default="detailed")
    parser.add_argument("--summary-interval", type=int, default=30)
    parser.add_argument("--max-iterations", type=int, default=0)
    parser.add_argument("--log-file", default="")
    parser.add_argument("--json-state-file", default="")

    parser.add_argument("--capsule-file", default="")
    parser.add_argument("--capsule-save-states", default="FULL_LOCK,GENERAL_TIME")
    parser.add_argument("--capsule-max-residual-ms", type=float, default=100.0)
    parser.add_argument("--capsule-replay-elapsed-sec", type=float, default=None)

    parser.add_argument(
        "--observation-mode",
        choices=["host_assist", "shifted_sim", "continuity_reentry", "free_run"],
        default="host_assist",
    )
    parser.add_argument("--host_hint_time", default="")
    parser.add_argument("--simulate-drift-ppm", type=float, default=0.0)

    parser.add_argument("--reentry-day-window", type=int, default=240)
    parser.add_argument("--reentry-coarse-day-step", type=int, default=1)
    parser.add_argument("--reentry-hour-radius", type=int, default=24)
    parser.add_argument("--reentry-minute-radius", type=int, default=120)
    parser.add_argument("--reentry-minute-step-sec", type=int, default=60)
    parser.add_argument("--reentry-second-radius-ms", type=int, default=60000)
    parser.add_argument("--reentry-second-step-ms", type=int, default=250)

    parser.add_argument("--calendar-weekday-weight", type=float, default=5.0e11)
    parser.add_argument("--calendar-month-weight", type=float, default=2.0e11)
    parser.add_argument("--calendar-yearday-weight", type=float, default=1.0e10)
    parser.add_argument("--calendar-year-weight", type=float, default=8.0e11)

    args = parser.parse_args()

    capsule_save_states = {x.strip() for x in args.capsule_save_states.split(",") if x.strip()}

    internal_time, startup_source, startup_elapsed_sec, startup_capsule = resolve_start_time(args)
    observed_time_provider = build_observed_time_provider(args, startup_elapsed_sec)

    drift_factor = 1.0 + (float(args.simulate_drift_ppm) / 1000000.0)
    free_run_active = args.observation_mode == "free_run"

    mono_prev = time.monotonic()
    start_mono = mono_prev

    healthy_streak = 0
    recovery_streak = 0
    current_state = "GENERAL_TIME"

    iteration = 0
    acquire_count = 0
    holdover_count = 0
    abstain_count = 0
    promote_count = 0
    recovery_event_count = 0
    capsule_write_count = 0
    last_reentry_score = None
    last_reentry_day_offset = None

    if startup_capsule is not None:
        acquire_count = int(startup_capsule.get("acquire_count", 0))
        holdover_count = int(startup_capsule.get("holdover_count", 0))
        abstain_count = int(startup_capsule.get("abstain_count", 0))
        promote_count = int(startup_capsule.get("promote_count", 0))
        recovery_event_count = int(startup_capsule.get("recovery_event_count", 0))

    print("SSUM-Time Live Clock Engine v8.1")
    print("Press Ctrl+C to stop.")
    print()
    print(
        f"STARTUP | source={startup_source} | "
        f"observation_mode={args.observation_mode} | "
        f"internal_time={internal_time.strftime('%Y-%m-%d %H:%M:%S')} | "
        f"elapsed={startup_elapsed_sec:.1f}s | "
        f"drift_ppm={args.simulate_drift_ppm:.3f} | "
        f"free_run={int(free_run_active)}"
    )
    print()

    try:
        while True:
            iteration += 1

            mono_now = time.monotonic()
            elapsed_sec_raw = mono_now - mono_prev
            mono_prev = mono_now
            elapsed_sec_effective = elapsed_sec_raw * drift_factor

            predicted_time = internal_time + dt.timedelta(seconds=elapsed_sec_effective)

            if free_run_active:
                observed_time = predicted_time
                mean_residual_ms = 0.0
                residuals = {name: 0.0 for name in CYCLE_PERIOD_DAYS}
                governance_mean_residual_ms = 0.0
                governance_residuals = {name: 0.0 for name in GOVERNANCE_CYCLES}
                raw_state = "GENERAL_TIME"
                soft_critical = []
                hard_critical = []
                acquired = False
                last_reentry_score = None
                last_reentry_day_offset = None
                corrected_time = predicted_time
                applied_correction_ms = 0.0
                post_mean_residual_ms = 0.0
                post_residuals = {name: 0.0 for name in CYCLE_PERIOD_DAYS}
                post_governance_mean_residual_ms = 0.0
                post_governance_residuals = {name: 0.0 for name in GOVERNANCE_CYCLES}
                post_raw_state = "GENERAL_TIME"
            else:
                observed_time = observed_time_provider()
                mean_residual_ms, residuals = weighted_residual_ms(observed_time, predicted_time)

                acquired = False
                if abs(mean_residual_ms) > args.acquire_threshold_ms:
                    if args.observation_mode == "continuity_reentry":
                        acquired_time, last_reentry_score, last_reentry_day_offset = acquire_time_long_horizon(
                            observed_time=observed_time,
                            internal_time=predicted_time,
                            day_window=args.reentry_day_window,
                            coarse_day_step=args.reentry_coarse_day_step,
                            hour_radius=args.reentry_hour_radius,
                            minute_radius=args.reentry_minute_radius,
                            minute_step_sec=args.reentry_minute_step_sec,
                            second_radius_ms=args.reentry_second_radius_ms,
                            second_step_ms=args.reentry_second_step_ms,
                            weekday_weight=args.calendar_weekday_weight,
                            month_weight=args.calendar_month_weight,
                            yearday_weight=args.calendar_yearday_weight,
                            year_weight=args.calendar_year_weight,
                        )
                    else:
                        acquired_time = acquire_time_local(
                            observed_time=observed_time,
                            internal_time=predicted_time,
                            search_window_sec=args.acquire_window_sec,
                            search_step_ms=args.acquire_step_ms,
                        )
                        last_reentry_score = None
                        last_reentry_day_offset = None

                    acquired = True
                    acquire_count += 1
                    predicted_time = acquired_time
                    mean_residual_ms, residuals = weighted_residual_ms(observed_time, predicted_time)

                governance_mean_residual_ms, governance_residuals = governance_residual_ms(observed_time, predicted_time)

                raw_state, soft_critical, hard_critical = classify_state(
                    abs_mean_residual_ms=abs(governance_mean_residual_ms),
                    residuals=governance_residuals,
                    soft_limit_ms=args.soft_limit_ms,
                    hard_limit_ms=args.hard_limit_ms,
                )

                bounded_correction_ms = max(
                    -args.max_correction_ms,
                    min(args.max_correction_ms, governance_mean_residual_ms),
                )

                if raw_state == "ABSTAIN" and not acquired:
                    corrected_time = predicted_time
                    applied_correction_ms = 0.0
                else:
                    corrected_time = predicted_time + dt.timedelta(milliseconds=bounded_correction_ms)
                    applied_correction_ms = bounded_correction_ms

                post_mean_residual_ms, post_residuals = weighted_residual_ms(observed_time, corrected_time)
                post_governance_mean_residual_ms, post_governance_residuals = governance_residual_ms(observed_time, corrected_time)

                post_raw_state, soft_critical, hard_critical = classify_state(
                    abs_mean_residual_ms=abs(post_governance_mean_residual_ms),
                    residuals=post_governance_residuals,
                    soft_limit_ms=args.soft_limit_ms,
                    hard_limit_ms=args.hard_limit_ms,
                )

            promoted = False
            recovery_event = False

            if free_run_active:
                display_state = "GENERAL_TIME"
                current_state = "GENERAL_TIME"
                healthy_streak = 0
                recovery_streak = 0
            elif acquired and post_raw_state != "ABSTAIN":
                display_state = "ACQUIRE"
                current_state = "GENERAL_TIME"
                healthy_streak = 0
                recovery_streak = 0
            else:
                display_state, healthy_streak, recovery_streak, promoted, recovery_event = apply_hysteresis(
                    previous_state=current_state,
                    raw_state=post_raw_state,
                    healthy_streak=healthy_streak,
                    recovery_streak=recovery_streak,
                    promotion_threshold=args.promotion_threshold,
                )
                current_state = display_state

            if display_state == "HOLDOVER":
                holdover_count += 1
            if display_state == "ABSTAIN":
                abstain_count += 1
            if promoted:
                promote_count += 1
            if recovery_event:
                recovery_event_count += 1

            internal_time = corrected_time
            uptime_sec = time.monotonic() - start_mono

            if args.mode == "compact":
                line = (
                    f"{internal_time.strftime('%Y-%m-%d %H:%M:%S')} | "
                    f"{display_state} | "
                    f"res={post_governance_mean_residual_ms:.1f}ms | "
                    f"drift={args.simulate_drift_ppm:.1f}ppm | "
                    f"uptime={uptime_string(uptime_sec)}"
                )
            else:
                extra = ""
                if args.observation_mode == "continuity_reentry" and last_reentry_score is not None:
                    extra = f" | reentry_score={last_reentry_score:.3e} | day_offset={last_reentry_day_offset}"
                if free_run_active:
                    extra += " | free_run=1"
                line = (
                    f"{internal_time.strftime('%Y-%m-%d %H:%M:%S')} | "
                    f"state={display_state} | "
                    f"obs_residual={governance_mean_residual_ms:.1f}ms | "
                    f"applied={applied_correction_ms:.1f}ms | "
                    f"post_residual={post_governance_mean_residual_ms:.1f}ms | "
                    f"soft={len(soft_critical)} | "
                    f"hard={len(hard_critical)} | "
                    f"drift={args.simulate_drift_ppm:.1f}ppm | "
                    f"uptime={uptime_string(uptime_sec)} | "
                    f"{format_residuals(post_residuals)}"
                    f"{extra}"
                )

            print(line, flush=True)
            append_log(args.log_file, line)

            if iteration % max(1, args.summary_interval) == 0:
                capsule_saved = False

                if args.capsule_file.strip():
                    if should_save_capsule(
                        display_state=display_state,
                        post_residual_ms=post_governance_mean_residual_ms,
                        capsule_save_states=capsule_save_states,
                        capsule_max_residual_ms=args.capsule_max_residual_ms,
                    ):
                        capsule_payload = build_capsule_payload(
                            internal_time=internal_time,
                            display_state=display_state,
                            post_residual_ms=post_governance_mean_residual_ms,
                            residuals=post_residuals,
                            iteration=iteration,
                            uptime_sec=uptime_sec,
                            acquire_count=acquire_count,
                            holdover_count=holdover_count,
                            abstain_count=abstain_count,
                            promote_count=promote_count,
                            recovery_event_count=recovery_event_count,
                            healthy_streak=healthy_streak,
                            recovery_streak=recovery_streak,
                            startup_source=startup_source,
                            observation_mode=args.observation_mode,
                            free_run_active=free_run_active,
                            drift_ppm=args.simulate_drift_ppm,
                        )
                        write_capsule(args.capsule_file.strip(), capsule_payload)
                        capsule_write_count += 1
                        capsule_saved = True

                summary_line = (
                    f"SUMMARY | iter={iteration} | "
                    f"uptime={uptime_string(uptime_sec)} | "
                    f"state={display_state} | "
                    f"post_residual={post_governance_mean_residual_ms:.1f}ms | "
                    f"acquire={acquire_count} | "
                    f"holdover={holdover_count} | "
                    f"abstain={abstain_count} | "
                    f"promote={promote_count} | "
                    f"recovery={recovery_event_count} | "
                    f"drift_ppm={args.simulate_drift_ppm:.3f} | "
                    f"free_run={int(free_run_active)} | "
                    f"capsule_saved={int(capsule_saved)} | "
                    f"capsule_writes={capsule_write_count}"
                )
                print(summary_line, flush=True)
                append_log(args.log_file, summary_line)

                json_payload = {
                    "clock_time": internal_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "state": display_state,
                    "uptime": uptime_string(uptime_sec),
                    "iteration": iteration,
                    "elapsed_sec_raw": round(elapsed_sec_raw, 6),
                    "elapsed_sec_effective": round(elapsed_sec_effective, 6),
                    "obs_residual_ms": round(governance_mean_residual_ms, 3),
                    "post_residual_ms": round(post_governance_mean_residual_ms, 3),
                    "full_mean_residual_ms": round(post_mean_residual_ms, 3),
                    "soft_count": len(soft_critical),
                    "hard_count": len(hard_critical),
                    "acquire_count": acquire_count,
                    "holdover_count": holdover_count,
                    "abstain_count": abstain_count,
                    "promote_count": promote_count,
                    "recovery_event_count": recovery_event_count,
                    "healthy_streak": healthy_streak,
                    "recovery_streak": recovery_streak,
                    "capsule_write_count": capsule_write_count,
                    "startup_source": startup_source,
                    "startup_elapsed_sec": round(startup_elapsed_sec, 3),
                    "observation_mode": args.observation_mode,
                    "free_run_active": int(free_run_active),
                    "simulate_drift_ppm": round(float(args.simulate_drift_ppm), 6),
                    "observed_time": observed_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "last_reentry_score": last_reentry_score,
                    "last_reentry_day_offset": last_reentry_day_offset,
                    "residuals_ms": {k: round(v, 3) for k, v in post_residuals.items()},
                }
                write_json_state(args.json_state_file, json_payload)

            if args.max_iterations > 0 and iteration >= args.max_iterations:
                break

            time.sleep(max(0.0, args.tick_ms / 1000.0))

    except KeyboardInterrupt:
        pass
    finally:
        if args.capsule_file.strip():
            if should_save_capsule(
                display_state=current_state if not free_run_active else "GENERAL_TIME",
                post_residual_ms=0.0,
                capsule_save_states=capsule_save_states,
                capsule_max_residual_ms=args.capsule_max_residual_ms,
            ):
                try:
                    final_payload = build_capsule_payload(
                        internal_time=internal_time,
                        display_state=current_state if not free_run_active else "GENERAL_TIME",
                        post_residual_ms=0.0,
                        residuals={k: 0.0 for k in CYCLE_PERIOD_DAYS},
                        iteration=iteration,
                        uptime_sec=time.monotonic() - start_mono,
                        acquire_count=acquire_count,
                        holdover_count=holdover_count,
                        abstain_count=abstain_count,
                        promote_count=promote_count,
                        recovery_event_count=recovery_event_count,
                        healthy_streak=healthy_streak,
                        recovery_streak=recovery_streak,
                        startup_source=startup_source,
                        observation_mode=args.observation_mode,
                        free_run_active=free_run_active,
                        drift_ppm=args.simulate_drift_ppm,
                    )
                    write_capsule(args.capsule_file.strip(), final_payload)
                except Exception:
                    pass

        print()
        print("SSUM-Time clock stopped.")


if __name__ == "__main__":
    main()