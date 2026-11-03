"""Deterministic, sampled growth metrics.

Only server observations and locally observed events are counted. Time spent
disconnected is excluded by breaking the rate denominators at gaps larger than
``max_gap_seconds``. Percentiles use the nearest-rank definition, which is
stable across platforms and easy to verify.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


def percentile(values, percent: float) -> float | None:
    """Nearest-rank percentile; returns ``None`` for an empty sample."""
    sample = sorted(float(value) for value in values)
    if not sample:
        return None
    if percent <= 0:
        return sample[0]
    if percent >= 100:
        return sample[-1]
    rank = math.ceil(percent / 100.0 * len(sample)) - 1
    return sample[max(0, min(rank, len(sample) - 1))]


@dataclass
class MetricsWindow:
    window_seconds: float = 900.0
    max_gap_seconds: float = 120.0
    sample_limit: int = 500
    kill_sample_limit: int = 500
    observations: list[dict] = field(default_factory=list)
    kill_samples: list[dict] = field(default_factory=list)
    _earned_xp: float = 0.0
    _progress: tuple[int, int, int] | None = None

    # -------------------------------------------------------------- sampling

    def observe(
        self,
        now: float,
        *,
        level: int,
        xp: int,
        next_level_xp: int,
        kills: int = 0,
        deaths: int = 0,
        stuck_events: int = 0,
    ) -> None:
        level = int(level)
        xp = int(xp)
        next_level_xp = int(next_level_xp)
        delta = 0
        if self._progress is not None:
            previous_level, previous_xp, previous_next = self._progress
            if level == previous_level and xp >= previous_xp:
                delta = xp - previous_xp
            elif level == previous_level + 1:
                delta = max(0, previous_next - previous_xp) + max(0, xp)
            # A larger jump cannot be attributed honestly; count zero.
        self._earned_xp += delta
        self._progress = (level, xp, next_level_xp)
        self.observations.append({
            "t": float(now),
            "earned": float(self._earned_xp),
            "kills": int(kills),
            "deaths": int(deaths),
            "stuck": int(stuck_events),
        })
        self._prune_observations(float(now))

    def record_kill(self, now: float, duration_seconds: float) -> None:
        self.kill_samples.append({
            "t": float(now), "duration": float(max(0.0, duration_seconds))})
        if len(self.kill_samples) > self.kill_sample_limit:
            del self.kill_samples[:len(self.kill_samples) - self.kill_sample_limit]

    def _prune_observations(self, now: float) -> None:
        cutoff = now - float(self.window_seconds)
        self.observations = [
            sample for sample in self.observations if sample["t"] >= cutoff]
        if len(self.observations) > self.sample_limit:
            del self.observations[:len(self.observations) - self.sample_limit]

    # -------------------------------------------------------------- snapshot

    def snapshot(self, now: float | None = None) -> dict:
        if now is None:
            now = self.observations[-1]["t"] if self.observations else 0.0
        cutoff = float(now) - float(self.window_seconds)
        samples = [s for s in self.observations if s["t"] >= cutoff]
        active_seconds = 0.0
        xp_delta = 0.0
        kills_delta = 0
        deaths_delta = 0
        stuck_delta = 0
        for previous, current in zip(samples, samples[1:]):
            gap = current["t"] - previous["t"]
            if 0 < gap <= self.max_gap_seconds:
                active_seconds += gap
                xp_delta += current["earned"] - previous["earned"]
                kills_delta += (int(current.get("kills") or 0)
                                - int(previous.get("kills") or 0))
                deaths_delta += (int(current.get("deaths") or 0)
                                 - int(previous.get("deaths") or 0))
                stuck_delta += (int(current.get("stuck") or 0)
                                - int(previous.get("stuck") or 0))

        hours = active_seconds / 3600.0
        if hours > 0:
            xp_per_hour = xp_delta / hours
            death_rate = deaths_delta / hours
            stuck_rate = stuck_delta / hours
            kills_per_hour = kills_delta / hours
        else:
            xp_per_hour = death_rate = stuck_rate = kills_per_hour = None

        durations = [
            sample["duration"] for sample in self.kill_samples
            if sample["t"] >= cutoff
        ]
        last = samples[-1] if samples else None
        return {
            "xp_per_hour": xp_per_hour,
            "kills_per_hour": kills_per_hour,
            "death_rate": death_rate,
            "stuck_rate": stuck_rate,
            "kill_time_p50": percentile(durations, 50),
            "kill_time_p90": percentile(durations, 90),
            "active_seconds": active_seconds,
            "kills": int(last["kills"]) if last else 0,
            "deaths": int(last["deaths"]) if last else 0,
            "stuck_events": int(last["stuck"]) if last else 0,
            "kill_samples": len(durations),
        }

    # ------------------------------------------------------------ persistence

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "window_seconds": float(self.window_seconds),
            "max_gap_seconds": float(self.max_gap_seconds),
            "sample_limit": int(self.sample_limit),
            "kill_sample_limit": int(self.kill_sample_limit),
            "observations": list(self.observations),
            "kill_samples": list(self.kill_samples),
            "earned_xp": float(self._earned_xp),
            "progress": list(self._progress) if self._progress else None,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "MetricsWindow":
        if not isinstance(data, dict):
            return cls()
        window = cls(
            window_seconds=float(data.get("window_seconds", 900.0)),
            max_gap_seconds=float(data.get("max_gap_seconds", 120.0)),
            sample_limit=int(data.get("sample_limit", 500)),
            kill_sample_limit=int(data.get("kill_sample_limit", 500)),
        )
        observations = data.get("observations")
        if isinstance(observations, list):
            window.observations = [
                sample for sample in observations
                if isinstance(sample, dict) and "t" in sample and "earned" in sample
            ][-window.sample_limit:]
        kill_samples = data.get("kill_samples")
        if isinstance(kill_samples, list):
            window.kill_samples = [
                sample for sample in kill_samples
                if isinstance(sample, dict) and "t" in sample and "duration" in sample
            ][-window.kill_sample_limit:]
        try:
            window._earned_xp = float(data.get("earned_xp", 0.0))
        except (TypeError, ValueError):
            window._earned_xp = 0.0
        progress = data.get("progress")
        if isinstance(progress, list) and len(progress) == 3:
            try:
                window._progress = (int(progress[0]), int(progress[1]),
                                    int(progress[2]))
            except (TypeError, ValueError):
                window._progress = None
        return window
