from __future__ import annotations

from dataclasses import fields
from pathlib import Path
import sys
from typing import TypeAlias

import numpy as np
from librosa.core import load
from quran_transcript import (
    Aya,
    MoshafAttributes,
    chunck_phonemes,
    quran_phonetizer,
)

from quran_muaalem.inference import Muaalem
from quran_muaalem.muaalem_typing import Sifa, SingleUnit


SAMPLE_RATE = 16_000
SNR_LEVELS_DB = (10, 5, 0, -5)
RANDOM_SEED = 2025
ROOT = Path(__file__).resolve().parents[1]
AUDIO_PATH = ROOT / "assets" / "test.wav"
SIFA_FIELDS = tuple(
    field.name for field in fields(Sifa) if field.name != "phonemes_group"
)

Edit: TypeAlias = tuple[str, int | None, int | None]


def align_tokens(reference: list[str], predicted: list[str]) -> list[Edit]:
    """Return a minimum-edit alignment with deterministic tie-breaking."""
    rows = len(reference) + 1
    columns = len(predicted) + 1
    costs = [[0] * columns for _ in range(rows)]

    for i in range(rows):
        costs[i][0] = i
    for j in range(columns):
        costs[0][j] = j

    for i in range(1, rows):
        for j in range(1, columns):
            costs[i][j] = min(
                costs[i - 1][j - 1]
                + (reference[i - 1] != predicted[j - 1]),
                costs[i - 1][j] + 1,
                costs[i][j - 1] + 1,
            )

    edits: list[Edit] = []
    i, j = len(reference), len(predicted)
    while i or j:
        if (
            i
            and j
            and reference[i - 1] == predicted[j - 1]
            and costs[i][j] == costs[i - 1][j - 1]
        ):
            edits.append(("match", i - 1, j - 1))
            i -= 1
            j -= 1
        elif i and j and costs[i][j] == costs[i - 1][j - 1] + 1:
            edits.append(("substitution", i - 1, j - 1))
            i -= 1
            j -= 1
        elif i and costs[i][j] == costs[i - 1][j] + 1:
            edits.append(("deletion", i - 1, None))
            i -= 1
        else:
            edits.append(("insertion", None, j - 1))
            j -= 1

    edits.reverse()
    return edits


def edit_counts(edits: list[Edit]) -> dict[str, int]:
    return {
        operation: sum(edit[0] == operation for edit in edits)
        for operation in ("substitution", "deletion", "insertion")
    }


def describe_edits(
    edits: list[Edit], reference: list[str], predicted: list[str]
) -> list[str]:
    descriptions = []
    for operation, ref_idx, pred_idx in edits:
        if operation == "match":
            continue
        if operation == "substitution":
            descriptions.append(
                f"SUB[{ref_idx + 1}] {reference[ref_idx]} -> {predicted[pred_idx]}"
            )
        elif operation == "deletion":
            descriptions.append(f"DEL[{ref_idx + 1}] {reference[ref_idx]}")
        else:
            descriptions.append(f"INS[{pred_idx + 1}] {predicted[pred_idx]}")
    return descriptions


def sifa_labels(sifa: Sifa) -> dict[str, str | None]:
    return {
        name: getattr(sifa, name).text
        if isinstance(getattr(sifa, name), SingleUnit)
        else None
        for name in SIFA_FIELDS
    }


def compare_sifa(
    reference_sifat: list[Sifa], predicted_sifat: list[Sifa]
) -> tuple[dict[str, int], dict[str, int], list[str]]:
    reference_groups = [item.phonemes_group for item in reference_sifat]
    predicted_groups = [item.phonemes_group for item in predicted_sifat]
    alignment = align_tokens(reference_groups, predicted_groups)
    group_edits = edit_counts(alignment)
    attribute_changes = {name: 0 for name in SIFA_FIELDS}
    details = []

    for operation, ref_idx, pred_idx in alignment:
        if operation != "match":
            continue
        before = sifa_labels(reference_sifat[ref_idx])
        after = sifa_labels(predicted_sifat[pred_idx])
        changed = [
            name for name in SIFA_FIELDS if before[name] != after[name]
        ]
        if changed:
            for name in changed:
                attribute_changes[name] += 1
            details.append(
                f"{reference_groups[ref_idx]}: "
                + ", ".join(
                    f"{name} {before[name]} -> {after[name]}" for name in changed
                )
            )

    return group_edits, attribute_changes, details


def make_noisy_waveforms(clean_wave: np.ndarray) -> list[tuple[int, float, np.ndarray]]:
    signal = clean_wave.astype(np.float64, copy=False)
    signal_power = float(np.mean(signal**2))
    if signal_power <= 0:
        raise ValueError(f"Audio has no measurable signal power: {AUDIO_PATH}")

    rng = np.random.default_rng(RANDOM_SEED)
    noise = rng.standard_normal(clean_wave.shape).astype(np.float64)
    noise -= noise.mean()
    noise_power = float(np.mean(noise**2))
    if noise_power <= 0:
        raise ValueError("Generated white noise has no measurable power")

    waveforms = []
    for target_snr_db in SNR_LEVELS_DB:
        target_noise_power = signal_power / (10 ** (target_snr_db / 10))
        scaled_noise = noise * np.sqrt(target_noise_power / noise_power)
        noisy_wave = (signal + scaled_noise).astype(np.float32)
        if noisy_wave.shape != clean_wave.shape:
            raise RuntimeError("Noisy waveform length differs from the clean waveform")

        measured_noise = noisy_wave.astype(np.float64) - signal
        measured_noise_power = float(np.mean(measured_noise**2))
        measured_snr_db = 10 * np.log10(signal_power / measured_noise_power)
        waveforms.append((target_snr_db, float(measured_snr_db), noisy_wave))
    return waveforms


def run_inference(muaalem: Muaalem, wave: np.ndarray, reference: object):
    outputs = muaalem([wave], [reference], sampling_rate=SAMPLE_RATE)
    if len(outputs) != 1:
        raise RuntimeError(f"Expected one inference result, received {len(outputs)}")
    return outputs[0]


def format_counts(counts: dict[str, int]) -> str:
    return (
        f"{counts['substitution']}/{counts['deletion']}/"
        f"{counts['insertion']}"
    )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")

    if not AUDIO_PATH.is_file():
        raise FileNotFoundError(f"Required clean audio was not found: {AUDIO_PATH}")

    uthmani_reference = Aya(8, 75).get_by_imlaey_words(17, 9).uthmani
    moshaf = MoshafAttributes(
        rewaya="hafs",
        madd_monfasel_len=2,
        madd_mottasel_len=4,
        madd_mottasel_waqf=4,
        madd_aared_len=2,
    )
    phonetizer_reference = quran_phonetizer(
        uthmani_reference, moshaf, remove_spaces=True
    )
    reference_tokens = chunck_phonemes(phonetizer_reference.phonemes)
    reference_sifa_groups = [item.phonemes for item in phonetizer_reference.sifat]
    if reference_tokens != reference_sifa_groups:
        raise RuntimeError(
            "Reference phoneme chunks do not match the Quran phonetizer Sifa groups"
        )

    clean_wave, _ = load(str(AUDIO_PATH), sr=SAMPLE_RATE, mono=True)
    clean_wave = np.asarray(clean_wave, dtype=np.float32)
    noisy_waveforms = make_noisy_waveforms(clean_wave)

    print(f"Audio: {AUDIO_PATH}")
    print(
        f"Input: {SAMPLE_RATE} Hz mono, {len(clean_wave)} samples "
        f"({len(clean_wave) / SAMPLE_RATE:.2f} s)"
    )
    print(f"CPU model: Muaalem v3.2; NumPy RNG seed: {RANDOM_SEED}")
    print("Loading the model once; running clean audio first.")
    muaalem = Muaalem(device="cpu")
    clean_output = run_inference(muaalem, clean_wave, phonetizer_reference)
    clean_tokens = chunck_phonemes(clean_output.phonemes.text)

    results = []
    for target_snr_db, measured_snr_db, noisy_wave in noisy_waveforms:
        noisy_output = run_inference(muaalem, noisy_wave, phonetizer_reference)
        noisy_tokens = chunck_phonemes(noisy_output.phonemes.text)

        clean_alignment = align_tokens(clean_tokens, noisy_tokens)
        reference_alignment = align_tokens(reference_tokens, noisy_tokens)
        sifa_group_edits, sifa_changes, sifa_details = compare_sifa(
            clean_output.sifat, noisy_output.sifat
        )
        results.append(
            (
                target_snr_db,
                measured_snr_db,
                clean_alignment,
                reference_alignment,
                sifa_group_edits,
                sifa_changes,
                sifa_details,
                noisy_tokens,
            )
        )

    print()
    print(
        "Phoneme edits are substitution/deletion/insertion counts. "
        "Clean-vs-noisy is model drift, not ground truth."
    )
    print(
        "Reference-vs-noisy uses the Quran phonetizer output, not human "
        "audio annotations."
    )
    print(
        f"{'Target':>7} {'Measured':>9} {'Clean drift S/D/I':>19} "
        f"{'Reference errors S/D/I':>24} {'Sifa group edits S/D/I':>24} "
        f"{'Sifa attribute changes':>23}"
    )
    for (
        target_snr_db,
        measured_snr_db,
        clean_alignment,
        reference_alignment,
        sifa_group_edits,
        sifa_changes,
        _,
        _,
    ) in results:
        clean_counts = edit_counts(clean_alignment)
        reference_counts = edit_counts(reference_alignment)
        changed_attributes = sum(sifa_changes.values())
        print(
            f"{target_snr_db:>4} dB {measured_snr_db:>8.3f} dB "
            f"{format_counts(clean_counts):>19} "
            f"{format_counts(reference_counts):>24} "
            f"{format_counts(sifa_group_edits):>24} "
            f"{changed_attributes:>23}"
        )

    for (
        target_snr_db,
        measured_snr_db,
        clean_alignment,
        reference_alignment,
        _,
        _,
        sifa_details,
        noisy_tokens,
    ) in results:
        print(f"\n{target_snr_db} dB target (measured {measured_snr_db:.3f} dB)")
        for heading, alignment, expected in (
            ("Model drift: clean prediction -> noisy prediction", clean_alignment, clean_tokens),
            ("Reference comparison: generated Quran phonemes -> noisy prediction", reference_alignment, reference_tokens),
        ):
            differences = describe_edits(alignment, expected, noisy_tokens)
            print(f"  {heading}:")
            print("    " + ("; ".join(differences) if differences else "no phoneme edits"))
        print("  Sifa changes on aligned matching phoneme groups:")
        print("    " + ("; ".join(sifa_details) if sifa_details else "no attribute changes"))


if __name__ == "__main__":
    main()
