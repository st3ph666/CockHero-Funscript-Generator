# CockHero Funscript Generator

Advanced Python tool for creating, analyzing and optimizing `.funscript` files, including CockHero-oriented rhythmic generation and visual analysis.

## Current version

**v4.14.9**

Main script:

`CockHero_Funscript_Generator_v4.14.9.py`

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

- Python 3
- NumPy
- FFmpeg / ffprobe
- Tkinter

On Debian/Ubuntu, Tkinter and FFmpeg can normally be installed with:

```bash
sudo apt install python3-tk ffmpeg
```

Install the Python dependency with:

```bash
python3 -m pip install numpy
```

## Run

```bash
python3 CockHero_Funscript_Generator_v4.14.9.py
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

## Author

st3ph666
