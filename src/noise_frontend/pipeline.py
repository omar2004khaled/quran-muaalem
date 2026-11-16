"""Composition adapter that preprocesses audio before Muaalem inference."""

from typing import Any

import numpy as np

from .frontend import FrontendResult, NoiseFrontend


class NoiseRobustMuaalem:
    """Compose a Muaalem instance with per-wave conditional enhancement.

    ``frontend_results`` contains the results from the most recent invocation.
    """

    def __init__(self, muaalem: Any, frontend: NoiseFrontend | None = None):
        self.muaalem = muaalem
        self.frontend = frontend or NoiseFrontend()
        self.frontend_results: list[FrontendResult] = []

    def __call__(self, waves, ref_quran_phonetic_script_list, sampling_rate: int,
                 **kwargs):
        if sampling_rate != 16000:
            raise ValueError("NoiseRobustMuaalem expects 16 kHz audio")
        self.frontend_results = [self.frontend.process(np.asarray(w), sampling_rate) for w in waves]
        return self.muaalem([r.wave for r in self.frontend_results],
                            ref_quran_phonetic_script_list,
                            sampling_rate=sampling_rate, **kwargs)
