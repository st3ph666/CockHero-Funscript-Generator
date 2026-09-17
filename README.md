# CockHero Funscript Generator

Advanced Python tool for creating, analyzing and optimizing `.funscript` files, including CockHero-oriented rhythmic generation and visual analysis.

![CockHero Funscript Generator v4.14.9](1CH.png)

## Current version

**v4.14.9**

Main standalone script:

`CockHero_Funscript_Generator_v4.14.9.py`

The tested standalone application is preserved, and a modular API is now available under `src/cockhero_generator/`.

## Modular structure

- `runtime.py` — compatibility loader for the tested standalone implementation
- `core.py` — funscript processing, normalization, cycles, gaps and fades
- `patterns.py` — CockHero rhythmic generation, episode presets and movement patterns
- `media.py` — video duration, MPV, audio beat and phase helpers
- `gui.py` — graphical interface entry point
- `__main__.py` — `python -m cockhero_generator` entry point

This layout keeps v4.14.9 behavior intact while providing clean modules for future development and refactoring.

## Features

- Graphical interface built with Tkinter
- Funscript analysis and optimization
- Video-based funscript generation
- CockHero BPM-based rhythmic generation
- Progressive start handling
- Automatic dead-time detection and filling
- Previous/next cycle mixing for gaps
- Smooth gap transitions
- End-of-video progressive fade
- General amplitude adjustment
- Detection/removal of repetitive fallback blocks
- 50 optional movement patterns
- Fixed and rhythmic episode presets
- Visual analysis tools
- Automatic video duration detection through `ffprobe`

## Requirements

- Python 3.10+
- NumPy
- FFmpeg / ffprobe
- Tkinter

On Debian/Ubuntu:

```bash
sudo apt install python3-tk ffmpeg
python3 -m pip install numpy
```

## Run standalone

```bash
python3 CockHero_Funscript_Generator_v4.14.9.py
```

## Run as a module

From an installed/editable checkout:

```bash
python3 -m pip install -e .
cockhero-funscript-generator
```

or:

```bash
python3 -m cockhero_generator
```

## Notes

The application works with `.funscript` files and supported video files in the selected/current working directory. Keep backups of original scripts before batch processing.

## Version history

### v4.14.9

- Current public release
- CockHero rhythmic generation and episode pattern support
- Visual analysis workflow
- Progressive movement generation and transition handling
- Expanded movement/pattern controls
- Modular `src/cockhero_generator/` API added without replacing the tested standalone application
- Application screenshot added to the project page

## Author

st3ph666
