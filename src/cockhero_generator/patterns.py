"""Pattern and CockHero generation API."""
from .runtime import load_standalone

_m = load_standalone()

PATTERN_NAMES = _m.PATTERN_NAMES
EPISODE_PATTERN_PRESETS = _m.EPISODE_PATTERN_PRESETS
EPISODE_PATTERN_NAMES = _m.EPISODE_PATTERN_NAMES
pattern_shape = _m._pattern_shape
fixed_episode_pattern_index = _m._fixed_episode_pattern_index
episode_pattern_range = _m._episode_pattern_range
fill_gap_with_custom_pattern = _m.fill_gap_with_custom_pattern
fill_end_with_custom_pattern = _m.fill_end_with_custom_pattern
generate_funscript_from_video_patterns = _m.generate_funscript_from_video_patterns
apply_custom_end_control = _m.apply_custom_end_control
generate_cockhero_actions = _m.generate_cockhero_actions
generate_cockhero_visual_full_range = _m.generate_cockhero_visual_full_range
