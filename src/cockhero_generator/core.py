"""Core funscript processing API."""
from .runtime import load_standalone

_m = load_standalone()

valid_action = _m.valid_action
normalize_actions = _m.normalize_actions
remove_old_fallback_runs = _m.remove_old_fallback_runs
apply_forced_progressive_start = _m.apply_forced_progressive_start
soften_first_original_block = _m.soften_first_original_block
get_cycle_range = _m.get_cycle_range
calculate_dynamic_amplification = _m.calculate_dynamic_amplification
amplify_pos = _m.amplify_pos
apply_general_amplitude = _m.apply_general_amplitude
get_turning_points = _m.get_turning_points
find_recent_complete_cycles = _m.find_recent_complete_cycles
find_first_complete_cycles = _m.find_first_complete_cycles
build_pattern_from_cycles = _m.build_pattern_from_cycles
fill_initial_gap_from_first_cycles = _m.fill_initial_gap_from_first_cycles
fill_gap = _m.fill_gap
repeat_cycles = _m.repeat_cycles
repeat_single_cycle_exact = _m.repeat_single_cycle_exact
scale_cycle_amplitude = _m.scale_cycle_amplitude
fill_with_previous_and_next_cycle = _m.fill_with_previous_and_next_cycle
fill_with_recent_cycles = _m.fill_with_recent_cycles
smooth_gap_fill = _m.smooth_gap_fill
fade_end_fill = _m.fade_end_fill
process_file = _m.process_file
batch_main = _m.batch_main
