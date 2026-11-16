import numpy as np
import types
import torch

from noise_frontend.frontend import NoiseFrontend, NoiseFrontendConfig


def test_clean_bypass_is_identical_float32():
    wave = np.array([0.1, -0.2, 0.3], dtype=np.float32)
    result = NoiseFrontend(snr_estimator=lambda *_: 25).process(wave, 16000)
    np.testing.assert_array_equal(result.wave, wave)
    assert result.mode == "bypass"
    assert result.wave.dtype == np.float32


def test_shape_dtype_and_input_immutable():
    wave = np.array([0.1, -0.2, 0.3], dtype=np.float64)
    before = wave.copy()
    frontend = NoiseFrontend(snr_estimator=lambda *_: 5,
                             enhancer=lambda x, *_: x * 0.5)
    result = frontend.process(wave, 16000)
    assert result.wave.dtype == np.float32 and result.wave.shape == wave.shape
    np.testing.assert_array_equal(wave, before)


def test_threshold_modes_and_too_noisy():
    for snr, mode, noisy in [(20, "bypass", False), (10, "moderate", False),
                             (9, "heavy", False), (-1, "heavy", True)]:
        frontend = NoiseFrontend(NoiseFrontendConfig(), snr_estimator=lambda *_: snr,
                                 enhancer=lambda x, *_: x.copy())
        result = frontend.process(np.ones(8, dtype=np.float32), 16000)
        assert result.mode == mode
        assert result.too_noisy is noisy


def test_disabled_bypasses_enhancer():
    frontend = NoiseFrontend(NoiseFrontendConfig(enabled=False),
                             snr_estimator=lambda *_: -5,
                             enhancer=lambda *_: (_ for _ in ()).throw(AssertionError()))
    assert frontend.process(np.ones(4), 16000).mode == "bypass"


def test_deepfilter_adapter_contract_is_mocked(monkeypatch):
    from noise_frontend import enhancer

    calls = {}

    class State:
        def sr(self):
            return 48000

    class Model:
        def to(self, device):
            calls["model_device"] = device
            return self

    def init_df():
        calls["device"] = __import__("os").environ["DEVICE"]
        calls["init_args"] = 0
        return Model(), State(), "mock-config"

    def df_enhance(model, state, audio, pad=True, atten_lim_db=None):
        calls["shape"] = tuple(audio.shape)
        calls["dtype"] = audio.dtype
        calls["pad"] = pad
        calls["atten"] = atten_lim_db
        return audio

    def resample(tensor, old_rate, new_rate):
        n = round(tensor.shape[-1] * new_rate / old_rate)
        return torch.nn.functional.interpolate(tensor.unsqueeze(1), size=n,
                                               mode="linear", align_corners=False).squeeze(1)

    fake_df = types.ModuleType("df")
    fake_df.__path__ = []
    fake_enhance_module = types.ModuleType("df.enhance")
    fake_enhance_module.init_df = init_df
    fake_enhance_module.enhance = df_enhance
    fake_audio = types.ModuleType("torchaudio")
    fake_functional = types.ModuleType("torchaudio.functional")
    fake_functional.resample = resample
    fake_audio.functional = fake_functional
    monkeypatch.setitem(__import__("sys").modules, "df", fake_df)
    monkeypatch.setitem(__import__("sys").modules, "df.enhance", fake_enhance_module)
    monkeypatch.setitem(__import__("sys").modules, "torchaudio", fake_audio)
    monkeypatch.setitem(__import__("sys").modules, "torchaudio.functional", fake_functional)
    monkeypatch.setattr(enhancer, "_MODELS", {})

    original = np.linspace(-0.2, 0.2, 101, dtype=np.float32)
    before = original.copy()
    actual = enhancer.enhance_wave(original, sr=16000, atten_lim_db=12, device="cpu")
    assert actual.shape == original.shape
    assert actual.dtype == np.float32
    np.testing.assert_array_equal(original, before)
    assert calls == {"device": "cpu", "init_args": 0, "model_device": "cpu", "shape": (1, 303),
                     "dtype": torch.float32, "pad": True, "atten": 12.0}
