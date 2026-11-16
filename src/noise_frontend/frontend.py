"""Conditional, whole-recording noise enhancement."""

from dataclasses import dataclass
import logging

import numpy as np

from .snr import estimate_snr_db

logger = logging.getLogger(__name__)


@dataclass
class NoiseFrontendConfig:
    clean_snr_db: float = 20.0
    moderate_atten_db: float = 10.0
    heavy_atten_db: float = 20.0
    too_noisy_snr_db: float = 0.0
    enabled: bool = True
    device: str = "cpu"


@dataclass
class FrontendResult:
    wave: np.ndarray
    snr_db: float
    mode: str
    too_noisy: bool


class NoiseFrontend:
    """Estimate SNR and gently enhance a complete 16 kHz recording."""

    def __init__(self, config: NoiseFrontendConfig | None = None, *,
                 snr_estimator=estimate_snr_db, enhancer=None):
        self.config = config or NoiseFrontendConfig()
        self.snr_estimator = snr_estimator
        self._enhancer = enhancer

    def process(self, wave: np.ndarray, sr: int) -> FrontendResult:
        if sr != 16000:
            raise ValueError("NoiseFrontend expects 16 kHz audio")
        source = np.asarray(wave)
        if source.ndim != 1:
            raise ValueError("wave must be mono")
        snr = float(self.snr_estimator(source, sr))
        mode = "bypass" if (not self.config.enabled or snr >= self.config.clean_snr_db) else (
            "moderate" if snr >= 10.0 else "heavy")
        too_noisy = snr < self.config.too_noisy_snr_db
        if mode == "bypass":
            output = source.astype(np.float32, copy=True)
        else:
            attenuation = self.config.moderate_atten_db if mode == "moderate" else self.config.heavy_atten_db
            if self._enhancer is None:
                from .enhancer import enhance_wave
                enhance_fn = enhance_wave
            else:
                enhance_fn = self._enhancer
            output = np.asarray(enhance_fn(source.astype(np.float32, copy=True), sr,
                                           attenuation, self.config.device), dtype=np.float32).reshape(-1)
            if output.size != source.size:
                output = output[:source.size]
                if output.size < source.size:
                    output = np.pad(output, (0, source.size - output.size))
        logger.info("Noise frontend mode=%s snr=%.1f dB too_noisy=%s", mode, snr, too_noisy)
        return FrontendResult(output, snr, mode, too_noisy)
