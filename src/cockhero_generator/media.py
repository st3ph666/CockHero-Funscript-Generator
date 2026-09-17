"""Video, audio and MPV helpers."""
from .runtime import load_standalone

_m = load_standalone()

get_video_duration_ms = _m.get_video_duration_ms
EmbeddedMPV = _m.EmbeddedMPV
detect_audio_beats = _m.detect_audio_beats
estimate_visual_audio_phase = _m.estimate_visual_audio_phase
