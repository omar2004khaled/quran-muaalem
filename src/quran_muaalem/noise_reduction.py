import logging
import os

import numpy as np
import torch
import torch.nn.functional as F


logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
FFT_SIZE = 512
WINDOW_SIZE = 400
HOP_SIZE = 160
NOISE_WINDOW_FRAMES = 51
MIN_NOISE_RATIO = 0.10
MAX_NOISE_FLOOR_VARIATION = 1.10
MINIMUM_GAIN = 0.85
NOISE_ESTIMATE_SCALE = 0.8
SUPPRESSION_STRENGTH = 0.75


def noise_reduction_enabled_from_env() -> bool:
    value = os.environ.get("QURAN_MUAALEM_NOISE_REDUCTION", "false").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(
        "QURAN_MUAALEM_NOISE_REDUCTION must be one of "
        "1/true/yes/on or 0/false/no/off"
    )


def conservative_wiener_filter(
    waveform: np.ndarray | torch.Tensor,
    sample_rate: int = SAMPLE_RATE,
) -> np.ndarray:
    """Apply a mild minimum-statistics Wiener mask to stationary background noise."""
    if sample_rate != SAMPLE_RATE:
        raise ValueError(f"Expected {SAMPLE_RATE} Hz audio, got {sample_rate} Hz")

    if isinstance(waveform, torch.Tensor):
        waveform = waveform.detach().to(device="cpu", dtype=torch.float32).numpy()
    wave = np.asarray(waveform, dtype=np.float32)
    if wave.ndim != 1 or wave.size < FFT_SIZE or not np.isfinite(wave).all():
        raise ValueError("Expected finite mono audio with at least 512 samples")

    with torch.inference_mode():
        audio = torch.from_numpy(np.ascontiguousarray(wave))
        window = torch.hann_window(WINDOW_SIZE, dtype=audio.dtype)
        spectrum = torch.stft(
            audio,
            n_fft=FFT_SIZE,
            hop_length=HOP_SIZE,
            win_length=WINDOW_SIZE,
            window=window,
            center=True,
            pad_mode="reflect",
            return_complex=True,
        )
        power = spectrum.abs().square().clamp_min(1e-12)
        smoothed_power = F.avg_pool1d(
            power.unsqueeze(0),
            kernel_size=5,
            stride=1,
            padding=2,
            count_include_pad=False,
        )[0]
        noise_floor = -F.max_pool1d(
            -smoothed_power.unsqueeze(0),
            kernel_size=NOISE_WINDOW_FRAMES,
            stride=1,
            padding=NOISE_WINDOW_FRAMES // 2,
        )[0]
        noise_ratio = noise_floor.mean() / power.mean().clamp_min(1e-12)
        frequency_means = noise_floor.mean(dim=-1)
        frequency_variation = noise_floor.std(dim=-1, unbiased=False) / (
            frequency_means + 1e-12
        )
        weighted_variation = (
            (frequency_variation * frequency_means).sum()
            / frequency_means.sum().clamp_min(1e-12)
        )

        if (
            noise_ratio.item() < MIN_NOISE_RATIO
            or weighted_variation.item() > MAX_NOISE_FLOOR_VARIATION
        ):
            return wave

        wiener_gain = torch.sqrt(
            power / (power + NOISE_ESTIMATE_SCALE * noise_floor).clamp_min(1e-12)
        )
        gain = 1.0 - SUPPRESSION_STRENGTH * (1.0 - wiener_gain)
        gain = F.avg_pool2d(
            F.pad(gain[None, None], (1, 1, 1, 1), mode="replicate"),
            kernel_size=3,
            stride=1,
        )[0, 0].clamp(min=MINIMUM_GAIN, max=1.0)
        enhanced = torch.istft(
            spectrum * gain,
            n_fft=FFT_SIZE,
            hop_length=HOP_SIZE,
            win_length=WINDOW_SIZE,
            window=window,
            center=True,
            length=wave.size,
        )

    result = enhanced.cpu().numpy().astype(np.float32, copy=False)
    if result.shape != wave.shape or not np.isfinite(result).all():
        raise ValueError("Noise reduction produced invalid audio")
    return result


def preprocess_waveform(
    waveform: np.ndarray | torch.Tensor | list[float],
    enabled: bool | None = None,
    sample_rate: int = SAMPLE_RATE,
) -> np.ndarray | torch.Tensor | list[float]:
    """Use the original waveform whenever reduction is disabled or fails."""
    if enabled is None:
        enabled = noise_reduction_enabled_from_env()
    if not enabled:
        return waveform

    try:
        return conservative_wiener_filter(waveform, sample_rate=sample_rate)
    except Exception:
        logger.warning(
            "Noise reduction failed; using the original waveform",
            exc_info=True,
        )
        return waveform
