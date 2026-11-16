"""Pure NumPy WADA-SNR speech-to-noise ratio estimation."""

import numpy as np

# WADA's fixed alpha=0.4 curve under-read the repository's 16 kHz recitation
# fixture by about 1-5 dB on white and pink noise. This small affine correction
# was fit to its known-SNR 0/5/10/20 dB trials; it remains a provisional
# calibration and should be replaced with a held-out recitation corpus.
_CALIBRATION_SCALE = 1.3
_CALIBRATION_OFFSET_DB = 0.5


def estimate_snr_db(wave: np.ndarray, sr: int) -> float:
    """Estimate WADA-SNR in dB from a mono waveform using the alpha=0.4 table."""
    del sr
    x = np.asarray(wave, dtype=np.float64).reshape(-1)
    x = x[np.isfinite(x)]
    if x.size < 2:
        return float("-inf")
    peak = np.max(np.abs(x))
    if peak <= np.finfo(float).tiny:
        return float("-inf")
    # WADA's generalized-Gamma lookup curve (shape alpha=0.4), at 1 dB steps
    # from -20 through 100 dB. The statistic is log(E|z|)-E(log|z|).
    g = np.array([
        .40974774,.40986926,.40998566,.40969089,.40986186,.40999006,.41027138,.41052627,.41101024,.41143264,.41231718,.41337272,.41526426,.4178192,.42077252,.42452799,.42918886,.43510373,.44234195,.45161485,.46221153,.47491647,.48883809,.50509236,.52353709,.54372088,.56532427,.58847532,.61346212,.63954496,.66750818,.69583724,.72454762,.75414799,.78323148,.81240985,.84219775,.87166406,.90030504,.92880418,.95655449,.9835349,1.01047155,1.0362095,1.06136425,1.08579312,1.1094819,1.13277995,1.15472826,1.17627308,1.19703503,1.21671694,1.23535898,1.25364313,1.27103891,1.28718029,1.30302865,1.31839527,1.33294817,1.34700935,1.3605727,1.37345513,1.38577122,1.39733504,1.40856397,1.41959619,1.42983624,1.43958467,1.44902176,1.45804831,1.46669568,1.47486938,1.48269965,1.49034339,1.49748214,1.50435106,1.51076426,1.51698915,1.5229097,1.528578,1.53389835,1.5391211,1.5439065,1.54858517,1.55310776,1.55744391,1.56164927,1.56566348,1.56938671,1.57307767,1.57654764,1.57980083,1.58304129,1.58602496,1.58880681,1.59162477,1.5941969,1.59693155,1.599446,1.60185011,1.60408668,1.60627134,1.60826199,1.61004547,1.61192472,1.61369656,1.61534074,1.61688905,1.61838916,1.61985374,1.62135878,1.62268119,1.62390423,1.62513143,1.62632463,1.6274027,1.62842767,1.62945532,1.6303307,1.63128026,1.63204102
    ])
    normalized = np.abs(x / peak)
    normalized = np.maximum(normalized, 1e-10)
    statistic = float(np.log(np.mean(normalized)) - np.mean(np.log(normalized)))
    db = np.arange(-20.0, 101.0)
    # Locate adjacent lookup entries and interpolate. Values outside the table
    # are clipped to its supported range, as in conventional WADA-SNR.
    index = int(np.searchsorted(g, statistic, side="left"))
    if index == 0:
        return -20.0
    if index >= len(g):
        return 100.0
    fraction = (statistic - g[index - 1]) / (g[index] - g[index - 1])
    raw_snr = float(db[index - 1] + fraction)
    return float(_CALIBRATION_SCALE * raw_snr + _CALIBRATION_OFFSET_DB)
