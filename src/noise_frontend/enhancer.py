"""Lazy DeepFilterNet adapter with 16/48 kHz resampling."""

import logging
import inspect
import os
from threading import Lock

import numpy as np

logger = logging.getLogger(__name__)
_MODELS = {}
_LOCK = Lock()


def _load_model(device: str):
    with _LOCK:
        if device not in _MODELS:
            try:
                from df.enhance import enhance, init_df
            except ImportError as exc:
                raise ImportError("Install requirements-noise.txt to use DeepFilterNet") from exc
            # DeepFilterNet 0.5.6's init_df has no device parameter; its
            # get_device() reads the DEVICE environment variable.
            os.environ["DEVICE"] = device
            model, df_state, suffix = init_df()
            model.to(device)
            logger.info("DeepFilterNet init_df returned model/state/suffix=%s; sample rate=%s",
                        suffix, df_state.sr())
            _MODELS[device] = (model, df_state, enhance)
        return _MODELS[device]


def enhance_wave(wave: np.ndarray, sr: int = 16000, atten_lim_db: float = 10.0,
                 device: str = "cpu") -> np.ndarray:
    """Enhance one recording and return audio at the original sample rate."""
    if sr != 16000:
        raise ValueError("Noise frontend expects 16 kHz audio")
    try:
        import torch
        import torchaudio.functional as AF
    except ImportError as exc:
        raise ImportError("Install torchaudio to resample for DeepFilterNet") from exc
    model, state, enhance = _load_model(device)
    model_sr = int(state.sr())
    if model_sr != 48000:
        raise RuntimeError(f"DeepFilterNet model sample rate is {model_sr}, expected 48000")
    original = np.asarray(wave, dtype=np.float32).reshape(-1)
    tensor = torch.from_numpy(original.copy()).unsqueeze(0)
    at48 = AF.resample(tensor, 16000, 48000)
    # Adapt to installed API after inspecting its actual signature. DeepFilterNet
    # releases expose pad for STFT boundary handling; some expose delay compensation.
    parameters = inspect.signature(enhance).parameters
    if "atten_lim_db" not in parameters:
        raise RuntimeError("Installed df.enhance.enhance has no atten_lim_db parameter")
    kwargs = {"atten_lim_db": float(atten_lim_db)}
    if "pad" in parameters:
        kwargs["pad"] = True
    if "compensate_delay" in parameters:
        kwargs["compensate_delay"] = True
    # In DeepFilterNet 0.5.6 pad=True compensates algorithmic delay. Its feature
    # helpers query the DEVICE setting at call time, so refresh it for cached models.
    os.environ["DEVICE"] = device
    result = enhance(model, state, at48, **kwargs)
    if isinstance(result, tuple):
        result = result[0]
    back = AF.resample(result.detach().cpu().to(torch.float32), 48000, 16000).reshape(-1).numpy()
    if back.size > original.size:
        back = back[:original.size]
    elif back.size < original.size:
        back = np.pad(back, (0, original.size - back.size))
    logger.debug("DeepFilterNet processed %d samples", original.size)
    return np.asarray(back, dtype=np.float32)
