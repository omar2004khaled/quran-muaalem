import io
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
import torch

from quran_muaalem import inference
from quran_muaalem import noise_reduction


TEST_AUDIO = Path(__file__).parents[1] / "assets" / "test.wav"


def _snr_db(reference: np.ndarray, noise: np.ndarray) -> float:
    signal_power = np.mean(np.square(reference, dtype=np.float64))
    noise_power = np.mean(np.square(noise, dtype=np.float64))
    return 10 * np.log10(signal_power / noise_power)


def test_disabled_preprocessing_returns_original_waveform() -> None:
    waveform = np.linspace(-0.2, 0.2, 1000, dtype=np.float32)

    result = noise_reduction.preprocess_waveform(waveform, enabled=False)

    assert result is waveform


def test_clean_ambiguous_recording_is_left_unchanged() -> None:
    waveform, sample_rate = sf.read(TEST_AUDIO, dtype="float32")

    result = noise_reduction.conservative_wiener_filter(
        waveform, sample_rate=sample_rate
    )

    np.testing.assert_array_equal(result, waveform)


def test_stationary_noise_reduction_preserves_length_and_improves_snr() -> None:
    waveform, sample_rate = sf.read(TEST_AUDIO, dtype="float32")
    rng = np.random.default_rng(77123)
    noise = rng.standard_normal(waveform.size).astype(np.float32)
    signal_rms = np.sqrt(np.mean(np.square(waveform, dtype=np.float64)))
    noise_rms = np.sqrt(np.mean(np.square(noise, dtype=np.float64)))
    noise *= signal_rms / (10 ** (-5 / 20) * noise_rms)
    noisy = waveform + noise

    enhanced = noise_reduction.conservative_wiener_filter(
        noisy, sample_rate=sample_rate
    )

    assert enhanced.shape == noisy.shape
    assert enhanced.dtype == np.float32
    assert np.isfinite(enhanced).all()
    before_snr = _snr_db(waveform, noisy - waveform)
    after_snr = _snr_db(waveform, enhanced - waveform)
    assert after_snr > before_snr + 0.5


def test_processing_failure_falls_back_to_original(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    waveform = np.linspace(-0.1, 0.1, 1000, dtype=np.float32)

    def fail(*args, **kwargs):
        raise RuntimeError("test failure")

    monkeypatch.setattr(noise_reduction, "conservative_wiener_filter", fail)
    result = noise_reduction.preprocess_waveform(waveform, enabled=True)

    assert result is waveform
    assert "using the original waveform" in caplog.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("1", True),
        ("off", False),
        ("0", False),
        ("", False),
    ],
)
def test_environment_configuration(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("QURAN_MUAALEM_NOISE_REDUCTION", value)

    assert noise_reduction.noise_reduction_enabled_from_env() is expected


def test_invalid_environment_configuration_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QURAN_MUAALEM_NOISE_REDUCTION", "sometimes")

    with pytest.raises(ValueError, match="QURAN_MUAALEM_NOISE_REDUCTION"):
        noise_reduction.noise_reduction_enabled_from_env()


@pytest.mark.parametrize("enabled", [False, True])
def test_muaalem_applies_optional_preprocessing_before_feature_extraction(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    waveform = np.zeros(1024, dtype=np.float32)
    transformed = waveform + 0.25
    captured = {}

    def preprocess(wave, enabled, sample_rate):
        captured["enabled"] = enabled
        captured["sample_rate"] = sample_rate
        return transformed if enabled else wave

    class StopAfterFeatureExtraction(Exception):
        pass

    class Processor:
        def __call__(self, waves, **kwargs):
            captured["wave"] = waves[0]
            raise StopAfterFeatureExtraction

    class Tokenizer:
        def tokenize(self, *args, **kwargs):
            return {"input_ids": torch.zeros((1, 1), dtype=torch.long)}

    model = inference.Muaalem.__new__(inference.Muaalem)
    model.device = "cpu"
    model.dtype = torch.float32
    model.processor = Processor()
    model.multi_level_tokenizer = Tokenizer()
    reference = SimpleNamespace(phonemes="", sifat=[])
    monkeypatch.setattr(inference, "preprocess_waveform", preprocess)

    with pytest.raises(StopAfterFeatureExtraction):
        model(
            [waveform],
            [reference],
            sampling_rate=16_000,
            noise_reduction=enabled,
        )

    assert captured["enabled"] is enabled
    assert captured["sample_rate"] == 16_000
    assert captured["wave"] is (transformed if enabled else waveform)


@pytest.mark.parametrize("enabled", [False, True])
def test_engine_applies_configured_preprocessing_before_feature_extraction(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    pytest.importorskip("litserve")
    from quran_muaalem.engine import serve as engine_serve

    waveform = np.zeros(1024, dtype=np.float32)
    transformed = waveform + 0.25
    captured = {}

    def load(*args, **kwargs):
        return waveform, 16_000

    def preprocess(wave, enabled, sample_rate):
        captured["enabled"] = enabled
        captured["sample_rate"] = sample_rate
        return transformed if enabled else wave

    class Processor:
        def __call__(self, wave, **kwargs):
            captured["wave"] = wave
            return {
                "input_features": torch.zeros((1, 1)),
                "attention_mask": torch.ones((1, 1)),
            }

    api = engine_serve.QuranMuaalemAPI.__new__(engine_serve.QuranMuaalemAPI)
    api.sampling_rate = 16_000
    api.max_audio_seconds = 1
    api.max_features = 1
    api.noise_reduction_enabled = enabled
    api.processor = Processor()
    monkeypatch.setattr(engine_serve.librosa, "load", load)
    monkeypatch.setattr(engine_serve, "preprocess_waveform", preprocess)

    result = api.decode_request(
        SimpleNamespace(file=io.BytesIO(b"audio payload"))
    )

    assert captured["enabled"] is enabled
    assert captured["sample_rate"] == 16_000
    assert captured["wave"] is (transformed if enabled else waveform)
    assert result["input_features"].shape == (1, 1)
