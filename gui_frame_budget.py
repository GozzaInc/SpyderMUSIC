"""Low-overhead GUI pulse telemetry and adaptive throttling (no Qt dependency).

This is a load-shedding mechanism, not a claim that hardware rendering got
faster. Under CPU power limits, prioritize interactive controls and playback
over redundant spectrum/VU/backdrop refreshes. Never queue missed frames.
"""
import time


class GuiFrameBudget:
    INTERVALS_MS = (40,)  # A/B test: fixed audio pulse, no adaptive throttling

    def __init__(self):
        self.level = 0
        self.last_start = None
        self.samples = 0
        self.pressured = 0
        self.quiet_windows = 0
        self.duration_total_ms = 0.0
        self.duration_max_ms = 0.0
        self.paint_samples = 0
        self.paint_total_ms = 0.0
        self.paint_max_ms = 0.0
        self.last_report = time.monotonic()

    @property
    def interval_ms(self):
        return self.INTERVALS_MS[self.level]

    def note_paint(self, duration_s):
        value = max(0.0, duration_s * 1000.0)
        self.paint_samples += 1
        self.paint_total_ms += value
        self.paint_max_ms = max(self.paint_max_ms, value)

    def tick(self, start, finish):
        """Return (new_interval_or_None, periodic_telemetry_or_None).

        Compute lateness from actual GUI event-loop arrival times, so blocking
        paint/Qt work is included even if the callback itself is inexpensive.
        """
        current = self.interval_ms
        elapsed_ms = max(0.0, (finish - start) * 1000.0)
        lag_ms = ((start - self.last_start) * 1000.0
                  if self.last_start is not None else current)
        self.last_start = start
        self.duration_total_ms += elapsed_ms
        self.duration_max_ms = max(self.duration_max_ms, elapsed_ms)
        self.samples += 1
        if lag_ms > current * 1.35 or elapsed_ms > current * .60:
            self.pressured += 1

        changed = None
        if self.samples % 24 == 0:
            ratio = self.pressured / 24.0
            if ratio >= .35 and self.level < len(self.INTERVALS_MS) - 1:
                self.level += 1
                self.quiet_windows = 0
                changed = self.interval_ms
            elif ratio < .08 and self.level > 0:
                self.quiet_windows += 1
                if self.quiet_windows >= 5:
                    self.level -= 1
                    self.quiet_windows = 0
                    changed = self.interval_ms
            else:
                self.quiet_windows = 0
            self.pressured = 0

        report = None
        if finish - self.last_report >= 30.0:
            report = {
                'pulse_ms': self.interval_ms,
                'mean_callback_ms': self.duration_total_ms / max(1, self.samples),
                'max_callback_ms': self.duration_max_ms,
                'mean_paint_ms': self.paint_total_ms / max(1, self.paint_samples),
                'max_paint_ms': self.paint_max_ms,
                'paint_samples': self.paint_samples,
            }
            self.last_report = finish
            self.duration_total_ms = self.duration_max_ms = 0.0
            self.paint_total_ms = self.paint_max_ms = 0.0
            self.paint_samples = self.samples = self.pressured = 0
        return changed, report
