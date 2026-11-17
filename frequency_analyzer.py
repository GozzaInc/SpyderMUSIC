"""Windowed FFT for SpyderMUSIC themes. No decoder or GUI dependencies."""
import numpy as np

SAMPLE_RATE = 48000
FFT_SAMPLES = 8192
BANDS = {"bass": (20, 250), "mids": (250, 4000), "highs": (4000, 16000)}

class FrequencyAnalyzer:
    """Analyse mono PCM at the current playhead. Return normalized, smoothed bands."""
    def __init__(self, sample_rate=SAMPLE_RATE, samples=FFT_SAMPLES):
        self.sample_rate = sample_rate
        self.samples = samples
        self.window = np.hanning(samples).astype(np.float32)
        self.freq = np.fft.rfftfreq(samples, 1 / sample_rate)
        self.levels = {name: 0.0 for name in BANDS}

    def reset(self):
        for name in self.levels:
            self.levels[name] = 0.0

    def update(self, pcm):
        if pcm is None or len(pcm) != self.samples:
            return self._smooth({name: 0.0 for name in BANDS})
        data = np.asarray(pcm, dtype=np.float32)
        if not np.all(np.isfinite(data)):
            return self._smooth({name: 0.0 for name in BANDS})
        # Hann-windowed, amplitude-normalized one-sided spectrum.
        magnitude = np.abs(np.fft.rfft(data * self.window)) * (2.0 / self.window.sum())
        result = {}
        for name, (low, high) in BANDS.items():
            region = magnitude[(self.freq >= low) & (self.freq < high)]
            rms = float(np.sqrt(np.mean(region ** 2))) if region.size else 0.0
            # Log response: roughly -65 dBFS to -20 dBFS band RMS mapped to 0..1.
            db = 20.0 * np.log10(max(rms, 1e-9))
            result[name] = float(np.clip((db + 65.0) / 45.0, 0.0, 1.0))
        return self._smooth(result)

    def _smooth(self, targets):
        for name, value in targets.items():
            previous = self.levels[name]
            self.levels[name] = previous + (0.55 if value > previous else 0.16) * (value - previous)
        return dict(self.levels)


class VisualAnalyzer:
    """Generate bounded display arrays from the same PCM window as Stage 14."""
    def __init__(self, sample_rate=SAMPLE_RATE, samples=FFT_SAMPLES):
        self.sample_rate = sample_rate
        self.samples = samples
        self.window = np.hanning(samples).astype(np.float32)
        self.frequencies = np.fft.rfftfreq(samples, 1 / sample_rate)
        self._spectrum = np.zeros(128, dtype=np.float32)
        self._waveform = np.zeros(1024, dtype=np.float32)
        self._band_weights = {}

    def update(self, pcm, bars=64, waveform_points=256):
        bars = max(8, min(128, int(bars)))
        waveform_points = max(32, min(1024, int(waveform_points)))
        if pcm is None or len(pcm) != self.samples:
            # Brief decoder underruns must not blank the visualizers.
            return {'spectrum': self._spectrum[:bars].copy(),
                    'waveform': self._waveform[:waveform_points].copy()}
        data = np.asarray(pcm, dtype=np.float32)
        if not np.all(np.isfinite(data)):
            return self.update(None, bars, waveform_points)
        magnitude = np.abs(np.fft.rfft(data * self.window)) * (2.0 / self.window.sum())
        edges = np.geomspace(25, min(18000, self.sample_rate * 0.48), bars + 1)
        # Each FFT bin represents a frequency interval, not a point. Integrate
        # its overlap with each display band so narrow bass bands never go empty.
        # Precompute the geometry because it is unchanged between UI frames.
        weights = self._band_weights.get(bars)
        if weights is None:
            df = self.sample_rate / self.samples
            bin_low = np.maximum(0.0, self.frequencies - df * 0.5)
            bin_high = self.frequencies + df * 0.5
            overlap = np.maximum(0.0, np.minimum(edges[1:, None], bin_high[None, :])
                                 - np.maximum(edges[:-1, None], bin_low[None, :]))
            weights = overlap / np.maximum(overlap.sum(axis=1, keepdims=True), 1e-12)
            self._band_weights[bars] = weights.astype(np.float32)
        power = magnitude.astype(np.float32) ** 2
        amplitude = np.sqrt(weights @ power)
        result = np.clip((20 * np.log10(np.maximum(amplitude, 1e-9)) + 75) / 60,
                         0, 1).astype(np.float32)
        previous = self._spectrum[:bars]
        self._spectrum[:bars] = previous + np.where(result > previous, .6, .22) * (result - previous)
        # Uniform decimation preserves actual signed PCM amplitudes, no synthetic wave.
        waveform = np.interp(np.linspace(0, len(data)-1, waveform_points),
                             np.arange(len(data)), data).astype(np.float32)
        self._waveform[:waveform_points] = np.clip(waveform, -1, 1)
        return {'spectrum': self._spectrum[:bars].copy(),
                'waveform': self._waveform[:waveform_points].copy()}
