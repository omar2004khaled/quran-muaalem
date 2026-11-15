import logging
from dataclasses import asdict, fields
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

import numpy as np
import soundfile as sf
from quran_transcript import (
    Aya,
    chunck_phonemes,
    quran_phonetizer,
    MoshafAttributes,
)
from quran_transcript.utils import PartOfUthmaniWord
from quran_transcript.phonetics.moshaf_attributes import (
    get_arabic_attributes,
    get_arabic_name,
)
from librosa.core import load
from pydantic.fields import FieldInfo, PydanticUndefined
import torch
import gradio as gr

from quran_muaalem.inference import Muaalem
from quran_muaalem.muaalem_typing import MuaalemOutput, Sifa, SingleUnit
from quran_muaalem.explain import explain_for_terminal
from quran_muaalem.explain_gradio import explain_for_gradio

# Initialize components
REQUIRED_MOSHAF_FIELDS = [
    "rewaya",
    "takbeer",
    "madd_monfasel_len",
    "madd_mottasel_len",
    "madd_mottasel_waqf",
    "madd_aared_len",
    "madd_alleen_len",
    "ghonna_lam_and_raa",
    "meem_aal_imran",
    "madd_yaa_alayn_alharfy",
    "saken_before_hamz",
    "sakt_iwaja",
    "sakt_marqdena",
    "sakt_man_raq",
    "sakt_bal_ran",
    "sakt_maleeyah",
    "between_anfal_and_tawba",
    "noon_and_yaseen",
    "yaa_ataan",
    "start_with_ism",
    "yabsut",
    "bastah",
    "almusaytirun",
    "bimusaytir",
    "tasheel_or_madd",
    "yalhath_dhalik",
    "irkab_maana",
    "noon_tamnna",
    "harakat_daaf",
    "alif_salasila",
    "idgham_nakhluqkum",
    "raa_firq",
    "raa_alqitr",
    "raa_misr",
    "raa_nudhur",
    "raa_yasr",
    "meem_mokhfah",
]
model_id = "obadx/muaalem-model-v3_2"
logging.basicConfig(level=logging.INFO)
device = "cuda" if torch.cuda.is_available() else "cpu"
muaalem = Muaalem(model_name_or_path=model_id, device=device)
sampling_rate = 16000
FRCRN_ROOT = Path(
    os.environ.get(
        "QURAN_MUAALEM_FRCRN_ROOT", r"D:\venvs\quran-muaalem-frcrn"
    )
)
FRCRN_PYTHON = Path(
    os.environ.get(
        "QURAN_MUAALEM_FRCRN_PYTHON",
        str(FRCRN_ROOT / "Scripts" / "python.exe"),
    )
)
FRCRN_WORKER = Path(__file__).with_name("frcrn_worker.py")
SIFA_FIELDS = tuple(
    field.name for field in fields(Sifa) if field.name != "phonemes_group"
)
FRCRN_MIX_STRENGTHS = (0.0, 0.25, 0.5, 0.75, 1.0)

# Load Sura information
sura_idx_to_name = {}
sura_to_aya_count = {}
start_aya = Aya()
for sura_idx in range(1, 115):
    start_aya.set(sura_idx, 1)
    sura_idx_to_name[sura_idx] = start_aya.get().sura_name
    sura_to_aya_count[sura_idx] = start_aya.get().num_ayat_in_sura

# Default moshaf settings
default_moshaf = MoshafAttributes(
    rewaya="hafs",
    madd_monfasel_len=4,
    madd_mottasel_len=4,
    madd_mottasel_waqf=4,
    madd_aared_len=4,
)

# Current moshaf settings (will be updated from settings page)
current_moshaf = default_moshaf


def get_field_name(field_name: str, field_info: FieldInfo) -> str:
    """Return the Arabic name of the field if applicable else the field_name"""
    label = field_name
    arabic_name = get_arabic_name(field_info)
    if arabic_name:
        label = f"{arabic_name} ({field_name})"
    return label


def unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """Unwrap Optional[T] / Union[T, None] to return (T, True) or (annotation, False)."""
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        non_none_args = [arg for arg in get_args(annotation) if arg is not type(None)]
        is_optional = len(non_none_args) < len(get_args(annotation))
        if len(non_none_args) == 1:
            return non_none_args[0], is_optional
    return annotation, False


def create_gradio_input_for_field(
    field_name: str,
    field_info: FieldInfo,
    default_value: Any = None,
    key_prefix="model_",
    help: str | None = None,
) -> Any:
    """Create a gradio input field given a pydantic field info"""
    # Extract Arabic name from field description if available
    label = get_field_name(field_name, field_info)

    if default_value is None:
        if field_info.default != PydanticUndefined:
            default_value = field_info.default

    if help is None:
        help = field_info.description

    unwrapped_annotation, is_optional = unwrap_optional(field_info.annotation)

    # Handle Literal types (including Optional[Literal[...]])
    if get_origin(unwrapped_annotation) is Literal:
        choices = list(get_args(unwrapped_annotation))
        arabic_attributes = get_arabic_attributes(field_info)

        # Create choice list with Arabic labels if available
        choice_list = []
        if is_optional:
            choice_list.append(("افتراضي (Default / None)", None))
        for choice in choices:
            if arabic_attributes and choice in arabic_attributes:
                choice_list.append((arabic_attributes[choice], choice))
            else:
                choice_list.append((str(choice), choice))

        return gr.Dropdown(
            choices=choice_list,
            value=default_value,
            label=label,
            info=help,
            interactive=True,
        )

    # Handle different field types
    if unwrapped_annotation is str:
        return gr.Textbox(value=default_value or "", label=label, info=help)
    elif unwrapped_annotation is int:
        return gr.Number(value=default_value or 0, label=label, info=help, precision=0)
    elif unwrapped_annotation is float:
        return gr.Number(
            value=default_value or 0.0, label=label, info=help, precision=1
        )
    elif unwrapped_annotation is bool:
        return gr.Checkbox(value=default_value or False, label=label, info=help)

    raise ValueError(f"Unsupported field type for {label}: {field_info.annotation}")


def update_aya_dropdown(sura_idx):
    if not sura_idx:
        sura_idx = 1
    return gr.update(
        choices=list(range(1, sura_to_aya_count[int(sura_idx)] + 1)), value=1
    )


def update_uthmani_ref(sura_idx, aya_idx, start_idx, num_words):
    if not all([sura_idx, aya_idx, start_idx is not None, num_words is not None]):
        return ""
    try:
        uthmani_ref = (
            Aya(int(sura_idx), int(aya_idx))
            .get_by_imlaey_words(int(start_idx), int(num_words))
            .uthmani
        )
        return uthmani_ref
    except PartOfUthmaniWord as e:
        return f"⚠️ Warning: You've selected part of a Uthmani word. Please adjust the number of words to include complete words only.\n\nError details: {str(e)}"
    except Exception as e:
        return f"Error: {str(e)}"


def process_audio(audio, sura_idx, aya_idx, start_idx, num_words):
    global current_moshaf

    if audio is None:
        return "Please upload an audio file first"

    try:
        # Get Uthmani reference text
        uthmani_ref = (
            Aya(int(sura_idx), int(aya_idx))
            .get_by_imlaey_words(int(start_idx), int(num_words))
            .uthmani
        )
        phonetizer_out = quran_phonetizer(
            uthmani_ref, current_moshaf, remove_spaces=True
        )

        # Process audio
        wave, _ = load(audio, sr=sampling_rate, mono=True)
        outs = muaalem(
            [wave],
            [phonetizer_out],
            sampling_rate=sampling_rate,
        )

        # # Prepare output
        # output_text = f"Phonemes: {outs[0].phonemes}\n\n"
        # for sifa in outs[0].sifat:
        #     output_text += json.dumps(asdict(sifa), indent=2, ensure_ascii=False) + "\n"
        #     output_text += "*" * 30 + "\n"
        # output_text += "-" * 40 + "\n\n"

        # Add explanation
        explanation_html = explain_for_gradio(
            outs[0].phonemes.text,
            phonetizer_out.phonemes,
            outs[0].sifat,
            phonetizer_out.sifat,
            lang="arabic",
        )

        return explanation_html

    except PartOfUthmaniWord as e:
        return f"⚠️ Error: The selected word range includes partial Uthmani words. Please adjust the number of words to include complete words only.\n\nError details: {str(e)}"
    # except Exception as e:
    #     return f"Error processing audio: {str(e)}"


def align_tokens(reference: list[str], predicted: list[str]):
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

    edits = []
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


def phoneme_edit_counts(reference: list[str], predicted: list[str]) -> dict[str, int]:
    edits = align_tokens(reference, predicted)
    return {
        operation: sum(edit[0] == operation for edit in edits)
        for operation in ("substitution", "deletion", "insertion")
    }


def _is_missing_or_pad(value: object) -> bool:
    if value is None:
        return True
    normalized = str(value).strip().upper()
    return normalized in {
        "",
        "PAD",
        "[PAD]",
        "<PAD>",
        "NONE",
        "NAN",
        "MISSING",
    } or normalized.strip("[]<>") in {"PAD", "MISSING"}


def sifa_change_count(before: list[Sifa], after: list[Sifa]) -> int:
    alignment = align_tokens(
        [item.phonemes_group for item in before],
        [item.phonemes_group for item in after],
    )
    changes = 0
    for operation, before_idx, after_idx in alignment:
        if operation != "match":
            continue
        if _is_missing_or_pad(before[before_idx].phonemes_group):
            continue

        for field_name in SIFA_FIELDS:
            before_value = getattr(before[before_idx], field_name)
            after_value = getattr(after[after_idx], field_name)
            if not isinstance(before_value, SingleUnit) or not isinstance(
                after_value, SingleUnit
            ):
                continue
            if _is_missing_or_pad(before_value.text) or _is_missing_or_pad(
                after_value.text
            ):
                continue
            if before_value.text != after_value.text:
                changes += 1
    return changes


def _format_edit_counts(counts: dict[str, int]) -> str:
    total = sum(counts.values())
    return (
        f"{counts['substitution']}/{counts['deletion']}/"
        f"{counts['insertion']} ({total})"
    )


def enhance_with_frcrn(wave: np.ndarray) -> np.ndarray:
    if not FRCRN_PYTHON.is_file():
        raise FileNotFoundError(f"FRCRN Python interpreter not found: {FRCRN_PYTHON}")
    if not FRCRN_ROOT.is_dir():
        raise FileNotFoundError(f"FRCRN environment directory not found: {FRCRN_ROOT}")
    if not FRCRN_WORKER.is_file():
        raise FileNotFoundError(f"FRCRN worker not found: {FRCRN_WORKER}")
    if wave.ndim != 1 or wave.size == 0 or not np.isfinite(wave).all():
        raise ValueError("FRCRN input must be a non-empty, finite mono waveform")

    checkpoint_dir = FRCRN_ROOT / "checkpoints" / "FRCRN_SE_16K"
    pointer_path = checkpoint_dir / "last_best_checkpoint"
    if not pointer_path.is_file():
        raise FileNotFoundError(f"FRCRN checkpoint pointer not found: {pointer_path}")
    checkpoint_name = pointer_path.read_text(encoding="utf-8").strip()
    checkpoint_path = (checkpoint_dir / checkpoint_name).resolve()
    if checkpoint_dir.resolve() not in checkpoint_path.parents:
        raise ValueError(
            "FRCRN checkpoint pointer refers outside its checkpoint directory"
        )
    if not checkpoint_path.is_file():
        raise FileNotFoundError(f"FRCRN checkpoint not found: {checkpoint_path}")

    with tempfile.TemporaryDirectory(prefix="quran-muaalem-frcrn-") as temp_dir:
        input_path = Path(temp_dir) / "input.wav"
        output_path = Path(temp_dir) / "enhanced.wav"
        sf.write(str(input_path), wave, sampling_rate, subtype="FLOAT")
        worker_env = os.environ.copy()
        worker_env["CUDA_VISIBLE_DEVICES"] = "-1"
        result = subprocess.run(
            [
                str(FRCRN_PYTHON),
                str(FRCRN_WORKER),
                str(input_path),
                str(output_path),
            ],
            cwd=str(FRCRN_ROOT),
            env=worker_env,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"FRCRN worker failed with exit code {result.returncode}: "
                f"{details[-4000:]}"
            )
        if not output_path.is_file():
            raise RuntimeError("FRCRN worker completed without creating its output WAV")

        enhanced, output_rate = sf.read(
            str(output_path), dtype="float32", always_2d=True
        )
        if output_rate != sampling_rate or enhanced.shape != (wave.size, 1):
            raise ValueError(
                "FRCRN output must remain 16 kHz mono and the same length as input; "
                f"got {output_rate} Hz, shape {enhanced.shape}"
            )
        enhanced_wave = enhanced[:, 0]
        if not np.isfinite(enhanced_wave).all():
            raise ValueError("FRCRN output contains non-finite samples")
        return enhanced_wave


def compare_audio_with_frcrn(audio, sura_idx, aya_idx, start_idx, num_words):
    if audio is None:
        return None, None, "", "", "", "Please upload or record audio first."

    try:
        uthmani_ref = (
            Aya(int(sura_idx), int(aya_idx))
            .get_by_imlaey_words(int(start_idx), int(num_words))
            .uthmani
        )
        phonetizer_out = quran_phonetizer(
            uthmani_ref, current_moshaf, remove_spaces=True
        )
        wave, _ = load(audio, sr=sampling_rate, mono=True)
        wave = np.asarray(wave, dtype=np.float32)
        raw_frcrn = enhance_with_frcrn(wave)

        candidates = []
        for strength in FRCRN_MIX_STRENGTHS:
            if strength == 0:
                candidate = wave.copy()
            elif strength == 1:
                candidate = raw_frcrn.copy()
            else:
                candidate = (
                    wave + np.float32(strength) * (raw_frcrn - wave)
                ).astype(np.float32)
            candidates.append(candidate)

        results = muaalem(
            candidates,
            [phonetizer_out] * len(candidates),
            sampling_rate=sampling_rate,
        )
        if len(results) != len(candidates):
            raise RuntimeError(
                f"Expected {len(candidates)} Muaalem results, received {len(results)}"
            )

        reference_tokens = chunck_phonemes(phonetizer_out.phonemes)
        candidate_tokens = [
            chunck_phonemes(output.phonemes.text) for output in results
        ]
        reference_edits = [
            phoneme_edit_counts(reference_tokens, tokens)
            for tokens in candidate_tokens
        ]

        def score(index: int):
            counts = reference_edits[index]
            return (
                sum(counts.values()),
                counts["deletion"],
                counts["substitution"],
                counts["insertion"],
                abs(FRCRN_MIX_STRENGTHS[index] - 0.25),
            )

        selected_idx = min(range(len(candidates)), key=score)
        original_output = results[0]
        raw_output = results[-1]
        selected_output = results[selected_idx]
        original_tokens = candidate_tokens[0]
        raw_tokens = candidate_tokens[-1]
        selected_tokens = candidate_tokens[selected_idx]

        original_html = explain_for_gradio(
            original_output.phonemes.text,
            phonetizer_out.phonemes,
            original_output.sifat,
            phonetizer_out.sifat,
            lang="arabic",
        )
        raw_html = explain_for_gradio(
            raw_output.phonemes.text,
            phonetizer_out.phonemes,
            raw_output.sifat,
            phonetizer_out.sifat,
            lang="arabic",
        )
        selected_html = explain_for_gradio(
            selected_output.phonemes.text,
            phonetizer_out.phonemes,
            selected_output.sifat,
            phonetizer_out.sifat,
            lang="arabic",
        )

        raw_drift = phoneme_edit_counts(original_tokens, raw_tokens)
        selected_drift = phoneme_edit_counts(original_tokens, selected_tokens)
        raw_sifa_changes = sifa_change_count(
            original_output.sifat, raw_output.sifat
        )
        selected_sifa_changes = sifa_change_count(
            original_output.sifat, selected_output.sifat
        )
        selected_strength = FRCRN_MIX_STRENGTHS[selected_idx]
        original_reference_edits = reference_edits[0]
        raw_reference_edits = reference_edits[-1]
        selected_reference_edits = reference_edits[selected_idx]
        comparison = (
            "**Phoneme edits vs. generated Quran reference (S/D/I; total):** "
            f"Original `{_format_edit_counts(original_reference_edits)}` · "
            f"Raw FRCRN `{_format_edit_counts(raw_reference_edits)}` · "
            f"Selected `{_format_edit_counts(selected_reference_edits)}`\n\n"
            "**Prediction drift vs. original (S/D/I; total):** "
            f"Raw FRCRN `{_format_edit_counts(raw_drift)}` · "
            f"Selected `{_format_edit_counts(selected_drift)}`\n\n"
            "**Sifa label changes vs. original (PAD/missing excluded):** "
            f"Raw FRCRN `{raw_sifa_changes}` · "
            f"Selected `{selected_sifa_changes}`\n\n"
            f"**Selected FRCRN mix:** `{selected_strength:.0%}` enhancement + "
            f"`{1 - selected_strength:.0%}` original audio. Selection minimizes "
            "Muaalem phoneme edits against the generated reference, then fewer "
            "deletions and a conservative 25% mix on ties. This is a "
            "preservation heuristic, not a guarantee of correct recitation."
        )
        return (
            (sampling_rate, raw_frcrn),
            (sampling_rate, candidates[selected_idx]),
            original_html,
            raw_html,
            selected_html,
            comparison,
        )
    except PartOfUthmaniWord as e:
        return (
            None,
            None,
            "",
            "",
            "",
            f"Selected range must include complete Uthmani words: {e}",
        )


def update_moshaf_settings(*args):
    """Update the global moshaf settings with values from the settings page"""
    global current_moshaf, field_names

    try:
        # Create a dictionary from the field names and values
        settings_dict = dict(zip(field_names, args))

        # Create a new MoshafAttributes object with the updated values
        current_moshaf = MoshafAttributes(**settings_dict)
        return "✅ تم حفظ الإعدادات بنجاح - Settings saved successfully!"
    except Exception as e:
        return f"❌ خطأ في حفظ الإعدادات - Error saving settings: {str(e)}"


def reset_settings():
    """Reset all settings to default values"""
    global current_moshaf

    try:
        current_moshaf = default_moshaf
        # Return default values for all fields
        default_values = [
            getattr(default_moshaf, field_name) for field_name in field_names
        ]
        return default_values + [
            "✅ تم إعادة التعيين إلى الإعدادات الافتراضية - Reset to default settings successfully!"
        ]
    except Exception as e:
        return [getattr(current_moshaf, field_name) for field_name in field_names] + [
            f"❌ Error resetting settings: {str(e)}"
        ]


AI_ADVISORY_NOTICE = """
> **إشعار الذكاء الاصطناعي / AI Advisory Notice**
>
> **العربية:** هذا المعلّم القرآني نموذج ذكاء اصطناعي قد يخطئ؛ فهو للإعانة فقط ولا يُعتمد عليه، وليس بديلاً عن معلّمٍ مُتقِنٍ مُجاز؛ لأنّ القرآن لا يُؤخذ إلا بالمشافهة.
>
> **English:** This Quranic Teacher is an artificial-intelligence model that may make mistakes. It is intended for assistance only and must not be relied upon. It is not a replacement for a proficient, authorized teacher, because the Quran is only received through oral transmission (mushafahah).
"""


# Create the Gradio app
with gr.Blocks(title="المعلم القرآني") as app:
    gr.Markdown(AI_ADVISORY_NOTICE)

    # Store current moshaf settings in session state
    current_moshaf_state = gr.State(default_moshaf)

    # Initialize field names list
    field_names = []

    with gr.Tab("التحليل الرئيسي - Main Analysis"):
        gr.Markdown("# كشف أخطاء التلاوة والتجويد وصفات الحروف")
        gr.Markdown("اختر المقطع القرآني المراد تعلمه")

        with gr.Row():
            with gr.Column(scale=1):
                gr.Markdown("### التلاة المقارنة")

                # Create sura dropdown with both index and name
                sura_choices = [
                    (f"{idx} - {sura_idx_to_name[idx]}", idx) for idx in range(1, 115)
                ]
                sura_dropdown = gr.Dropdown(
                    choices=sura_choices,
                    label="السورة",
                    value=1,
                    elem_id="sura_dropdown",
                )

                aya_dropdown = gr.Dropdown(
                    choices=list(range(1, sura_to_aya_count[1] + 1)),
                    label="رقم الآية",
                    value=1,
                    elem_id="aya_dropdown",
                )
                start_idx = gr.Number(
                    value=0,
                    label="رقمة الكلمة بداية من صفر (Word Index)",
                    minimum=0,
                    step=1,
                    elem_id="start_idx",
                )
                num_words = gr.Number(
                    value=4,
                    label="عدد الكلمات",
                    minimum=1,
                    step=1,
                    elem_id="num_words",
                )
                uthmani_text = gr.Textbox(
                    label="الرسم العثماني",
                    interactive=False,
                    elem_id="uthmani_text",
                )

            with gr.Column(scale=2):
                gr.Markdown("### فحص التلاوة القرآنية")
                audio_input = gr.Audio(
                    sources=["upload", "microphone"],
                    label="Upload or Record Audio",
                    type="filepath",
                    elem_id="audio_input",
                )
                analyze_btn = gr.Button(
                    "افحص التلاوة", variant="primary", elem_id="analyze_btn"
                )
                output_html = gr.HTML(
                    label="نتيجة الفحص",
                    elem_id="output_html",
                )

        # Initial update of uthmani text
        app.load(
            update_uthmani_ref,
            inputs=[sura_dropdown, aya_dropdown, start_idx, num_words],
            outputs=uthmani_text,
        )

        # Update aya dropdown when sura changes and reset aya_idx to 1
        sura_dropdown.change(
            update_aya_dropdown, inputs=sura_dropdown, outputs=aya_dropdown
        ).then(
            update_uthmani_ref,
            inputs=[sura_dropdown, aya_dropdown, start_idx, num_words],
            outputs=uthmani_text,
        )

        # Update uthmani text when any parameter changes
        for component in [aya_dropdown, start_idx, num_words]:
            component.change(
                update_uthmani_ref,
                inputs=[sura_dropdown, aya_dropdown, start_idx, num_words],
                outputs=uthmani_text,
            )

        # Process audio when button is clicked
        analyze_btn.click(
            process_audio,
            inputs=[audio_input, sura_dropdown, aya_dropdown, start_idx, num_words],
            outputs=output_html,
        )

        with gr.Accordion(
            "تجربة تحسين الضوضاء - FRCRN enhancement comparison",
            open=False,
        ):
            gr.Markdown(
                "Run edge-padded FRCRN on the uploaded recording. Muaalem evaluates "
                "five conservative blends and selects the one with the fewest "
                "reference phoneme edits; CPU processing can take several minutes."
            )
            frcrn_compare_btn = gr.Button(
                "تحسين ومقارنة - Enhance and compare", variant="secondary"
            )
            frcrn_raw_audio = gr.Audio(
                label="صوت FRCRN الخام - Raw FRCRN audio",
                type="numpy",
            )
            frcrn_selected_audio = gr.Audio(
                label="الصوت المحسن المختار - Selected preserved audio",
                type="numpy",
            )
            frcrn_comparison = gr.Markdown()
            with gr.Row():
                original_analysis = gr.HTML(
                    label="تحليل الصوت الأصلي - Original analysis"
                )
                raw_analysis = gr.HTML(
                    label="تحليل FRCRN الخام - Raw FRCRN analysis"
                )
                selected_analysis = gr.HTML(
                    label="تحليل الصوت المختار - Selected audio analysis"
                )
            frcrn_compare_btn.click(
                compare_audio_with_frcrn,
                inputs=[audio_input, sura_dropdown, aya_dropdown, start_idx, num_words],
                outputs=[
                    frcrn_raw_audio,
                    frcrn_selected_audio,
                    original_analysis,
                    raw_analysis,
                    selected_analysis,
                    frcrn_comparison,
                ],
                concurrency_limit=1,
            )

    with gr.Tab("إعدادات المصحف - Moshaf Settings"):
        gr.Markdown("# إعدادات خصائص المصحف")
        gr.Markdown("قم بتعديل خصائص المصحف حسب التلاوة المطلوبة")

        # Create settings inputs directly in the tab
        settings_components = []
        fields = MoshafAttributes.model_fields

        # Create inputs for all required fields
        for field_name in REQUIRED_MOSHAF_FIELDS:
            field_info = fields[field_name]
            input_component = create_gradio_input_for_field(
                field_name, field_info, getattr(default_moshaf, field_name, None)
            )
            settings_components.append(input_component)
            field_names.append(field_name)

        # Save button and status message
        with gr.Row():
            save_btn = gr.Button("حفظ الإعدادات - Save Settings", variant="primary")
            reset_btn = gr.Button("إعادة التعيين - Reset to Default")

        status_message = gr.Markdown()

        # Save settings event
        save_btn.click(
            update_moshaf_settings, inputs=settings_components, outputs=status_message
        )

        # Reset to default event
        reset_btn.click(
            reset_settings, inputs=[], outputs=settings_components + [status_message]
        )

    with gr.Tab("حول - About"):
        gr.Markdown("# حول المعلم القرآني - About Quran Muaalem")
        gr.Markdown(AI_ADVISORY_NOTICE)
        gr.Markdown(
            "الترخيص: [Quran Muaalem License (QML) v1.0]"
            "(https://github.com/obadx/quran-muaalem/blob/main/LICENSE)\n\n"
            "License: [Quran Muaalem License (QML) v1.0]"
            "(https://github.com/obadx/quran-muaalem/blob/main/LICENSE)"
        )


def main(
    app=app,
    server_name: str | None = None,
    server_port: int | None = None,
    share: bool | None = None,
    inbrowser: bool = False,
):
    import os

    if server_name is None:
        server_name = os.getenv("GRADIO_SERVER_NAME", "127.0.0.1")
    if server_port is None:
        server_port = int(os.getenv("GRADIO_SERVER_PORT", "7860"))
    if share is None:
        share = os.getenv("GRADIO_SHARE", "False").lower() in ("true", "1", "yes")

    app.launch(
        server_name=server_name,
        server_port=server_port,
        share=share,
        inbrowser=inbrowser,
    )


if __name__ == "__main__":
    main()
