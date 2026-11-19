# Noise frontend

## Run the raw-versus-frontend comparison demo

From the repository root, run:

```powershell
.\run_noise_demo.bat
```

This uses the isolated `.venv-noise` environment and opens the Gradio demo at
`http://127.0.0.1:7860`. The page compares raw audio and frontend-processed
audio against the selected Quran reference, then recommends the path with fewer
phoneme edit errors. The comparison takes longer than a single analysis because
it runs the noise enhancer and performs Muaalem inference on both audio paths.
The first run in a new process can also take longer while models load.

DeepFilterNet 0.5.6 metadata currently conflicts with the repository's NumPy
requirement (`deepfilternet` requires NumPy below 2; Muaalem requires NumPy 2.2
or newer). Install the noise requirements without transitive dependencies,
then install the project and the other direct packages:

```powershell
python -m pip install -r requirements-noise.txt --no-deps
python -m pip install -e .
python -m pip install torch==2.8.0 torchaudio==2.8.0 librosa gradio soundfile pytest
python -m pip install --no-deps deepfilterlib==0.5.6
```

On Windows, building DeepFilterLib from source also requires the Rust toolchain,
Visual C++ Build Tools, and the Windows SDK. The compatible Torch/torchaudio
pair is pinned because torchaudio 2.11 removed an import used by DeepFilterNet
0.5.6.

Use a separate virtual environment, for example `.venv-noise`.
Wrap an existing model without changing its API:

```python
from noise_frontend.pipeline import NoiseRobustMuaalem

robust = NoiseRobustMuaalem(muaalem)
outputs = robust(waves, phonetizer_outputs, sampling_rate=16000)
for status in robust.frontend_results:
    if status.too_noisy:
        print("Too noisy; please re-record")
```

The default 20/10/0 dB thresholds and 10/20 dB attenuation values are starting
points. The WADA score has a provisional affine calibration based on the
repository's `assets/test.wav` with known white and pink noise at 0/5/10/20 dB;
it still needs validation on a held-out recitation/noise corpus. Tune thresholds
on held-out recitations by comparing phoneme error and each
sifa against the clean recording's Muaalem output; preserve short bursts,
nasality, fricatives, and madd durations. `enabled=False` bypasses enhancement.

Run `python scripts/eval_noise_frontend.py AUDIO_DIR NOISE_DIR --output results.csv`.
Each audio file must have a same-stem `.txt` containing its Uthmani reference;
the script uses `quran_phonetizer` and `MoshafAttributes` as shown in README.md.
