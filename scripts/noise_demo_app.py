"""Local side-by-side demo for raw and noise-frontend Muaalem inference."""

import logging
from pathlib import Path

import gradio as gr
import librosa
import numpy as np
import torch
import soundfile as sf

from quran_transcript import Aya, MoshafAttributes, quran_phonetizer
from quran_muaalem.inference import Muaalem
from quran_muaalem.explain_gradio import explain_for_gradio
from noise_frontend.frontend import NoiseFrontend, NoiseFrontendConfig
from noise_frontend.pipeline import NoiseRobustMuaalem

logging.basicConfig(level=logging.INFO)
SR = 16000
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
MODEL = Muaalem(device=DEVICE, dtype=torch.float32)
ROBUST_MODEL = NoiseRobustMuaalem(MODEL)
DEFAULT_MOSHAF = MoshafAttributes(
    rewaya="hafs", madd_monfasel_len=4, madd_mottasel_len=4,
    madd_mottasel_waqf=4, madd_aared_len=4,
)

_aya = Aya()
SURA_NAMES = {}
SURA_COUNTS = {}
for _sura in range(1, 115):
    _aya.set(_sura, 1)
    SURA_NAMES[_sura] = _aya.get().sura_name
    SURA_COUNTS[_sura] = _aya.get().num_ayat_in_sura


def _ayah_choices(sura):
    return gr.update(choices=list(range(1, SURA_COUNTS[int(sura)] + 1)), value=1)


def _reference(sura, aya, start, count):
    if None in (sura, aya, start, count):
        return ""
    return Aya(int(sura), int(aya)).get_by_imlaey_words(int(start), int(count)).uthmani


def _edit_distance(left, right):
    """Return character-level Levenshtein distance for two phoneme strings."""
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        for column, right_char in enumerate(right, start=1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1]


def _sifa_html(out_a, out_b):
    fields = ("qalqla", "ghonna", "hams_or_jahr", "shidda_or_rakhawa",
              "tafkheem_or_taqeeq", "itbaq", "safeer", "tikraar", "tafashie", "istitala")
    rows = ["<table style='border-collapse:collapse;width:100%'><tr><th>Phoneme</th><th>Feature</th><th>Raw A</th><th>Frontend B</th></tr>"]
    for a, b in zip(out_a.sifat, out_b.sifat):
        for field in fields:
            ua, ub = getattr(a, field), getattr(b, field)
            va, vb = (ua.text if ua else "—"), (ub.text if ub else "—")
            style = "background:#ffd5d5" if va != vb else ""
            rows.append(f"<tr style='{style}'><td>{a.phonemes_group}</td><td>{field}</td><td>{va}</td><td>{vb}</td></tr>")
    rows.append("</table>")
    return "".join(rows)


def process(audio_path, sura, aya, start, count, add_noise, snr_db,
            clean_threshold, moderate_atten, heavy_atten):
    if not audio_path:
        raise gr.Error("Upload or record a recitation first.")
    wave, _ = librosa.load(audio_path, sr=SR, mono=True)
    wave = np.asarray(wave, dtype=np.float32)
    if add_noise:
        rng = np.random.default_rng(2026)
        noise = rng.standard_normal(wave.size).astype(np.float32)
        noise *= np.sqrt(np.mean(wave ** 2) + 1e-12) / (10 ** (float(snr_db) / 20)) / (np.sqrt(np.mean(noise ** 2)) + 1e-12)
        raw = wave + noise
    else:
        raw = wave.copy()
    raw = raw.astype(np.float32, copy=False)
    ref = _reference(sura, aya, start, count)
    phonetic = quran_phonetizer(ref, DEFAULT_MOSHAF, remove_spaces=True)

    config = NoiseFrontendConfig(clean_snr_db=float(clean_threshold),
                                 moderate_atten_db=float(moderate_atten),
                                 heavy_atten_db=float(heavy_atten), device=DEVICE)
    ROBUST_MODEL.frontend = NoiseFrontend(config)
    out_a = MODEL([raw.copy()], [phonetic], sampling_rate=SR)[0]
    out_b = ROBUST_MODEL([raw.copy()], [phonetic], sampling_rate=SR)[0]
    status = ROBUST_MODEL.frontend_results[0]
    processed = status.wave
    raw_errors = _edit_distance(out_a.phonemes.text, phonetic.phonemes)
    processed_errors = _edit_distance(out_b.phonemes.text, phonetic.phonemes)
    # Keep raw on ties so preprocessing cannot change an equally good result.
    use_frontend = processed_errors < raw_errors
    selected_path = "Frontend B" if use_frontend else "Raw A"
    selected_wave = processed if use_frontend else raw
    if processed.size != raw.size or processed.dtype != np.float32:
        raise gr.Error("Frontend output shape or dtype is invalid")
    joint_peak = max(float(np.max(np.abs(raw), initial=0.0)),
                     float(np.max(np.abs(processed), initial=0.0)))
    if joint_peak > 0.99:
        shared_gain = 0.99 / joint_peak
        raw = (raw * shared_gain).astype(np.float32)
        processed = (processed * shared_gain).astype(np.float32)
    output_dir = Path("outputs/noise_demo")
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "raw.wav"
    processed_path = output_dir / "processed.wav"
    selected_pathname = output_dir / "recommended.wav"
    sf.write(raw_path, raw, SR, subtype="FLOAT")
    sf.write(processed_path, processed, SR, subtype="FLOAT")
    sf.write(selected_pathname, selected_wave, SR, subtype="FLOAT")
    phonemes = (
        "<p>Phoneme colors: white = matches reference, red = reference sound missing, "
        "green = extra predicted sound. Sifa tables compare predictions with the "
        "selected reference.</p>"
        "<h2>A · Raw compared with reference</h2>"
        + explain_for_gradio(out_a.phonemes.text, phonetic.phonemes,
                             out_a.sifat, phonetic.sifat, lang="arabic")
        + "<h2>B · Frontend compared with reference</h2>"
        + explain_for_gradio(out_b.phonemes.text, phonetic.phonemes,
                             out_b.sifat, phonetic.sifat, lang="arabic")
    )
    info = f"Estimated SNR: {status.snr_db:.1f} dB · mode: {status.mode} · too_noisy: {status.too_noisy}"
    recommendation = (
        f"**Reference-guided recommendation: {selected_path}.** "
        f"Phoneme edit errors: Raw A = {raw_errors}, Frontend B = {processed_errors}. "
        "This uses the selected reference to choose between the two predictions; "
        "it does not independently verify the recitation."
    )
    return (info, recommendation, str(selected_pathname), str(raw_path),
            str(processed_path), phonemes, _sifa_html(out_a, out_b))


with gr.Blocks(title="Noise Frontend Local Demo") as app:
    gr.Markdown("# Quran Muaalem noise frontend · localhost")
    with gr.Row():
        audio = gr.Audio(sources=["upload", "microphone"], type="filepath", label="Recitation")
        with gr.Column():
            sura = gr.Dropdown([(f"{i} · {SURA_NAMES[i]}", i) for i in range(1, 115)], value=1, label="Sura")
            aya = gr.Dropdown(list(range(1, max(SURA_COUNTS.values()) + 1)), value=1, label="Aya")
            start = gr.Number(value=0, minimum=0, step=1, label="Start word index")
            count = gr.Number(value=4, minimum=1, step=1, label="Number of words")
            reference = gr.Textbox(label="Uthmani reference", interactive=False)
    app.load(_ayah_choices, sura, aya)
    app.load(_reference, [sura, aya, start, count], reference)
    sura.change(_ayah_choices, sura, aya)
    for component in (sura, aya, start, count):
        component.change(_reference, [sura, aya, start, count], reference)
    add_noise = gr.Checkbox(label="Add synthetic noise", value=False)
    snr = gr.Slider(0, 30, value=10, step=1, label="Synthetic noise SNR (dB)")
    with gr.Row():
        clean_threshold = gr.Slider(
            0, 120, value=25, step=1, label="clean_snr_db",
            info="Estimated SNR at or above this value bypasses enhancement. To force a trial, set this above the displayed SNR.",
        )
        moderate = gr.Slider(0, 30, value=10, step=1, label="moderate_atten_db")
        heavy = gr.Slider(0, 30, value=20, step=1, label="heavy_atten_db")
    run = gr.Button("Compare raw and frontend", variant="primary")
    status_out = gr.Markdown()
    recommendation_out = gr.Markdown()
    recommended_audio = gr.Audio(label="Recommended for selected reference")
    with gr.Row():
        raw_audio = gr.Audio(label="A · Raw")
        frontend_audio = gr.Audio(label="B · Frontend")
    phoneme_out = gr.HTML(label="Each path compared with the selected reference")
    sifa_out = gr.HTML()
    run.click(process, [audio, sura, aya, start, count, add_noise, snr, clean_threshold, moderate, heavy],
              [status_out, recommendation_out, recommended_audio, raw_audio,
               frontend_audio, phoneme_out, sifa_out])


if __name__ == "__main__":
    app.launch(server_name="127.0.0.1", server_port=7860, share=False)
