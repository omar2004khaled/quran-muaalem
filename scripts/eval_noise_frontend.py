"""Compare noisy and enhanced recitations against clean Muaalem predictions."""

import argparse
import csv
import logging
from pathlib import Path

import numpy as np

SIFAT = ("qalqla", "ghonna", "hams_or_jahr", "shidda_or_rakhawa",
         "tafkheem_or_taqeeq", "itbaq", "safeer", "tikraar", "tafashie", "istitala")


def _distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        row = [i]
        for j, cb in enumerate(b, 1):
            row.append(min(row[-1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)))
        prev = row
    return prev[-1]


def _mix_noise(clean, noise, snr_db):
    if noise.size == 0:
        raise ValueError("Noise file is empty")
    repeats = int(np.ceil(clean.size / noise.size))
    noise = np.tile(noise, repeats)[:clean.size].astype(np.float32)
    clean_rms = np.sqrt(np.mean(clean ** 2) + 1e-12)
    noise_rms = np.sqrt(np.mean(noise ** 2) + 1e-12)
    scaled = noise * (clean_rms / (10 ** (snr_db / 20)) / noise_rms)
    return clean + scaled


def _agreement(clean_out, test_out):
    clean_sifat, test_sifat = clean_out.sifat, test_out.sifat
    count = min(len(clean_sifat), len(test_sifat))
    scores = {}
    for name in SIFAT:
        compared = [(getattr(a, name), getattr(b, name)) for a, b in zip(clean_sifat[:count], test_sifat[:count])]
        compared = [(a, b) for a, b in compared if a is not None and b is not None]
        scores[name] = (sum(a.text == b.text for a, b in compared) / len(compared)) if compared else float("nan")
    return scores


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio_dir", type=Path)
    parser.add_argument("noise_dir", type=Path)
    parser.add_argument("--output", type=Path, default=Path("noise_frontend_eval.csv"))
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--rir-dir", type=Path, help="Optional folder of mono room impulse WAV files")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    import librosa
    from quran_transcript import MoshafAttributes, quran_phonetizer
    from quran_muaalem import Muaalem
    from noise_frontend.enhancer import enhance_wave

    model = Muaalem(device=args.device)
    noise_paths = sorted(args.noise_dir.glob("*.wav"))
    if not noise_paths:
        parser.error("noise_dir must contain WAV noise files")
    rir_paths = sorted(args.rir_dir.glob("*.wav")) if args.rir_dir else []
    rows = []
    for audio_path in sorted(args.audio_dir.glob("*.wav")):
        ref_path = audio_path.with_suffix(".txt")
        if not ref_path.exists():
            logging.warning("Skipping %s: missing same-stem .txt reference", audio_path.name)
            continue
        clean, _ = librosa.load(audio_path, sr=16000, mono=True)
        text = ref_path.read_text(encoding="utf-8").strip()
        phonetizer_out = quran_phonetizer(text, MoshafAttributes(), remove_spaces=True)
        clean_out = model([clean], [phonetizer_out], sampling_rate=16000)[0]
        noise, _ = librosa.load(noise_paths[len(rows) % len(noise_paths)], sr=16000, mono=True)
        base = clean.astype(np.float32)
        if rir_paths:
            rir, _ = librosa.load(rir_paths[len(rows) % len(rir_paths)], sr=16000, mono=True)
            convolved = np.convolve(base, rir, mode="full")[:base.size]
            base = convolved.astype(np.float32)
        for snr in (20, 10, 5, 0):
            noisy = _mix_noise(base, noise.astype(np.float32), snr)
            variants = {"raw": noisy}
            enhanced = {}
            for attenuation in (6, 12, 20, 100):
                enhanced[attenuation] = enhance_wave(noisy, 16000, attenuation, args.device)
                variants[f"deepfilter_{attenuation}"] = enhanced[attenuation]
            for mu in (0.3, 0.5, 0.7):
                variants[f"observation_mu_{mu}"] = mu * noisy + (1 - mu) * enhanced[100]
            for variant, signal in variants.items():
                out = model([signal.astype(np.float32)], [phonetizer_out], sampling_rate=16000)[0]
                distance = _distance(clean_out.phonemes.text, out.phonemes.text)
                row = {"file": audio_path.name, "snr_db": snr, "variant": variant,
                       "phoneme_error_rate": distance / max(1, len(clean_out.phonemes.text))}
                row.update({f"agreement_{k}": v for k, v in _agreement(clean_out, out).items()})
                rows.append(row)
    if rows:
        with args.output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    logging.info("Wrote %d evaluation rows to %s", len(rows), args.output)


if __name__ == "__main__":
    main()
