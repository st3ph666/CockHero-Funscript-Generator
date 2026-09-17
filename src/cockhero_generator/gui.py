"""Tk GUI entry point."""
from .runtime import load_standalone

_m = load_standalone()

FunscriptGUI = _m.FunscriptGUI


def main() -> None:
    _m.gui_main()
