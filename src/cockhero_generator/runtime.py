"""Compatibility loader for the CockHero v4.14.9 standalone application.

The project keeps the tested standalone script as the single implementation source
while exposing a clean modular API under src/cockhero_generator.
"""
from __future__ import annotations

import importlib.util
from functools import lru_cache
from pathlib import Path
from types import ModuleType


@lru_cache(maxsize=1)
def load_standalone() -> ModuleType:
    root = Path(__file__).resolve().parents[2]
    script = root / "CockHero_Funscript_Generator_v4.14.9.py"
    if not script.is_file():
        raise FileNotFoundError(f"CockHero standalone script not found: {script}")

    spec = importlib.util.spec_from_file_location("cockhero_generator._standalone_v4_14_9", script)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load CockHero standalone script: {script}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
