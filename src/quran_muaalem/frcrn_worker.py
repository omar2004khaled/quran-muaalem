import argparse
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from clearvoice import ClearVoice


SAMPLE_RATE = 16_000
EDGE_PADDING_SAMPLES = SAMPLE_RATE // 4


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_path", type=Path)
    parser.add_argument("output_path", type=Path)
    args = parser.parse_args()

    checkpoint_dir = Path.cwd() / "checkpoints" / "FRCRN_SE_16K"
    pointer_path = checkpoint_dir / "last_best_checkpoint"
    if not pointer_path.is_file():
        raise FileNotFoundError(f"FRCRN checkpoint pointer not found: {pointer_path}")
    checkpoint_name = pointer_path.read_text(encoding="utf-8").strip()
    checkpoint_path = (checkpoint_dir / checkpoint_name).resolve()
    if checkpoint_dir.resolve() not in checkpoint_path.parents:
        raise ValueError("FRCRN checkpoint pointer refers outside its directory")
    if not checkpoint_path.is_file():
        raise FileNotFoundError(f"FRCRN checkpoint not found: {checkpoint_path}")
    if args.output_path.exists():
        raise FileExistsError(f"Refusing to overwrite output: {args.output_path}")

    audio, sample_rate = sf.read(
        str(args.input_path), dtype="float32", always_2d=True
    )
    if sample_rate != SAMPLE_RATE or audio.shape[1] != 1:
        raise ValueError(
            f"Expected 16 kHz mono input; got {sample_rate} Hz, shape {audio.shape}"
        )
    waveform = np.ascontiguousarray(audio[:, 0][None, :], dtype=np.float32)
    if waveform.size == 0 or not np.isfinite(waveform).all():
        raise ValueError("Input audio is empty or contains non-finite samples")
    if torch.cuda.is_available():
        raise RuntimeError("FRCRN worker must run with CPU-only PyTorch")

    pad_samples = min(EDGE_PADDING_SAMPLES, max(0, waveform.shape[1] - 1))
    if pad_samples:
        padded_waveform = np.pad(
            waveform,
            ((0, 0), (pad_samples, pad_samples)),
            mode="reflect",
        )
    else:
        padded_waveform = waveform

    model = ClearVoice(
        task="speech_enhancement",
        model_names=["FRCRN_SE_16K"],
    )
    network = model.models[0]
    if network.device.type != "cpu" or any(
        parameter.device.type != "cpu" for parameter in network.model.parameters()
    ):
        raise RuntimeError("FRCRN model was not loaded entirely on CPU")

    with torch.inference_mode():
        enhanced = np.asarray(model(padded_waveform))
    if enhanced.shape != padded_waveform.shape:
        raise ValueError(
            "FRCRN changed padded waveform shape from "
            f"{padded_waveform.shape} to {enhanced.shape}"
        )
    if enhanced.size == 0 or not np.isfinite(enhanced).all():
        raise ValueError("FRCRN output is empty or contains non-finite samples")
    if pad_samples:
        enhanced = enhanced[:, pad_samples : pad_samples + waveform.shape[1]]
    if enhanced.shape != waveform.shape:
        raise ValueError(
            f"Trimmed FRCRN output shape {enhanced.shape} does not match input "
            f"shape {waveform.shape}"
        )

    sf.write(
        str(args.output_path),
        enhanced[0],
        SAMPLE_RATE,
        subtype="FLOAT",
    )


if __name__ == "__main__":
    main()
