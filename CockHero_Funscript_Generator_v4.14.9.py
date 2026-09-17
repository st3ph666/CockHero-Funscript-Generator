#!/usr/bin/env python3
import json
import os
import sys
import subprocess

# ============================================================
# RÉGLAGES
# ============================================================

CURRENT_DIR = os.getcwd()

# Un intervalle égal ou supérieur à cette durée est considéré
# comme un temps mort.
DEADTIME_MS = 900
# Réglages d'amplitude
GENERAL_AMPLITUDE_SCALE = 1.40   # +40 % d'amplitude sur l'ensemble du script
INITIAL_AMPLITUDE_SCALE = 0.25   # progression douce conservée au tout début
FILL_AMPLITUDE_SCALE    = 0.70   # 50 % + 40 % = 70 % dans les temps morts

# Fenêtre de recherche des cycles précédents.
LOOKBACK_SECONDS = 10

# Durée acceptable d'un cycle complet.
MIN_CYCLE_MS = 180
MAX_CYCLE_MS = 6000

# Nombre maximal de cycles récents utilisés pour remplir un trou.
RECENT_CYCLES_TO_REPEAT = 3

# Remplissage des temps morts du milieu :
# 25 % avec le dernier cycle avant le trou, puis 75 % avec
# le premier cycle complet après le trou. L’amplitude descend
# progressivement sans jamais descendre sous 50 %, puis remonte.
MIX_PREVIOUS_NEXT_CYCLES = True
PREVIOUS_CYCLE_RATIO = 0.25
MIN_MIX_STRENGTH = 0.50

# Amplification dynamique des petits mouvements ajoutés.
MIN_FILL_RANGE = 50
FILL_MIN_POS = 5
FILL_MAX_POS = 95

# Mouvement de secours si aucun cycle n'est détecté.
# IMPORTANT : désactivé pour ne plus créer les blocs rouges 10 ↔ 90.
STEP_MS = 120
LOW = 10
HIGH = 90
GENERATE_FALLBACK_PATTERN = False

# Considère comme un vide toute longue alternance répétitive entre deux
# hauteurs fixes (le « bloc rouge » visible dans OpenFunscripter).
# Plus de 3 points à chaque hauteur = au moins 4 bas + 4 hauts = 8 points.
REMOVE_OLD_FALLBACK_RUNS = True
FALLBACK_MATCH_TOLERANCE = 2
FALLBACK_MIN_SAME_HEIGHT_POINTS = 4
FALLBACK_MIN_RUN_POINTS = FALLBACK_MIN_SAME_HEIGHT_POINTS * 2
# Évite de fusionner des points très éloignés qui ne forment pas un bloc continu.
FALLBACK_MAX_POINT_GAP_MS = DEADTIME_MS

# Début progressif : uniquement le premier cycle réel.
PROGRESSIVE_INITIAL_GAP = True
INITIAL_CYCLES_TO_USE = 2
INITIAL_START_STRENGTH = 0.0  # conservé pour compatibilité

# Remplissage du vide initial : utiliser les 20 premières secondes
# du mouvement original et faire croître leur amplitude de 5 % à 100 %.
INITIAL_SOURCE_WINDOW_MS = 20_000
INITIAL_GAP_START_STRENGTH = 0.0

# Détection automatique d’un long début presque vide. Les points isolés placés
# avant le premier vrai groupe de mouvements sont ignorés, puis les premiers
# cycles fiables sont recopiés vers le début avec la progression 0 -> 25 %.
AUTO_DETECT_REAL_MOTION_START = True
INITIAL_DENSE_RUN_POINTS = 12
INITIAL_MAX_DENSE_GAP_MS = 2_000
INITIAL_MIN_TURNING_POINTS = 4
INITIAL_SPARSE_PREFIX_MIN_MS = 10_000

# Le bloc initial reste volontairement limité à 25 % de l'amplitude.
# Il ne remonte jamais à 100 % avant le premier mouvement original.
INITIAL_SOFT_PHASE_RATIO = 1.0  # conservé uniquement pour compatibilité

# Durée de secours si aucun premier cycle complet ne peut être détecté.
FORCE_PROGRESSIVE_FIRST_MS = 15_000

# Espacement des points du tout premier cycle. Plus la valeur est grande,
# plus le mouvement initial est lent et composé de longues transitions.
FIRST_CYCLE_POINT_SPACING_MS = 120
INITIAL_CENTER = 50

# Va-et-vient du premier temps ajouté au début.
# Le reste du script garde STEP_MS = 120.
# 240 ms = deux fois plus lent que le mouvement de secours normal.
INITIAL_STEP_MS_START = 700    # très lent
INITIAL_STEP_MS_END = 120      # vitesse normale
INITIAL_SPEED_RAMP_MS = 10_000  # progression sur 10 secondes avant pleine intensité

# Adoucit également le premier bloc original après le vide initial.
# L'amplitude part de 25 % au premier point original et rejoint 100 %
# progressivement sur 10 secondes, sans rupture ni second traitement superposé.
SOFTEN_FIRST_ORIGINAL_BLOCK = True
FIRST_ORIGINAL_RAMP_MS = 10_000

# Transitions douces au début et à la fin des temps morts.
SMOOTH_GAP_TRANSITIONS = True
GAP_FADE_MS = 700

# Fin ajoutée jusqu’à la durée réelle de la vidéo : l’intensité diminue
# progressivement sur toute la longueur du remplissage.
END_FILL_FADE_ENABLED = True
END_FILL_FINAL_STRENGTH = 0.08  # 8 % de l’amplitude au tout dernier instant

# Mets None pour détecter automatiquement la durée réelle de chaque vidéo avec ffprobe.
# Ne pas mettre une durée fixe : elle empêcherait le remplissage après cette limite.
FORCED_DURATION_MS = None

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm"}


# ============================================================
# OUTILS DE BASE
# ============================================================

def valid_action(action):
    return (
        isinstance(action, dict)
        and "at" in action
        and "pos" in action
    )


def normalize_actions(actions):
    cleaned = []

    for action in actions:
        if not valid_action(action):
            continue

        try:
            at = int(action["at"])
            pos = max(0, min(100, int(action["pos"])))
        except (TypeError, ValueError):
            continue

        cleaned.append({"at": at, "pos": pos})

    cleaned.sort(key=lambda item: item["at"])

    unique = {}
    for action in cleaned:
        unique[action["at"]] = action

    return sorted(unique.values(), key=lambda item: item["at"])




def remove_old_fallback_runs(actions):
    """
    Supprime les longs « blocs rouges » répétitifs afin qu'ils soient ensuite
    traités comme des temps morts par la logique normale de remplissage.

    Un bloc est reconnu lorsqu'au moins quatre points reviennent à une première
    hauteur et au moins quatre points reviennent à une seconde hauteur :

        bas, haut, bas, haut, bas, haut, bas, haut...

    Les deux hauteurs peuvent être n'importe quelles valeurs (pas seulement
    10 et 90). Une petite tolérance est acceptée, par exemple 10/90 puis 11/89.
    """
    if not REMOVE_OLD_FALLBACK_RUNS or len(actions) < FALLBACK_MIN_RUN_POINTS:
        return actions, 0

    keep = [True] * len(actions)
    removed = 0
    i = 0

    while i <= len(actions) - FALLBACK_MIN_RUN_POINTS:
        first_pos = int(actions[i]["pos"])
        second_pos = int(actions[i + 1]["pos"])

        # Il faut deux hauteurs réellement différentes.
        if abs(second_pos - first_pos) <= FALLBACK_MATCH_TOLERANCE:
            i += 1
            continue

        j = i + 2

        while j < len(actions):
            dt = int(actions[j]["at"]) - int(actions[j - 1]["at"])
            if dt <= 0 or dt > FALLBACK_MAX_POINT_GAP_MS:
                break

            expected_pos = first_pos if (j - i) % 2 == 0 else second_pos
            if abs(int(actions[j]["pos"]) - expected_pos) > FALLBACK_MATCH_TOLERANCE:
                break

            j += 1

        run_length = j - i
        first_height_count = (run_length + 1) // 2
        second_height_count = run_length // 2

        if (
            first_height_count >= FALLBACK_MIN_SAME_HEIGHT_POINTS
            and second_height_count >= FALLBACK_MIN_SAME_HEIGHT_POINTS
        ):
            # Conserver les deux points situés juste avant/après le bloc permet
            # à process_file() de voir un grand intervalle et de le remplir avec
            # le mélange progressif des cycles précédent et suivant.
            for index in range(i, j):
                keep[index] = False
            removed += run_length
            i = j
        else:
            i += 1

    cleaned = [action for index, action in enumerate(actions) if keep[index]]
    return normalize_actions(cleaned), removed

def smoothstep(value):
    value = max(0.0, min(1.0, float(value)))
    return value * value * (3.0 - 2.0 * value)


def cosine_ease(value):
    """Progression cosinus très douce, sans départ ni arrivée brusques."""
    import math
    value = max(0.0, min(1.0, float(value)))
    return 0.5 - 0.5 * math.cos(math.pi * value)


def apply_forced_progressive_start(actions):
    """
    Adoucit réellement le début sans créer un gros segment droit.

    - conserve tous les points existants du début ;
    - ajoute des points intermédiaires au besoin ;
    - part exactement de la position 0 ;
    - fait monter progressivement le centre et l'amplitude ;
    - atteint 25 % de l'amplitude source après la rampe initiale.
    """
    if len(actions) < 2:
        return actions

    ramp_start_at = int(actions[0]["at"])
    ramp_end_at = ramp_start_at + max(1, int(INITIAL_SPEED_RAMP_MS))

    # Utiliser la zone initiale complète pour déterminer un centre stable.
    initial_window = [
        action for action in actions
        if int(action["at"]) <= ramp_end_at
    ]
    if not initial_window:
        return actions

    positions = [int(action["pos"]) for action in initial_window]
    source_center = (min(positions) + max(positions)) / 2.0

    # Densifier les grands intervalles afin d'éviter les gros triangles/blocs.
    max_spacing = max(40, min(120, int(INITIAL_STEP_MS_END)))
    dense_actions = []

    for left, right in zip(actions, actions[1:]):
        left_at = int(left["at"])
        right_at = int(right["at"])
        left_pos = int(left["pos"])
        right_pos = int(right["pos"])
        dense_actions.append({"at": left_at, "pos": left_pos})

        interval = right_at - left_at
        if left_at < ramp_end_at and interval > max_spacing:
            steps = interval // max_spacing
            for step in range(1, steps + 1):
                at = left_at + step * max_spacing
                if at >= right_at or at > ramp_end_at:
                    break
                ratio = (at - left_at) / max(1, interval)
                pos = left_pos + (right_pos - left_pos) * ratio
                dense_actions.append({"at": int(at), "pos": int(round(pos))})

    dense_actions.append({
        "at": int(actions[-1]["at"]),
        "pos": int(actions[-1]["pos"]),
    })
    dense_actions.sort(key=lambda item: item["at"])

    result = []
    for action in dense_actions:
        at = int(action["at"])
        original_pos = float(action["pos"])

        if at <= ramp_end_at:
            progress = max(0.0, min(1.0, (at - ramp_start_at) / max(1, ramp_end_at - ramp_start_at)))
            envelope = cosine_ease(progress)

            # Le centre monte lui aussi de 0 vers son niveau normal : aucun mur vertical au départ.
            progressive_center = source_center * envelope
            progressive_amplitude = INITIAL_AMPLITUDE_SCALE * envelope
            new_pos = progressive_center + (original_pos - source_center) * progressive_amplitude
        else:
            new_pos = original_pos

        result.append({
            "at": at,
            "pos": max(0, min(100, int(round(new_pos)))),
        })

    result[0]["pos"] = 0
    return normalize_actions(result)

def soften_first_original_block(actions):
    """
    Adoucit une seule fois le premier bloc original.

    Le premier point original commence à INITIAL_AMPLITUDE_SCALE, puis
    l'amplitude rejoint graduellement 100 % sur FIRST_ORIGINAL_RAMP_MS.
    Le centre du bloc reste stable, ce qui évite les murs verticaux.
    """
    if not SOFTEN_FIRST_ORIGINAL_BLOCK or len(actions) < 2:
        return actions

    start_at = int(actions[0]["at"])
    ramp_end_at = start_at + max(1, int(FIRST_ORIGINAL_RAMP_MS))

    window = [
        action for action in actions
        if start_at <= int(action["at"]) <= ramp_end_at
    ]
    if len(window) < 2:
        return actions

    positions = [int(action["pos"]) for action in window]
    center = (min(positions) + max(positions)) / 2.0

    result = []
    for action in actions:
        at = int(action["at"])
        pos = float(action["pos"])

        if at <= ramp_end_at:
            progress = (at - start_at) / max(1, ramp_end_at - start_at)
            strength = (
                INITIAL_AMPLITUDE_SCALE
                + (1.0 - INITIAL_AMPLITUDE_SCALE) * cosine_ease(progress)
            )
            pos = center + (pos - center) * strength

        result.append({
            "at": at,
            "pos": max(0, min(100, int(round(pos)))),
        })

    return normalize_actions(result)


def get_video_duration_ms(base_name):
    if FORCED_DURATION_MS is not None:
        return int(FORCED_DURATION_MS)

    for extension in VIDEO_EXTENSIONS:
        video_path = os.path.join(CURRENT_DIR, base_name + extension)

        if not os.path.exists(video_path):
            continue

        try:
            result = subprocess.check_output(
                [
                    "ffprobe",
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    video_path,
                ],
                stderr=subprocess.DEVNULL,
            )
            return int(float(result.decode().strip()) * 1000)
        except Exception:
            return None

    return None


# ============================================================
# AMPLIFICATION DYNAMIQUE
# ============================================================

def get_cycle_range(actions):
    positions = [int(action["pos"]) for action in actions]

    if not positions:
        return 0

    return max(positions) - min(positions)


def calculate_dynamic_amplification(actions):
    movement_range = get_cycle_range(actions)

    if movement_range <= 0:
        return 1.0

    if movement_range >= MIN_FILL_RANGE:
        return 1.0

    return MIN_FILL_RANGE / movement_range


def amplify_pos(pos, center, factor):
    amplified = center + (int(pos) - center) * factor

    return max(
        FILL_MIN_POS,
        min(FILL_MAX_POS, int(round(amplified)))
    )


def apply_general_amplitude(actions):
    """Augmente de 40 % l'amplitude générale autour du centre 50, limitée à 0..100."""
    result = []
    center = 50.0

    for action in actions:
        pos = float(action["pos"])
        amplified = center + (pos - center) * GENERAL_AMPLITUDE_SCALE
        result.append({
            "at": int(action["at"]),
            "pos": max(0, min(100, int(round(amplified)))),
        })

    return normalize_actions(result)


# ============================================================
# DÉTECTION DES CYCLES
# ============================================================

def get_turning_points(actions):
    if len(actions) < 3:
        return []

    turning_points = []
    previous_direction = 0

    for index in range(1, len(actions)):
        delta = actions[index]["pos"] - actions[index - 1]["pos"]

        if delta > 0:
            direction = 1
        elif delta < 0:
            direction = -1
        else:
            continue

        if previous_direction != 0 and direction != previous_direction:
            turning_points.append(index - 1)

        previous_direction = direction

    return turning_points


def find_recent_complete_cycles(actions, start_at, count):
    start_at = int(start_at)
    from_at = max(0, start_at - LOOKBACK_SECONDS * 1000)

    window = [
        action
        for action in actions
        if from_at <= action["at"] <= start_at
    ]

    turning_points = get_turning_points(window)

    if len(turning_points) < 3:
        return []

    cycles = []
    index = len(turning_points) - 1

    while index >= 2 and len(cycles) < count:
        start_index = turning_points[index - 2]
        end_index = turning_points[index]

        cycle = window[start_index:end_index + 1]

        if len(cycle) >= 3:
            duration = cycle[-1]["at"] - cycle[0]["at"]

            if MIN_CYCLE_MS <= duration <= MAX_CYCLE_MS:
                cycles.append(cycle)

        index -= 2

    cycles.reverse()
    return cycles


def find_first_complete_cycles(actions, count):
    turning_points = get_turning_points(actions)

    if len(turning_points) < 3:
        return []

    cycles = []
    index = 0

    while index + 2 < len(turning_points) and len(cycles) < count:
        start_index = turning_points[index]
        end_index = turning_points[index + 2]

        cycle = actions[start_index:end_index + 1]

        if len(cycle) >= 3:
            duration = cycle[-1]["at"] - cycle[0]["at"]

            if MIN_CYCLE_MS <= duration <= MAX_CYCLE_MS:
                cycles.append(cycle)

        index += 2

    return cycles


def build_pattern_from_cycles(cycles):
    if not cycles:
        return []

    base_at = cycles[0][0]["at"]
    pattern = []

    for cycle in cycles:
        for action in cycle:
            pattern.append(
                {
                    "dt": int(action["at"]) - base_at,
                    "pos": int(action["pos"]),
                }
            )

    unique = {}
    for point in pattern:
        unique[point["dt"]] = point

    return sorted(unique.values(), key=lambda point: point["dt"])


# ============================================================
# DÉBUT PROGRESSIF
# ============================================================

def get_initial_step_ms(current_time):
    """
    Calcule l'intervalle avant le prochain changement de direction.

    Au début : INITIAL_STEP_MS_START (très lent).
    Après INITIAL_SPEED_RAMP_MS : INITIAL_STEP_MS_END (vitesse normale).
    """
    if INITIAL_SPEED_RAMP_MS <= 0:
        return max(1, int(INITIAL_STEP_MS_END))

    progress = max(
        0.0,
        min(1.0, int(current_time) / INITIAL_SPEED_RAMP_MS)
    )

    eased = smoothstep(progress)

    step = (
        INITIAL_STEP_MS_START
        + (INITIAL_STEP_MS_END - INITIAL_STEP_MS_START) * eased
    )

    return max(1, int(round(step)))


def find_initial_real_motion_start_index(actions):
    """
    Trouve le premier groupe de mouvements réellement continu.

    Un ou plusieurs points isolés au début ne doivent pas empêcher le
    remplissage progressif d’un long vide. Le groupe retenu doit contenir
    plusieurs points rapprochés et plusieurs changements de direction.
    """
    if not AUTO_DETECT_REAL_MOTION_START or len(actions) < INITIAL_DENSE_RUN_POINTS:
        return 0

    run_size = max(4, int(INITIAL_DENSE_RUN_POINTS))

    for start_index in range(0, len(actions) - run_size + 1):
        window = actions[start_index:start_index + run_size]
        gaps = [
            int(window[i + 1]["at"]) - int(window[i]["at"])
            for i in range(len(window) - 1)
        ]

        if any(gap <= 0 or gap > INITIAL_MAX_DENSE_GAP_MS for gap in gaps):
            continue

        if len(get_turning_points(window)) < INITIAL_MIN_TURNING_POINTS:
            continue

        start_at = int(window[0]["at"])
        if start_at < INITIAL_SPARSE_PREFIX_MIN_MS:
            return 0

        return start_index

    return 0


def fill_initial_gap_from_first_cycles(actions, end_at):
    """
    Remplit le vide avant le premier point original avec les 20 premières
    secondes de mouvement qui suivent ce vide.

    La forme et le rythme de cette fenêtre source sont répétés autant de fois
    que nécessaire. L'amplitude commence à 5 % autour du centre de la fenêtre
    source et augmente graduellement jusqu'à 25 % au premier point original.
    La courbe de progression s'étire automatiquement sur toute la durée du vide.
    """
    end_at = int(end_at)

    if not PROGRESSIVE_INITIAL_GAP or end_at <= 0 or len(actions) < 2:
        return []

    source_start_at = int(actions[0]["at"])
    source_end_at = source_start_at + int(INITIAL_SOURCE_WINDOW_MS)

    source_actions = [
        action
        for action in actions
        if source_start_at <= int(action["at"]) <= source_end_at
    ]

    # Si moins de 20 secondes sont disponibles, utiliser tout ce qui existe.
    if len(source_actions) < 2:
        source_actions = actions[:]

    if len(source_actions) < 2:
        return []

    pattern = [
        {
            "dt": int(point["at"]) - source_start_at,
            "pos": int(point["pos"]),
        }
        for point in source_actions
    ]

    # Éliminer les éventuels points négatifs et doublons temporels.
    unique = {}
    for point in pattern:
        if point["dt"] >= 0:
            unique[int(point["dt"])] = point
    pattern = sorted(unique.values(), key=lambda point: point["dt"])

    if len(pattern) < 2:
        return []

    pattern_duration = int(pattern[-1]["dt"])
    if pattern_duration <= 0:
        return []

    positions = [int(point["pos"]) for point in pattern]
    source_min = min(positions)
    source_max = max(positions)
    source_center = (source_min + source_max) / 2.0
    first_original_pos = int(actions[0]["pos"])

    # Départ réel à zéro : évite le gros bloc vertical au commencement.
    filled = [{"at": 0, "pos": 0}]
    repetition = 0

    while True:
        added_this_loop = False

        for point in pattern:
            current_time = repetition * pattern_duration + int(point["dt"])

            if current_time <= 0:
                continue

            if current_time >= end_at:
                filled.sort(key=lambda item: item["at"])

                # Raccord très court vers le premier point original. Le bloc ajouté
                # reste toutefois limité à 25 % jusqu'à sa fin.
                fade_ms = min(120, max(1, end_at // 40))
                result = []

                for action in filled:
                    at = int(action["at"])
                    pos = float(action["pos"])

                    if at > end_at - fade_ms:
                        join_progress = cosine_ease(
                            (at - (end_at - fade_ms)) / fade_ms
                        )
                        pos = pos + (first_original_pos - pos) * join_progress

                    result.append(
                        {
                            "at": at,
                            "pos": max(0, min(100, int(round(pos)))),
                        }
                    )

                return result

            # Une seule progression sur toute la durée du vide :
            # 0 % -> 25 %. Le remplissage initial ne remonte jamais à 100 %,
            # ce qui élimine le gros bloc rouge à pleine amplitude.
            progress = max(0.0, min(1.0, current_time / end_at))
            eased = cosine_ease(progress)
            strength = (
                INITIAL_GAP_START_STRENGTH
                + (INITIAL_AMPLITUDE_SCALE - INITIAL_GAP_START_STRENGTH) * eased
            )

            # Le centre monte doucement depuis zéro, mais reste cohérent avec
            # l'enveloppe d'amplitude réduite.
            progressive_center = source_center * eased
            progressive_pos = (
                progressive_center
                + (int(point["pos"]) - source_center) * strength
            )

            filled.append(
                {
                    "at": int(current_time),
                    "pos": max(0, min(100, int(round(progressive_pos)))),
                }
            )
            added_this_loop = True

        if not added_this_loop:
            return []

        repetition += 1


# ============================================================
# MOUVEMENT DE SECOURS
# ============================================================

def fill_gap(start_at, end_at, start_pos=None):
    """
    Aucun mouvement de secours agressif n'est généré.

    Auparavant, cette fonction créait une alternance 10 ↔ 90 toutes les
    120 ms, visible comme de grands blocs rouges. Quand aucun cycle fiable
    n'est disponible, le temps mort reste maintenant vide.
    """
    if not GENERATE_FALLBACK_PATTERN:
        return []

    filled = []
    current_time = int(start_at) + STEP_MS
    use_high = True if start_pos is None else int(start_pos) <= 50

    while current_time < int(end_at):
        filled.append({
            "at": current_time,
            "pos": HIGH if use_high else LOW,
        })
        use_high = not use_high
        current_time += STEP_MS

    return filled


# ============================================================
# RÉPÉTITION DES CYCLES
# ============================================================

def repeat_cycles(cycles, start_at, end_at):
    start_at = int(start_at)
    end_at = int(end_at)

    pattern = build_pattern_from_cycles(cycles)

    if len(pattern) < 2:
        return []

    pattern_duration = pattern[-1]["dt"]

    if pattern_duration <= 0:
        return []

    all_points = [
        action
        for cycle in cycles
        for action in cycle
    ]

    positions = [point["pos"] for point in pattern]
    center = (min(positions) + max(positions)) / 2.0
    amplification = calculate_dynamic_amplification(all_points)

    filled = []
    repeat_number = 0

    while True:
        added_this_loop = False

        for point in pattern:
            new_at = (
                start_at
                + repeat_number * pattern_duration
                + point["dt"]
            )

            if new_at <= start_at:
                continue

            if new_at >= end_at:
                return filled

            filled.append(
                {
                    "at": int(new_at),
                    "pos": amplify_pos(
                        point["pos"],
                        center,
                        amplification
                    ),
                }
            )

            added_this_loop = True

        if not added_this_loop:
            return filled

        repeat_number += 1



def repeat_single_cycle_exact(cycle, start_at, end_at):
    """Répète un cycle sans modifier son amplitude ni son rythme."""
    if not cycle or len(cycle) < 2:
        return []

    start_at = int(start_at)
    end_at = int(end_at)
    base_at = int(cycle[0]["at"])
    pattern = [
        {"dt": int(point["at"]) - base_at, "pos": int(point["pos"])}
        for point in cycle
    ]
    duration = int(pattern[-1]["dt"])

    if duration <= 0:
        return []

    filled = []
    repetition = 0

    while True:
        added = False
        for point in pattern:
            at = start_at + repetition * duration + point["dt"]
            if at <= start_at:
                continue
            if at >= end_at:
                return filled
            filled.append({"at": int(at), "pos": int(point["pos"])})
            added = True
        if not added:
            return filled
        repetition += 1


def scale_cycle_amplitude(points, center, start_at, minimum_at, end_at):
    """
    Réduit puis rétablit progressivement l'amplitude autour du centre.

    - 70 % au début du trou ;
    - 50 % minimum au point de transition 25/75 ;
    - 70 % à la fin du trou.
    """
    if not points:
        return []

    result = []
    start_at = int(start_at)
    minimum_at = int(minimum_at)
    end_at = int(end_at)

    for point in points:
        at = int(point["at"])
        pos = int(point["pos"])

        if at <= minimum_at:
            duration = max(1, minimum_at - start_at)
            progress = smoothstep((at - start_at) / duration)
            strength = FILL_AMPLITUDE_SCALE + (MIN_MIX_STRENGTH - FILL_AMPLITUDE_SCALE) * cosine_ease(progress)
        else:
            duration = max(1, end_at - minimum_at)
            progress = smoothstep((at - minimum_at) / duration)
            strength = MIN_MIX_STRENGTH + (FILL_AMPLITUDE_SCALE - MIN_MIX_STRENGTH) * cosine_ease(progress)

        new_pos = center + (pos - center) * strength
        result.append({
            "at": at,
            "pos": max(0, min(100, int(round(new_pos)))),
        })

    return result


def fill_with_previous_and_next_cycle(actions, gap_index, start_at, end_at, start_pos):
    """
    Remplit un trou central avec :
      - 25 % du trou basé sur le dernier cycle complet avant le trou ;
      - 75 % du trou basé sur le premier cycle complet après le trou.

    L'amplitude part de 70 %, descend progressivement jusqu'à 50 % minimum
    au point de transition, puis remonte progressivement jusqu'à 70 %.
    """
    previous_actions = actions[:gap_index + 1]
    next_actions = actions[gap_index + 1:]

    previous_cycles = find_recent_complete_cycles(previous_actions, start_at, 1)
    next_cycles = find_first_complete_cycles(next_actions, 1)

    if not previous_cycles or not next_cycles:
        return fill_with_recent_cycles(previous_actions, start_at, end_at, start_pos)

    gap_duration = int(end_at) - int(start_at)
    transition_at = int(start_at + gap_duration * PREVIOUS_CYCLE_RATIO)

    first_part = repeat_single_cycle_exact(
        previous_cycles[-1],
        start_at,
        transition_at,
    )
    second_part = repeat_single_cycle_exact(
        next_cycles[0],
        transition_at,
        end_at,
    )

    previous_positions = [int(p["pos"]) for p in previous_cycles[-1]]
    next_positions = [int(p["pos"]) for p in next_cycles[0]]
    previous_center = (min(previous_positions) + max(previous_positions)) / 2.0
    next_center = (min(next_positions) + max(next_positions)) / 2.0

    first_part = scale_cycle_amplitude(
        first_part,
        previous_center,
        start_at,
        transition_at,
        end_at,
    )
    second_part = scale_cycle_amplitude(
        second_part,
        next_center,
        start_at,
        transition_at,
        end_at,
    )

    filler = sorted(first_part + second_part, key=lambda point: point["at"])

    if not filler:
        return fill_with_recent_cycles(previous_actions, start_at, end_at, start_pos)

    source_points = previous_cycles[-1] + next_cycles[0]
    return filler, True, get_cycle_range(source_points), 2


def fill_with_recent_cycles(actions, start_at, end_at, start_pos):
    cycles = find_recent_complete_cycles(
        actions,
        start_at,
        RECENT_CYCLES_TO_REPEAT
    )

    if cycles:
        repeated = repeat_cycles(cycles, start_at, end_at)

        if repeated:
            all_points = [
                action
                for cycle in cycles
                for action in cycle
            ]

            return (
                repeated,
                True,
                get_cycle_range(all_points),
                len(cycles),
            )

    return (
        fill_gap(start_at, end_at, start_pos),
        False,
        0,
        0,
    )


# ============================================================
# TRANSITIONS DOUCES DANS LES TEMPS MORTS
# ============================================================

def smooth_gap_fill(filler, start_at, end_at, start_pos, end_pos):
    if not SMOOTH_GAP_TRANSITIONS or not filler:
        return filler

    start_at = int(start_at)
    end_at = int(end_at)
    start_pos = int(start_pos)
    end_pos = int(end_pos)

    gap_duration = end_at - start_at

    if gap_duration <= 0:
        return filler

    fade_ms = min(GAP_FADE_MS, gap_duration // 2)

    if fade_ms <= 0:
        return filler

    smoothed = []

    for action in filler:
        at = int(action["at"])
        pos = int(action["pos"])

        # Raccord progressif au début du trou.
        if at < start_at + fade_ms:
            progress = smoothstep((at - start_at) / fade_ms)
            pos = start_pos + (pos - start_pos) * progress

        # Raccord progressif à la fin du trou.
        elif at > end_at - fade_ms:
            progress = smoothstep((end_at - at) / fade_ms)
            pos = end_pos + (pos - end_pos) * progress

        smoothed.append(
            {
                "at": at,
                "pos": max(0, min(100, int(round(pos)))),
            }
        )

    return smoothed


def fade_end_fill(filler, start_at, end_at, start_pos):
    """
    Réduit tranquillement l’amplitude du mouvement ajouté à la fin.

    L’amplitude commence à 100 % juste après le dernier mouvement original
    et descend avec une courbe douce jusqu’à END_FILL_FINAL_STRENGTH.
    Le centre du mouvement reste stable afin d’éviter une dérive vers le haut
    ou vers le bas.
    """
    if not END_FILL_FADE_ENABLED or not filler:
        return filler

    start_at = int(start_at)
    end_at = int(end_at)
    duration = max(1, end_at - start_at)

    positions = [int(action["pos"]) for action in filler]
    center = (min(positions) + max(positions)) / 2.0
    faded = []

    for action in filler:
        at = int(action["at"])
        pos = int(action["pos"])

        linear = max(0.0, min(1.0, (at - start_at) / duration))
        eased = smoothstep(linear)
        strength = 1.0 + (END_FILL_FINAL_STRENGTH - 1.0) * eased
        new_pos = center + (pos - center) * strength

        # Petit raccord au dernier point original.
        if at < start_at + GAP_FADE_MS:
            join = smoothstep((at - start_at) / max(1, GAP_FADE_MS))
            new_pos = int(start_pos) + (new_pos - int(start_pos)) * join

        faded.append({
            "at": at,
            "pos": max(0, min(100, int(round(new_pos)))),
        })

    return faded


# ============================================================
# TRAITEMENT D'UN FICHIER
# ============================================================

def process_file(input_path, filename):
    with open(input_path, "r", encoding="utf-8-sig") as file:
        data = json.load(file)

    actions = data.get("actions", [])

    if not isinstance(actions, list):
        return data, 0, 0, 0, 0, 0, 0, 0, 0

    actions = normalize_actions(actions)

    # Retire les blocs rouges répétitifs avant tout nouveau traitement.
    actions, removed_fallback_points = remove_old_fallback_runs(actions)

    # Ignore les points isolés qui précèdent le premier vrai groupe de mouvements.
    # Ils créeraient autrement un immense temps mort impossible à remplir, puisque
    # aucun cycle complet n’existe encore avant eux.
    real_start_index = find_initial_real_motion_start_index(actions)
    removed_sparse_prefix_points = 0
    detected_real_start_at = 0
    if real_start_index > 0:
        removed_sparse_prefix_points = real_start_index
        detected_real_start_at = int(actions[real_start_index]["at"])
        actions = actions[real_start_index:]

    # Une seule progression continue sur le premier bloc original.
    actions = soften_first_original_block(actions)

    if not actions:
        return data, 0, 0, 0, 0, 0, 0, 0, 0

    base_name = os.path.splitext(filename)[0]
    duration_ms = get_video_duration_ms(base_name)

    new_actions = []
    added = 0
    cycle_gaps_used = 0
    amplified_cycle_gaps = 0
    fallback_gaps_used = 0
    progressive_start_used = 0
    total_cycles_repeated = 0
    smoothed_gaps = 0

    first_at = actions[0]["at"]

    if removed_sparse_prefix_points:
        print(
            f"  Début réel détecté à {detected_real_start_at / 1000:.3f} s "
            f"({removed_sparse_prefix_points} point(s) isolé(s) ignoré(s))"
        )

    # Début du fichier.
    if first_at >= DEADTIME_MS:
        filler = fill_initial_gap_from_first_cycles(actions, first_at)

        if filler:
            new_actions.extend(filler)
            added += len(filler)
            progressive_start_used = 1
        else:
            # Cas exceptionnel : aucun remplissage possible.
            fallback_gaps_used += 1

    # Temps morts du milieu.
    for index in range(len(actions) - 1):
        current = actions[index]
        following = actions[index + 1]

        new_actions.append(current)

        current_at = current["at"]
        next_at = following["at"]

        if next_at - current_at >= DEADTIME_MS:
            if USE_CUSTOM_GAP_PATTERNS:
                filler = fill_gap_with_custom_pattern(
                    current_at,
                    next_at,
                    current["pos"],
                    following["pos"],
                    CUSTOM_GAP_SELECTED,
                )
                used_cycles = False
                original_range = 0
                cycles_count = 0
            elif MIX_PREVIOUS_NEXT_CYCLES:
                (
                    filler,
                    used_cycles,
                    original_range,
                    cycles_count,
                ) = fill_with_previous_and_next_cycle(
                    actions,
                    index,
                    current_at,
                    next_at,
                    current["pos"],
                )
            else:
                previous_actions = actions[:index + 1]
                (
                    filler,
                    used_cycles,
                    original_range,
                    cycles_count,
                ) = fill_with_recent_cycles(
                    previous_actions,
                    current_at,
                    next_at,
                    current["pos"],
                )

            filler = smooth_gap_fill(
                filler,
                current_at,
                next_at,
                current["pos"],
                following["pos"],
            )

            if filler:
                smoothed_gaps += 1

            new_actions.extend(filler)
            added += len(filler)

            if used_cycles:
                cycle_gaps_used += 1
                total_cycles_repeated += cycles_count

                if original_range < MIN_FILL_RANGE:
                    amplified_cycle_gaps += 1
            else:
                fallback_gaps_used += 1

    new_actions.append(actions[-1])

    # Fin du fichier.
    if duration_ms is not None:
        last_at = actions[-1]["at"]

        if duration_ms - last_at >= DEADTIME_MS:
            if USE_CUSTOM_END_PATTERNS:
                filler = fill_end_with_custom_pattern(
                    last_at,
                    duration_ms,
                    actions[-1]["pos"],
                    CUSTOM_END_SELECTED,
                )
                used_cycles = False
                original_range = 0
                cycles_count = 0
            else:
                (
                    filler,
                    used_cycles,
                    original_range,
                    cycles_count,
                ) = fill_with_recent_cycles(
                    actions,
                    last_at,
                    duration_ms,
                    actions[-1]["pos"],
                )

            # À la fin, l’amplitude diminue progressivement sur toute la
            # séquence ajoutée, y compris lorsqu'un pattern personnalisé est utilisé.
            filler = fade_end_fill(
                filler,
                last_at,
                duration_ms,
                actions[-1]["pos"],
            )

            if filler:
                smoothed_gaps += 1

            new_actions.extend(filler)
            added += len(filler)

            if used_cycles:
                cycle_gaps_used += 1
                total_cycles_repeated += cycles_count

                if original_range < MIN_FILL_RANGE:
                    amplified_cycle_gaps += 1
            else:
                fallback_gaps_used += 1

    # Ne pas appliquer une deuxième progression ici : elle créerait des blocs
    # rouges en retraitant le remplissage et le premier bloc original ensemble.
    final_actions = normalize_actions(new_actions)
    final_actions = apply_general_amplitude(final_actions)

    data["actions"] = final_actions

    return (
        data,
        len(actions),
        len(final_actions),
        added,
        cycle_gaps_used,
        amplified_cycle_gaps,
        fallback_gaps_used,
        progressive_start_used,
        total_cycles_repeated,
        smoothed_gaps,
    )


# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================

def batch_main():
    print(f"Répertoire traité : {CURRENT_DIR}")
    print(f"Temps mort minimum : {DEADTIME_MS} ms")
    print("Mouvement de secours 10 ↔ 90 : DÉSACTIVÉ")
    print("Blocs répétitifs (plus de 3 points par hauteur) considérés comme vides : ACTIVÉ")
    print(f"Cycles récents utilisés : jusqu'à {RECENT_CYCLES_TO_REPEAT}")
    print(f"Amplitude minimale ajoutée (secours/fin) : {MIN_FILL_RANGE}")
    print("Fin de vidéo : remplissage automatique jusqu’à la durée réelle détectée par ffprobe")
    print("Trous du milieu : 25 % dernier cycle + 75 % prochain cycle")
    print("Amplitude générale : +40 %")
    print("Amplitude des trous : 70 % -> 50 % minimum -> 70 % (progression cosinus)")
    print(f"Transition des trous : {GAP_FADE_MS} ms")
    print(
        "Premier cycle : départ à 0 puis montée progressive "
        "jusqu'à 25 % de son amplitude originale"
    )

    if PROGRESSIVE_INITIAL_GAP:
        print(
            "Début progressif : 20 secondes du prochain mouvement, "
            f"{INITIAL_GAP_START_STRENGTH * 100:g} % vers {INITIAL_AMPLITUDE_SCALE * 100:g} %, "
            "puis progression complète vers 100 % avant le premier mouvement"
        )

    filenames = sorted(
        filename
        for filename in os.listdir(CURRENT_DIR)
        if filename.lower().endswith(".funscript")
    )

    if not filenames:
        print("Aucun fichier .funscript trouvé.")
        return

    for filename in filenames:
        input_path = os.path.join(CURRENT_DIR, filename)
        output_path = input_path

        try:
            (
                data,
                before,
                after,
                added,
                cycle_gaps_used,
                amplified_cycle_gaps,
                fallback_gaps_used,
                progressive_start_used,
                total_cycles_repeated,
                smoothed_gaps,
            ) = process_file(input_path, filename)

            if before == 0:
                print(f"SKIP: {filename} — aucun mouvement trouvé")
                continue

            with open(output_path, "w", encoding="utf-8") as file:
                json.dump(
                    data,
                    file,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )

            print(
                f"OK: {filename} "
                f"| avant: {before} "
                f"| après: {after} "
                f"| ajoutés: {added} "
                f"| trous avec cycles: {cycle_gaps_used} "
                f"| cycles utilisés: {total_cycles_repeated} "
                f"| trous amplifiés (secours/fin): {amplified_cycle_gaps} "
                f"| secours: {fallback_gaps_used} "
                f"| début progressif: {progressive_start_used} "
                f"| trous adoucis: {smoothed_gaps}"
            )

        except Exception as error:
            print(f"ERREUR: {filename} -> {error}")

# ============================================================
# INTERFACE GRAPHIQUE
# ============================================================

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import tempfile
import wave
import threading
import queue
import numpy as np
import copy
import math
from pathlib import Path
import tkinter.font as tkfont
import socket
import time
import random
import uuid

APP_VERSION = "v4.14.9"

# Projet CockHero : analyse visuelle uniquement.

PARAMETERS = [
    ("Détection", "DEADTIME_MS", "Temps mort minimum (ms)", int, 900,
     "Intervalle à partir duquel un vide est considéré comme un temps mort."),
    ("Amplitude", "GENERAL_AMPLITUDE_SCALE", "Amplitude générale", float, 1.40,
     "1.40 = +40 % autour du centre 50."),
    ("Amplitude", "FILL_AMPLITUDE_SCALE", "Amplitude des trous", float, 0.70,
     "Amplitude au début et à la fin d'un trou."),
    ("Amplitude", "MIN_MIX_STRENGTH", "Minimum au centre du trou", float, 0.50,
     "Amplitude minimale au point de transition."),
    ("Amplitude", "MIN_FILL_RANGE", "Amplitude minimale ajoutée", int, 50,
     "Amplifie les petits cycles utilisés pour remplir les trous."),
    ("Mélange", "PREVIOUS_CYCLE_RATIO", "Part du cycle précédent", float, 0.25,
     "0.25 = 25 % du trou basé sur le cycle précédent."),
    ("Cycles", "LOOKBACK_SECONDS", "Recherche arrière (s)", int, 10,
     "Fenêtre utilisée pour rechercher les cycles précédents."),
    ("Cycles", "RECENT_CYCLES_TO_REPEAT", "Cycles récents à répéter", int, 3,
     "Nombre maximal de cycles utilisés pour le remplissage."),
    ("Cycles", "MIN_CYCLE_MS", "Cycle minimum (ms)", int, 180,
     "Durée minimale d'un cycle reconnu."),
    ("Cycles", "MAX_CYCLE_MS", "Cycle maximum (ms)", int, 6000,
     "Durée maximale d'un cycle reconnu."),
    ("Début", "INITIAL_SOURCE_WINDOW_MS", "Fenêtre source initiale (ms)", int, 20000,
     "Durée du mouvement source recopié vers le début."),
    ("Début", "INITIAL_AMPLITUDE_SCALE", "Amplitude fin du début", float, 0.25,
     "Amplitude atteinte juste avant le premier mouvement original."),
    ("Début", "INITIAL_STEP_MS_START", "Pas initial très lent (ms)", int, 700,
     "Valeur de départ pour la vitesse progressive."),
    ("Début", "INITIAL_STEP_MS_END", "Pas final (ms)", int, 120,
     "Valeur finale pour la vitesse progressive."),
    ("Début", "INITIAL_SPEED_RAMP_MS", "Rampe de vitesse (ms)", int, 10000,
     "Durée de la montée progressive de vitesse."),
    ("Début", "FIRST_ORIGINAL_RAMP_MS", "Rampe premier bloc (ms)", int, 10000,
     "Durée pour ramener le premier bloc original à pleine amplitude."),
    ("Transitions", "GAP_FADE_MS", "Transition des trous (ms)", int, 700,
     "Durée du raccord doux au début et à la fin des trous."),
    ("Fin", "END_FILL_FINAL_STRENGTH", "Amplitude finale", float, 0.08,
     "0.08 = 8 % au dernier instant de la vidéo."),
    ("Détection", "FALLBACK_MATCH_TOLERANCE", "Tolérance blocs répétitifs", int, 2,
     "Tolérance sur les hauteurs des anciennes alternances."),
    ("Détection", "FALLBACK_MIN_SAME_HEIGHT_POINTS", "Points par hauteur", int, 4,
     "Minimum de répétitions par hauteur pour retirer un ancien bloc."),
]

TOGGLES = [
    ("Détection", "REMOVE_OLD_FALLBACK_RUNS", "Retirer anciens blocs répétitifs"),
    ("Détection", "AUTO_DETECT_REAL_MOTION_START", "Détecter automatiquement le vrai début"),
    ("Mélange", "MIX_PREVIOUS_NEXT_CYCLES", "Mélanger cycle précédent / suivant"),
    ("Début", "PROGRESSIVE_INITIAL_GAP", "Remplissage progressif du début"),
    ("Début", "SOFTEN_FIRST_ORIGINAL_BLOCK", "Adoucir le premier bloc original"),
    ("Transitions", "SMOOTH_GAP_TRANSITIONS", "Transitions douces dans les trous"),
    ("Fin", "END_FILL_FADE_ENABLED", "Diminuer progressivement la fin"),
]



# ============================================================
# PATTERNS OPTIONNELS POUR LES TEMPS MORTS
# ============================================================

PATTERN_NAMES = [
    "01 - Pulse régulier",
    "02 - Pulse lent",
    "03 - Pulse rapide",
    "04 - Montée / descente",
    "05 - Escalier montant",
    "06 - Escalier descendant",
    "07 - Double pulse",
    "08 - Triple pulse",
    "09 - Vague douce",
    "10 - Vague forte",
    "11 - Centre court",
    "12 - Centre large",
    "13 - Bas dominant",
    "14 - Haut dominant",
    "15 - Alternance courte",
    "16 - Alternance longue",
    "17 - Accélération",
    "18 - Décélération",
    "19 - Accélération / freinage",
    "20 - Respiration",
    "21 - Battement 1-2",
    "22 - Battement 1-2-3",
    "23 - Battement progressif",
    "24 - Rebond",
    "25 - Rebond serré",
    "26 - Rebond large",
    "27 - Zigzag",
    "28 - Zigzag doux",
    "29 - Zigzag rapide",
    "30 - Marche 4 niveaux",
    "31 - Marche 6 niveaux",
    "32 - Ping-pong centre",
    "33 - Ping-pong large",
    "34 - Pulse irrégulier",
    "35 - Pulse aléatoire",
    "36 - Vague aléatoire",
    "37 - Bas -> haut lent",
    "38 - Haut -> bas lent",
    "39 - Bas -> haut rapide",
    "40 - Haut -> bas rapide",
    "41 - Montée double",
    "42 - Descente double",
    "43 - Compression douce",
    "44 - Compression forte",
    "45 - Expansion douce",
    "46 - Expansion forte",
    "47 - Mini pulses",
    "48 - Maxi pulses",
    "49 - Chaos contrôlé",
    "50 - Mix dynamique",
]


# ============================================================
# 12 PRESETS D'AMPLITUDE FIXE PAR ÉPISODE
# ============================================================
_EPISODE_AMPLITUDES = [
    (100, 50), (100, 40), (100, 30), (100, 25),
    (100, 15), (100, 5),  (90, 50),  (90, 35),
    (90, 20),  (80, 40), (80, 25),  (70, 20),
]

# Un preset d'épisode est maintenant un MOTIF FIXE de 1, 4 ou 8 temps.
# Le motif sélectionné est répété à l'identique pendant TOUT l'épisode.
# Il n'y a aucune rampe/progression interne : seule la plage peut changer
# d'un épisode au suivant.
EPISODE_PATTERN_PRESETS = []
for i, (high, low) in enumerate(_EPISODE_AMPLITUDES):
    EPISODE_PATTERN_PRESETS.append({
        "name": f"{i+1:03d} - Fixe {high}-{low}",
        "high": high,
        "low": low,
        "ranges": [(high, low)],
    })

# Motifs rythmiques fixes demandés. Chaque entrée représente UN temps/cycle.
# Exemple : 100-40 | 100-40 | 100-20 | 100-40, répété sans changement.
_EPISODE_RHYTHMIC_PATTERNS = [
    ("4T 100-40 | 100-40 | 100-20 | 100-40",
     [(100, 40), (100, 40), (100, 20), (100, 40)]),
    ("4T 100-50 | 100-50 | 100-40 | 100-50",
     [(100, 50), (100, 50), (100, 40), (100, 50)]),
    ("4T 100-40 | 100-40 | 100-30 | 100-40",
     [(100, 40), (100, 40), (100, 30), (100, 40)]),
    ("4T 100-30 | 100-30 | 100-10 | 100-30",
     [(100, 30), (100, 30), (100, 10), (100, 30)]),
    ("8T 100-40 x5 | 100-20 x2 | 100-40",
     [(100, 40), (100, 40), (100, 40), (100, 40),
      (100, 40), (100, 20), (100, 20), (100, 40)]),
]
for name, ranges in _EPISODE_RHYTHMIC_PATTERNS:
    EPISODE_PATTERN_PRESETS.append({
        "name": f"{len(EPISODE_PATTERN_PRESETS)+1:03d} - {name}",
        "high": ranges[0][0],
        "low": ranges[0][1],
        "ranges": ranges,
    })

EPISODE_PATTERN_NAMES = [p["name"] for p in EPISODE_PATTERN_PRESETS]


def _fixed_episode_pattern_index(preset_index):
    """Normalise l'index et garde la compatibilité avec les anciens presets."""
    try:
        value = int(preset_index)
    except Exception:
        value = 0
    return max(0, value) % len(EPISODE_PATTERN_PRESETS)


def _episode_pattern_range(preset_index, progress=None, beat_index=0):
    """Retourne la plage du temps demandé dans le motif fixe de l'épisode."""
    preset = EPISODE_PATTERN_PRESETS[_fixed_episode_pattern_index(preset_index)]
    ranges = preset.get("ranges") or [(preset["high"], preset["low"])]
    high, low = ranges[int(beat_index) % len(ranges)]
    return int(high), int(low)

USE_CUSTOM_GAP_PATTERNS = False
CUSTOM_GAP_PATTERN_RANDOM = True
CUSTOM_GAP_PATTERN_STEP_MS = 180
CUSTOM_GAP_PATTERN_MIN = 10
CUSTOM_GAP_PATTERN_MAX = 90
CUSTOM_GAP_SELECTED = [0]

# Patterns indépendants pour la fin de vidéo.
USE_CUSTOM_END_PATTERNS = False
CUSTOM_END_PATTERN_RANDOM = True
CUSTOM_END_PATTERN_STEP_MS = 220
CUSTOM_END_PATTERN_MIN = 10
CUSTOM_END_PATTERN_MAX = 90
CUSTOM_END_SELECTED = [8]

# Génération complète d'un funscript à partir de la vidéo seule.
VIDEO_GEN_PATTERN_RANDOM = True
VIDEO_GEN_PATTERN_STEP_MS = 180
VIDEO_GEN_PATTERN_MIN = 10
VIDEO_GEN_PATTERN_MAX = 90
VIDEO_GEN_SELECTED = [8]
VIDEO_GEN_BLOCK_MS = 4000
VIDEO_GEN_START_FADE_MS = 10000
VIDEO_GEN_END_FADE_MS = 10000
VIDEO_GEN_START_STRENGTH = 0.05
VIDEO_GEN_END_STRENGTH = 0.08

# Contrôle final commun aux scripts optimisés et générés.
CUSTOM_END_FADE_DURATION_MS = 10000
CUSTOM_END_TARGET_POS = 50
CUSTOM_END_HOLD_MS = 0

# Mode CockHero : génération rythmique basée sur un BPM.
COCKHERO_BPM = 120.0
COCKHERO_OFFSET_MS = 0
COCKHERO_BEATS_PER_HALF_CYCLE = 1.0
COCKHERO_MIN_POS = 10
COCKHERO_MAX_POS = 90
COCKHERO_ACCENT_EVERY = 4
COCKHERO_ACCENT_STRENGTH = 1.0
COCKHERO_NORMAL_STRENGTH = 0.75
COCKHERO_START_FADE_MS = 5000
COCKHERO_END_FADE_MS = 5000


def _pattern_shape(pattern_index, phase, cycle_index=0):
    """Retourne une valeur normalisée 0..1 pour l'un des 50 patterns."""
    import math
    p = int(pattern_index) % 50
    x = max(0.0, min(1.0, float(phase)))

    # Bases
    tri = 1.0 - abs(2.0 * x - 1.0)
    sine = 0.5 - 0.5 * math.cos(2.0 * math.pi * x)
    saw_up = x
    saw_down = 1.0 - x
    sq = 1.0 if x >= 0.5 else 0.0

    if p == 0: return sq
    if p == 1: return 1.0 if x >= 0.65 else 0.0
    if p == 2: return 1.0 if (x % 0.25) >= 0.125 else 0.0
    if p == 3: return tri
    if p == 4: return min(1.0, int(x * 5) / 4.0)
    if p == 5: return 1.0 - min(1.0, int(x * 5) / 4.0)
    if p == 6: return 1.0 if (x < 0.18 or 0.42 < x < 0.60) else 0.15
    if p == 7: return 1.0 if (x < 0.12 or 0.33 < x < 0.45 or 0.66 < x < 0.78) else 0.1
    if p == 8: return sine
    if p == 9: return sine ** 0.55
    if p == 10: return 0.35 + 0.3 * sine
    if p == 11: return 0.15 + 0.7 * sine
    if p == 12: return 0.15 + 0.45 * sine
    if p == 13: return 0.40 + 0.60 * sine
    if p == 14: return 1.0 if x < 0.30 else 0.0
    if p == 15: return 1.0 if x < 0.60 else 0.0
    if p == 16: return 1.0 if (x * (2 + 8*x)) % 1.0 > 0.5 else 0.0
    if p == 17: return 1.0 if (x * (10 - 8*x)) % 1.0 > 0.5 else 0.0
    if p == 18:
        speed = 2 + 8 * (1.0 - abs(2*x - 1))
        return 1.0 if (x * speed) % 1.0 > 0.5 else 0.0
    if p == 19: return 0.5 - 0.5 * math.cos(math.pi * x)
    if p == 20: return 1.0 if x < 0.22 else (0.35 if x < 0.55 else 0.0)
    if p == 21: return 1.0 if x < 0.13 else (0.65 if x < 0.35 else (0.35 if x < 0.58 else 0.0))
    if p == 22: return min(1.0, max(0.0, x * 1.7)) * sine
    if p == 23: return max(0.0, 1.0 - abs(3*x - 1.5))
    if p == 24: return max(0.0, 1.0 - abs(4*x - 2.0))
    if p == 25: return max(0.0, 1.0 - abs(2.5*x - 1.25))
    if p == 26: return (x * 4) % 1.0 if int(x*4) % 2 == 0 else 1.0 - ((x*4) % 1.0)
    if p == 27: return 0.25 + 0.5 * tri
    if p == 28: return 1.0 if int(x * 8) % 2 else 0.0
    if p == 29: return [0.0, 0.33, 0.66, 1.0][min(3, int(x*4))]
    if p == 30: return min(1.0, int(x*6)/5.0)
    if p == 31: return 0.35 + 0.3 * (1.0 if x >= 0.5 else 0.0)
    if p == 32: return 0.05 + 0.9 * sq
    if p == 33:
        slots = [0.0, 1.0, 0.2, 0.85, 0.35, 1.0, 0.1, 0.7]
        return slots[min(len(slots)-1, int(x*len(slots)))]
    if p == 34:
        # deterministic "random"
        val = math.sin((cycle_index + int(x*10) + 1) * 12.9898) * 43758.5453
        return val - math.floor(val)
    if p == 35:
        val = math.sin((cycle_index + int(x*12) + 7) * 78.233) * 12345.678
        r = val - math.floor(val)
        return 0.2 + 0.8 * r
    if p == 36: return saw_up
    if p == 37: return saw_down
    if p == 38: return (x * 2.0) % 1.0
    if p == 39: return 1.0 - ((x * 2.0) % 1.0)
    if p == 40: return min(1.0, x*2) if x < 0.5 else max(0.0, (x-0.5)*2)
    if p == 41: return max(0.0, 1.0 - (x*2 % 1.0))
    if p == 42: return 0.3 + 0.4 * sine
    if p == 43: return 0.05 + 0.9 * sine
    if p == 44: return 0.5 - 0.3 * sine
    if p == 45: return 0.5 - 0.48 * sine
    if p == 46: return 1.0 if (x*6) % 1.0 > 0.5 else 0.25
    if p == 47: return 1.0 if (x*2) % 1.0 > 0.5 else 0.0
    if p == 48:
        slots = [0.1, 0.85, 0.3, 1.0, 0.45, 0.7, 0.2, 0.95, 0.4, 0.75]
        return slots[min(len(slots)-1, int(x*len(slots)))]
    # 50 Mix dynamique
    return max(0.0, min(1.0, 0.5*sine + 0.5*tri))


def fill_gap_with_custom_pattern(start_at, end_at, start_pos, end_pos, pattern_indices):
    """Génère un pattern dans un temps mort central."""
    start_at = int(start_at)
    end_at = int(end_at)
    duration = end_at - start_at
    if duration <= 0:
        return []

    selected = [int(i) for i in pattern_indices if 0 <= int(i) < 50]
    if not selected:
        selected = [0]

    step = max(40, int(CUSTOM_GAP_PATTERN_STEP_MS))
    low = max(0, min(100, int(CUSTOM_GAP_PATTERN_MIN)))
    high = max(low, min(100, int(CUSTOM_GAP_PATTERN_MAX)))
    span = high - low

    # Change de pattern à chaque tranche de 2.5 s environ si plusieurs sont sélectionnés.
    block_ms = 2500
    points = []
    t = start_at + step
    point_index = 0

    while t < end_at:
        elapsed = t - start_at
        block_index = elapsed // block_ms

        if CUSTOM_GAP_PATTERN_RANDOM and len(selected) > 1:
            rnd = random.Random((start_at // 10) + int(block_index) * 7919)
            pattern_index = rnd.choice(selected)
        else:
            pattern_index = selected[int(block_index) % len(selected)]

        local = elapsed % block_ms
        phase = local / max(1, block_ms)
        value = _pattern_shape(pattern_index, phase, int(block_index))
        pos = low + value * span

        points.append({
            "at": int(t),
            "pos": max(0, min(100, int(round(pos))))
        })

        t += step
        point_index += 1

    # raccords existants du moteur
    return smooth_gap_fill(points, start_at, end_at, start_pos, end_pos)




def fill_end_with_custom_pattern(start_at, end_at, start_pos, pattern_indices):
    """
    Génère un pattern jusqu'à la fin de la vidéo.

    Contrairement aux trous du milieu, il n'y a pas de point original après la
    zone. On effectue donc seulement un raccord doux au début. La décroissance
    finale reste ensuite gérée par fade_end_fill().
    """
    start_at = int(start_at)
    end_at = int(end_at)
    duration = end_at - start_at
    if duration <= 0:
        return []

    selected = [int(i) for i in pattern_indices if 0 <= int(i) < 50]
    if not selected:
        selected = [0]

    step = max(40, int(CUSTOM_END_PATTERN_STEP_MS))
    low = max(0, min(100, int(CUSTOM_END_PATTERN_MIN)))
    high = max(low, min(100, int(CUSTOM_END_PATTERN_MAX)))
    span = high - low

    block_ms = 2500
    points = []
    t = start_at + step

    while t < end_at:
        elapsed = t - start_at
        block_index = elapsed // block_ms

        if CUSTOM_END_PATTERN_RANDOM and len(selected) > 1:
            rnd = random.Random((start_at // 10) + int(block_index) * 104729)
            pattern_index = rnd.choice(selected)
        else:
            pattern_index = selected[int(block_index) % len(selected)]

        local = elapsed % block_ms
        phase = local / max(1, block_ms)
        value = _pattern_shape(pattern_index, phase, int(block_index))
        pos = low + value * span

        # Raccord doux uniquement au début de la séquence.
        if elapsed < GAP_FADE_MS:
            join = smoothstep(elapsed / max(1, GAP_FADE_MS))
            pos = int(start_pos) + (pos - int(start_pos)) * join

        points.append({
            "at": int(t),
            "pos": max(0, min(100, int(round(pos))))
        })

        t += step

    return points




def generate_funscript_from_video_patterns(
    duration_ms,
    pattern_indices,
    step_ms=180,
    min_pos=10,
    max_pos=90,
    random_patterns=True,
    block_ms=4000,
    start_fade_ms=10000,
    end_fade_ms=10000,
    start_strength=0.05,
    end_strength=0.08,
):
    """
    Crée une trame complète uniquement à partir de la durée de la vidéo.

    Ce générateur n'analyse pas l'image de la vidéo : il synthétise un mouvement
    cohérent à l'aide des patterns intégrés. Il permet néanmoins de créer un
    .funscript complet lorsqu'aucun script n'existe.
    """
    duration_ms = max(1, int(duration_ms))
    step_ms = max(40, int(step_ms))
    min_pos = max(0, min(100, int(min_pos)))
    max_pos = max(min_pos, min(100, int(max_pos)))
    block_ms = max(step_ms * 2, int(block_ms))
    start_fade_ms = max(0, int(start_fade_ms))
    end_fade_ms = max(0, int(end_fade_ms))
    start_strength = max(0.0, min(1.0, float(start_strength)))
    end_strength = max(0.0, min(1.0, float(end_strength)))

    selected = [int(i) for i in pattern_indices if 0 <= int(i) < 50]
    if not selected:
        selected = [8]

    center = (min_pos + max_pos) / 2.0
    half_range = (max_pos - min_pos) / 2.0

    actions = [{"at": 0, "pos": int(round(center))}]
    t = step_ms

    while t < duration_ms:
        block_index = t // block_ms

        if random_patterns and len(selected) > 1:
            rnd = random.Random(0xF00D + int(block_index) * 65537)
            pattern_index = rnd.choice(selected)
        else:
            pattern_index = selected[int(block_index) % len(selected)]

        local = t % block_ms
        phase = local / max(1, block_ms)
        normalized = _pattern_shape(pattern_index, phase, int(block_index))

        # convertit 0..1 en amplitude autour du centre
        raw_pos = min_pos + normalized * (max_pos - min_pos)

        # Rampe douce au début
        start_strength_now = 1.0
        if start_fade_ms > 0 and t < start_fade_ms:
            progress = cosine_ease(t / max(1, start_fade_ms))
            start_strength_now = start_strength + (1.0 - start_strength) * progress

        # Rampe douce à la fin
        end_strength_now = 1.0
        remaining = duration_ms - t
        if end_fade_ms > 0 and remaining < end_fade_ms:
            progress = cosine_ease(remaining / max(1, end_fade_ms))
            end_strength_now = end_strength + (1.0 - end_strength) * progress

        strength = min(start_strength_now, end_strength_now)
        pos = center + (raw_pos - center) * strength

        actions.append({
            "at": int(t),
            "pos": max(0, min(100, int(round(pos))))
        })

        t += step_ms

    # point final stable
    if actions[-1]["at"] < duration_ms:
        actions.append({
            "at": int(duration_ms),
            "pos": int(round(center))
        })

    return normalize_actions(actions)




def apply_custom_end_control(actions, duration_ms):
    """Applique une descente finale réglable vers une position cible."""
    if not actions or not END_FILL_FADE_ENABLED:
        return actions

    duration_ms = int(duration_ms)
    fade_ms = max(0, int(CUSTOM_END_FADE_DURATION_MS))
    hold_ms = max(0, int(CUSTOM_END_HOLD_MS))
    target = max(0, min(100, int(CUSTOM_END_TARGET_POS)))
    final_strength = max(0.0, min(1.0, float(END_FILL_FINAL_STRENGTH)))

    fade_end = max(0, duration_ms - hold_ms)
    fade_start = max(0, fade_end - fade_ms)

    result = []
    for a in actions:
        at = int(a["at"])
        pos = int(a["pos"])

        if hold_ms > 0 and at >= fade_end:
            pos = target
        elif fade_ms > 0 and at >= fade_start:
            progress = max(0.0, min(1.0, (at - fade_start) / max(1, fade_ms)))
            eased = cosine_ease(progress)
            # Réduit l'écart à la position cible jusqu'à la force finale.
            strength = 1.0 - (1.0 - final_strength) * eased
            pos = target + (pos - target) * strength

        result.append({
            "at": at,
            "pos": max(0, min(100, int(round(pos))))
        })

    if result:
        if hold_ms > 0:
            result[-1]["pos"] = target
        elif result[-1]["at"] >= fade_start:
            last = result[-1]
            last["pos"] = max(
                0, min(100, int(round(target + (last["pos"] - target) * final_strength)))
            )

    return normalize_actions(result)




def generate_cockhero_actions(
    duration_ms,
    bpm=120.0,
    offset_ms=0,
    beats_per_half_cycle=1.0,
    min_pos=10,
    max_pos=90,
    accent_every=4,
    accent_strength=1.0,
    normal_strength=0.75,
    start_fade_ms=5000,
    end_fade_ms=5000,
):
    """
    Génère une trame rythmique CockHero.

    Le mouvement alterne entre bas et haut sur les battements.
    beats_per_half_cycle=1.0 -> un battement pour aller d'une extrémité à l'autre.
    2.0 -> mouvement deux fois plus lent.
    0.5 -> deux demi-cycles par battement.
    """
    duration_ms = max(1, int(duration_ms))
    bpm = max(1.0, float(bpm))
    offset_ms = max(0, int(offset_ms))
    beats_per_half_cycle = max(0.125, float(beats_per_half_cycle))
    min_pos = max(0, min(100, int(min_pos)))
    max_pos = max(min_pos, min(100, int(max_pos)))
    accent_every = max(1, int(accent_every))
    accent_strength = max(0.0, min(1.5, float(accent_strength)))
    normal_strength = max(0.0, min(1.5, float(normal_strength)))
    start_fade_ms = max(0, int(start_fade_ms))
    end_fade_ms = max(0, int(end_fade_ms))

    beat_ms = 60000.0 / bpm
    half_cycle_ms = beat_ms * beats_per_half_cycle

    center = (min_pos + max_pos) / 2.0
    half_range = (max_pos - min_pos) / 2.0

    actions = [{"at": 0, "pos": int(round(center))}]

    # Reste centré jusqu'au premier battement.
    if offset_ms > 0 and offset_ms < duration_ms:
        actions.append({"at": int(offset_ms), "pos": int(round(center))})

    t = float(offset_ms)
    half_index = 0

    while t <= duration_ms:
        target_sign = -1 if (half_index % 2 == 0) else 1

        # Un accent tous les N demi-cycles. Cela donne un mouvement plus large.
        accent = ((half_index + 1) % accent_every == 0)
        strength = accent_strength if accent else normal_strength

        # Rampe au début.
        if start_fade_ms > 0 and t < offset_ms + start_fade_ms:
            p = max(0.0, min(1.0, (t - offset_ms) / max(1.0, start_fade_ms)))
            strength *= cosine_ease(p)

        # Rampe à la fin.
        remaining = duration_ms - t
        if end_fade_ms > 0 and remaining < end_fade_ms:
            p = max(0.0, min(1.0, remaining / max(1.0, end_fade_ms)))
            strength *= cosine_ease(p)

        pos = center + target_sign * half_range * strength
        actions.append({
            "at": int(round(t)),
            "pos": max(0, min(100, int(round(pos))))
        })

        half_index += 1
        t = offset_ms + half_index * half_cycle_ms

    if actions[-1]["at"] < duration_ms:
        actions.append({
            "at": int(duration_ms),
            "pos": int(round(center))
        })
    else:
        actions[-1]["at"] = int(duration_ms)

    return normalize_actions(actions)





def generate_cockhero_visual_full_range(
    duration_ms,
    event_times_ms,
    min_pos=10,
    max_pos=90,
    logic="marker_peaks",
    progress_cb=None,
):
    """
    CockHero visuel v4.6.3.

    Deux logiques sélectionnables :

    marker_peaks
        Chaque marqueur bleu = sommet.
        Un creux est ajouté au milieu entre deux marqueurs.
        C'est la logique v4.6.2.

    marker_alternating
        Chaque marqueur bleu = extrémité directe du mouvement.
        Les positions alternent haut / bas / haut / bas.
        Aucun point intermédiaire n'est ajouté.
    """
    duration_ms = max(1, int(duration_ms))
    events = sorted({
        int(t) for t in event_times_ms
        if 0 <= int(t) <= duration_ms
    })
    if not events:
        return []

    min_pos = max(0, min(100, int(min_pos)))
    max_pos = max(min_pos, min(100, int(max_pos)))

    actions = []

    if logic == "marker_alternating":
        # Chaque marqueur = une extrémité du mouvement.
        if events[0] > 0:
            actions.append({"at": 0, "pos": min_pos})

        total_events = max(1, len(events))
        progress_step = max(1, total_events // 100)

        for i, t in enumerate(events):
            pos = max_pos if i % 2 == 0 else min_pos
            actions.append({"at": int(t), "pos": int(pos)})

            if progress_cb and (i % progress_step == 0 or i == total_events - 1):
                progress_cb((i + 1) / total_events)

        if actions and actions[-1]["at"] < duration_ms:
            actions.append({
                "at": duration_ms,
                "pos": int(actions[-1]["pos"]),
            })

    else:
        # Logique v4.6.2 : chaque marqueur = sommet,
        # creux au milieu du marqueur suivant.
        if events[0] > 0:
            actions.append({"at": 0, "pos": max_pos})

        total_events = max(1, len(events))
        progress_step = max(1, total_events // 100)

        for i, t in enumerate(events):
            actions.append({"at": int(t), "pos": int(max_pos)})

            if progress_cb and (i % progress_step == 0 or i == total_events - 1):
                progress_cb((i + 1) / total_events)

            if i < len(events) - 1:
                next_t = int(events[i + 1])
                mid_t = int(round((int(t) + next_t) / 2.0))
                if int(t) < mid_t < next_t:
                    actions.append({
                        "at": mid_t,
                        "pos": int(min_pos),
                    })

        if actions and actions[-1]["at"] < duration_ms:
            actions.append({
                "at": duration_ms,
                "pos": int(max_pos),
            })

    return normalize_actions(actions)




class EmbeddedMPV:
    def __init__(self, canvas, status_callback=None):
        self.canvas = canvas
        self.status_callback = status_callback
        self.process = None
        self.socket_path = f"/tmp/funscript-optimisator-{os.getpid()}-{uuid.uuid4().hex}.sock"
        self.video_path = None
        self._last_error = None

    def _status(self, text):
        if self.status_callback:
            self.status_callback(text)

    def start(self, video_path):
        self.stop()
        self.video_path = str(video_path)
        self.canvas.update_idletasks()
        wid = self.canvas.winfo_id()

        cmd = [
            "mpv",
            "--no-terminal",
            "--force-window=yes",
            f"--wid={wid}",
            f"--input-ipc-server={self.socket_path}",
            "--keep-open=yes",
            "--idle=no",
            "--pause=yes",
            "--osd-level=1",
            "--hwdec=auto-safe",
            self.video_path,
        ]

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._status(f"Vidéo chargée : {Path(self.video_path).name}")
            return True
        except FileNotFoundError:
            self._status("mpv n'est pas installé.")
            self.process = None
            return False
        except Exception as exc:
            self._status(f"Erreur mpv : {exc}")
            self.process = None
            return False

    def stop(self):
        try:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=1.0)
                except Exception:
                    self.process.kill()
        except Exception:
            pass
        self.process = None
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        except Exception:
            pass

    def _ipc(self, command, timeout=0.15):
        if not self.process or self.process.poll() is not None:
            return None
        if not os.path.exists(self.socket_path):
            return None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(self.socket_path)
            payload = json.dumps({"command": command}).encode("utf-8") + b"\n"
            sock.sendall(payload)
            data = b""
            while b"\n" not in data:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
            sock.close()
            if not data:
                return None
            return json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
        except Exception:
            return None

    def command(self, *command):
        return self._ipc(list(command))

    def toggle_pause(self):
        paused = self.get_property("pause")
        if paused is None:
            return
        self.command("set_property", "pause", not bool(paused))

    def set_pause(self, value):
        self.command("set_property", "pause", bool(value))

    def seek_absolute(self, seconds):
        self.command("seek", float(seconds), "absolute+exact")

    def get_property(self, name):
        response = self._ipc(["get_property", name])
        if isinstance(response, dict) and response.get("error") == "success":
            return response.get("data")
        return None

    def time_pos(self):
        value = self.get_property("time-pos")
        try:
            return float(value)
        except Exception:
            return None

    def duration(self):
        value = self.get_property("duration")
        try:
            return float(value)
        except Exception:
            return None




def detect_audio_beats(video_path, min_gap_ms=90, sensitivity=2.8):
    """Détecte des transitoires audio sans dépendance Python externe.

    ffmpeg extrait une piste mono 8 kHz temporaire. Le signal est ensuite
    transformé en enveloppe d'énergie courte. Les pics robustes retournés
    servent uniquement de références de synchronisation; ils ne créent pas
    directement de mouvements.
    """
    if not video_path or not os.path.isfile(video_path):
        return []

    wav_path = None
    try:
        fd, wav_path = tempfile.mkstemp(prefix="cockhero_audio_", suffix=".wav")
        os.close(fd)
        cmd = [
            "ffmpeg", "-v", "error", "-y", "-i", video_path,
            "-vn", "-ac", "1", "-ar", "8000", "-acodec", "pcm_s16le", wav_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True, timeout=180)

        with wave.open(wav_path, "rb") as wf:
            rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())

        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        if samples.size < rate:
            return []

        samples = np.abs(samples)
        hop = max(1, int(rate * 0.010))       # 10 ms
        win = max(hop, int(rate * 0.030))    # 30 ms
        n = max(0, (samples.size - win) // hop + 1)
        if n < 8:
            return []

        energy = np.empty(n, dtype=np.float32)
        for i in range(n):
            block = samples[i * hop:i * hop + win]
            energy[i] = float(np.mean(block))

        # Flux positif de l'enveloppe : robuste aux passages musicaux soutenus.
        flux = np.maximum(0.0, np.diff(energy, prepend=energy[0]))
        med = float(np.median(flux))
        mad = float(np.median(np.abs(flux - med))) + 1e-6
        threshold = med + float(sensitivity) * 1.4826 * mad

        beats = []
        min_gap_bins = max(1, int(round(min_gap_ms / 10.0)))
        last_i = -min_gap_bins
        for i in range(1, len(flux) - 1):
            if (flux[i] >= threshold and flux[i] >= flux[i - 1]
                    and flux[i] > flux[i + 1] and i - last_i >= min_gap_bins):
                beats.append(int(round(i * hop * 1000.0 / rate)))
                last_i = i
        return beats
    except Exception:
        return []
    finally:
        if wav_path:
            try:
                os.unlink(wav_path)
            except OSError:
                pass


def estimate_visual_audio_phase(events, audio_beats, max_steps=8, tolerance_ms=85):
    """Détecte une erreur de phase en nombre de passages."""
    if len(events) < 8 or len(audio_beats) < 8:
        return 0, 0.0, 0, 0.0

    ev = np.asarray(events, dtype=np.float64)
    beats = np.asarray(audio_beats, dtype=np.float64)

    diffs = np.diff(ev)
    usable = diffs[(diffs >= 90.0) & (diffs <= 1500.0)]
    if usable.size < 4:
        return 0, 0.0, 0, 0.0

    step_ms = float(np.median(usable))
    sample = ev[:min(len(ev), 1200)]

    def score_shift(shift_ms):
        shifted = sample + shift_ms
        pos = np.searchsorted(beats, shifted)
        distances = np.full(shifted.shape, 1e9, dtype=np.float64)

        right = pos < beats.size
        if np.any(right):
            distances[right] = np.minimum(
                distances[right],
                np.abs(beats[pos[right]] - shifted[right])
            )

        left = pos > 0
        if np.any(left):
            distances[left] = np.minimum(
                distances[left],
                np.abs(beats[pos[left] - 1] - shifted[left])
            )

        good = distances <= float(tolerance_ms)
        matches = int(np.count_nonzero(good))
        closeness = float(np.sum(
            np.clip(1.0 - distances[good] / tolerance_ms, 0.0, 1.0)
        ))
        return matches + 0.35 * closeness, matches

    candidates = []
    for phase in range(-int(max_steps), int(max_steps) + 1):
        shift_ms = float(phase) * step_ms
        score, matches = score_shift(shift_ms)
        candidates.append((score, matches, phase, shift_ms))

    candidates.sort(reverse=True, key=lambda x: (x[0], x[1], -abs(x[2])))
    best_score, best_matches, best_phase, best_shift = candidates[0]
    zero = next(c for c in candidates if c[2] == 0)

    # Ne corriger que si la phase gagnante est nettement meilleure que la phase 0.
    if best_phase != 0 and (best_score - zero[0]) < max(5.0, 0.015 * len(sample)):
        return 0, 0.0, int(zero[1]), float(zero[0])

    return int(best_phase), float(best_shift), int(best_matches), float(best_score)


class FunscriptGUI(tk.Tk):
    def __init__(self):
        super().__init__()

        self.config_dir = Path.home() / ".config" / "funscript_optimisator"
        self.config_file = self.config_dir / "config.json"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.saved_config = self._load_app_config()

        self.title(f"CockHero Funscript Generator - {APP_VERSION}")
        self.geometry(self.saved_config.get("geometry", "1860x980"))
        self.minsize(1250, 760)

        self.current_file = None
        self.current_video = None
        self.original_data = None
        self.preview_data = None
        self.param_vars = {}
        self.toggle_vars = {}

        # v4.14.6 — génération automatique / traitement par lot.
        self.visual_auto_generate_pending = False
        self.batch_videos = []
        self.batch_index = 0
        self.batch_results = []
        self.batch_running = False
        self.batch_profile_id = None
        self.batch_progress_window = None
        self.batch_video_progress_var = tk.DoubleVar(value=0.0)
        self.batch_total_progress_var = tk.DoubleVar(value=0.0)
        self.batch_status_var = tk.StringVar(value="Prêt")
        self.batch_detail_var = tk.StringVar(value="")
        # v4.14.6 — protection des funscripts existants en traitement par lot.
        self.batch_overwrite_existing_var = tk.BooleanVar(value=False)

        self.status_var = tk.StringVar(value="Prêt")
        self.file_var = tk.StringVar(value="Aucun fichier")
        self.video_var = tk.StringVar(value="Aucune vidéo")
        self.current_video_name_var = tk.StringVar(value="VIDÉO : aucune")
        self.current_funscript_name_var = tk.StringVar(value="FUNSCRIPT : aucun")
        self.show_original = tk.BooleanVar(value=True)
        self.show_preview = tk.BooleanVar(value=True)
        self.auto_preview = tk.BooleanVar(value=False)
        self.show_detection_events = tk.BooleanVar(value=True)

        self.use_patterns_var = tk.BooleanVar(value=False)
        self.pattern_random_var = tk.BooleanVar(value=True)
        self.pattern_step_var = tk.StringVar(value="180")
        self.pattern_min_var = tk.StringVar(value="10")
        self.pattern_max_var = tk.StringVar(value="90")

        self.use_end_patterns_var = tk.BooleanVar(value=False)
        self.end_pattern_random_var = tk.BooleanVar(value=True)
        self.end_pattern_step_var = tk.StringVar(value="220")
        self.end_pattern_min_var = tk.StringVar(value="10")
        self.end_pattern_max_var = tk.StringVar(value="90")

        # Génération depuis vidéo seule
        self.video_gen_random_var = tk.BooleanVar(value=True)
        self.video_gen_step_var = tk.StringVar(value="180")
        self.video_gen_min_var = tk.StringVar(value="10")
        self.video_gen_max_var = tk.StringVar(value="90")
        self.video_gen_block_var = tk.StringVar(value="4000")
        self.video_gen_start_fade_var = tk.StringVar(value="10000")
        self.video_gen_end_fade_var = tk.StringVar(value="10000")
        self.video_gen_start_strength_var = tk.StringVar(value="0.05")
        self.video_gen_end_strength_var = tk.StringVar(value="0.08")

        # Contrôle détaillé de la fin, miroir du contrôle du début.
        self.end_fade_enabled_var = tk.BooleanVar(value=True)
        self.end_fade_duration_var = tk.StringVar(value="10000")
        self.end_final_strength_var = tk.StringVar(value="0.08")
        self.end_target_pos_var = tk.StringVar(value="50")
        self.end_hold_ms_var = tk.StringVar(value="0")

        # Mode CockHero
        self.cockhero_bpm_var = tk.StringVar(value="120")
        self.cockhero_offset_var = tk.StringVar(value="0")
        self.cockhero_beats_half_var = tk.StringVar(value="1")
        self.cockhero_min_var = tk.StringVar(value="10")
        self.cockhero_max_var = tk.StringVar(value="90")
        self.cockhero_accent_every_var = tk.StringVar(value="4")
        self.cockhero_accent_strength_var = tk.StringVar(value="1.0")
        self.cockhero_normal_strength_var = tk.StringVar(value="0.75")
        self.cockhero_start_fade_var = tk.StringVar(value="5000")
        self.cockhero_end_fade_var = tk.StringVar(value="5000")

        # Mode d'interface
        self.interface_mode = tk.StringVar(value="cockhero_visual")

        # Résultats de détection CockHero
        # Détection visuelle CockHero : zone basse configurable.
        self.visual_roi_top_var = tk.StringVar(value=str(self.saved_config.get("visual_roi_top", "72")))
        self.visual_roi_bottom_var = tk.StringVar(value=str(self.saved_config.get("visual_roi_bottom", "100")))
        self.visual_threshold_var = tk.StringVar(value=str(self.saved_config.get("visual_threshold", "18")))
        self.visual_min_interval_var = tk.StringVar(value=str(self.saved_config.get("visual_min_interval", "180")))
        self.visual_sample_fps_var = tk.StringVar(value=str(self.saved_config.get("visual_sample_fps", "12")))
        self.use_visual_beats_var = tk.BooleanVar(value=bool(self.saved_config.get("use_visual_beats", False)))
        self.detected_visual_times_ms = []
        self.visual_detection_status_var = tk.StringVar(value="Aucun indicateur visuel analysé")

        # Mode CockHero Visuel : détection de toute forme qui atteint une ligne.
        self.visual_line_y_var = tk.StringVar(value="97")
        self.visual_line_band_var = tk.StringVar(value="6")
        self.visual_line_threshold_var = tk.StringVar(value="14")
        self.visual_line_min_changed_var = tk.StringVar(value="3.0")
        self.visual_line_interval_var = tk.StringVar(value="140")
        self.visual_line_fps_var = tk.StringVar(value="60")
        self.visual_sync_offset_var = tk.StringVar(value="-35")
        self.visual_target_x_var = tk.StringVar(value="45")
        self.visual_target_width_var = tk.StringVar(value="2.4")
        self.visual_auto_target_var = tk.BooleanVar(value=False)
        self.visual_ignore_right_half_var = tk.BooleanVar(
            value=bool(self.saved_config.get("visual_ignore_right_half", True))
        )
        self.visual_reverse_analysis_var = tk.BooleanVar(value=False)

        # Post-traitement des franchissements
        self.visual_merge_window_var = tk.StringVar(value="170")
        self.visual_auto_start_var = tk.BooleanVar(value=True)
        self.visual_start_window_var = tk.StringVar(value="5000")
        self.visual_start_min_events_var = tk.StringVar(value="8")

        # Récupération prudente des indicateurs manqués.
        # Le moteur principal reste inchangé; cette passe ne travaille que
        # dans les grands trous laissés après le nettoyage.
        self.visual_recovery_var = tk.BooleanVar(value=True)
        self.visual_recovery_gap_var = tk.StringVar(value="650")
        self.visual_recovery_factor_var = tk.StringVar(value="0.82")

        self.visual_raw_count = 0
        self.visual_merged_count = 0
        self.visual_started_count = 0

        self.visual_generation_logic_var = tk.StringVar(value="marker_peaks")
        # Épisodes CockHero : une pause > seuil crée un nouvel épisode.
        self.visual_episode_gap_var = tk.StringVar(
            value=str(self.saved_config.get("visual_episode_gap_ms", "3000"))
        )
        self.visual_episode_high_var = tk.StringVar(value="100")
        self.visual_episode_low_var = tk.StringVar(value="50")
        self.visual_episode_high_deg_var = tk.StringVar(value="180")
        self.visual_episode_low_deg_var = tk.StringVar(value="90")
        self.visual_episode_name_var = tk.StringVar(value="")
        self.visual_episode_enabled_var = tk.BooleanVar(value=True)
        self.visual_episode_invert_var = tk.BooleanVar(value=False)
        self.visual_episode_start_side_var = tk.StringVar(value="high")
        self.visual_episode_offset_var = tk.StringVar(value="0")
        self.visual_episode_pause_mode_var = tk.StringVar(value="hold")
        self.visual_episode_color_var = tk.StringVar(value="#ff6b6b")
        self.visual_episode_pattern_var = tk.StringVar(value=EPISODE_PATTERN_NAMES[0])
        self.visual_episodes = []
        self.visual_episode_palette = [
            "#ff6b6b", "#4dabf7", "#ffd43b", "#69db7c",
            "#b197fc", "#ffa94d", "#38d9a9", "#f783ac"
        ]
        # Édition manuelle de la trame générée.
        self.track_edit_enabled_var = tk.BooleanVar(value=True)
        self.track_edit_selected_index = None
        self.track_edit_dragging = False
        self.track_edit_pick_radius = 9
        self.track_edit_mode_var = tk.StringVar(value="points")
        self.track_context_canvas = None
        self.track_context_kind = None
        self.track_context_event = None
        self.track_locked_var = tk.BooleanVar(value=False)
        self.track_sync_total_ms = 0
        self.track_sync_offset_ms_var = tk.StringVar(value="Offset : +0 ms")
        self.loaded_marker_times_ms = []
        self.loaded_marker_source_logic = None
        self.visual_line_status_var = tk.StringVar(value="Piste visuelle non analysée")
        self.visual_audio_status_var = tk.StringVar(value="AUDIO : EN ATTENTE | Beats : 0 | Correspondances : 0 | Offset : +0 ms | Correction : NON")
        self.detected_line_times_ms = []
        self.use_line_events_var = tk.BooleanVar(value=bool(self.saved_config.get("use_line_events", True)))

        # Analyse asynchrone : garde l'interface réactive et affiche la progression.
        self.analysis_running = False
        self.analysis_cancel_requested = False
        self.analysis_queue = queue.Queue()
        self.analysis_progress_var = tk.DoubleVar(value=0.0)
        self.analysis_stage_var = tk.StringVar(value="Prêt")

        self.zoom_start = 0.0
        self.zoom_end = 1.0
        self.dragging_detail = False
        self.detail_drag_x = 0
        self.detail_drag_window = (0.0, 1.0)

        self.playhead_ms = 0

        # Lecture / diagnostic visuel
        self.follow_detail_var = tk.BooleanVar(value=True)
        self.motion_pos = 50.0
        self.motion_last_pos = 50.0
        self._updating_seek_scale = False

        self._configure_theme()
        self._build_ui()

        self.mpv = EmbeddedMPV(self.video_canvas, self._set_status)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(250, self._tick_video)

        # Le projet est maintenant centré sur la génération CockHero.
        # On restaure d'abord la dernière vidéo; l'ancien funscript n'est chargé
        # automatiquement que si aucune vidéo n'est disponible.
        last_video = self.saved_config.get("last_video")
        last_file = self.saved_config.get("last_file")
        if last_video and Path(last_video).exists():
            self.after(350, lambda: self.load_video(last_video))
        elif last_file and Path(last_file).exists():
            self.after(350, lambda: self.load_file(last_file))

    # ------------------------------------------------------------
    # CONFIG
    # ------------------------------------------------------------
    def _load_app_config(self):
        try:
            if self.config_file.exists():
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
        return {}
        try:
            self.batch_overwrite_existing_var.set(bool(data.get("batch_overwrite_existing", False)))
        except Exception:
            pass

    def _save_app_config(self):
        try:
            data["batch_overwrite_existing"] = bool(self.batch_overwrite_existing_var.get())
        except Exception:
            pass
        try:
            data = {
                "last_file": (
                    self.current_file
                    if self.current_file and Path(self.current_file).exists()
                    else self.saved_config.get("last_file")
                ),
                "last_video": self.current_video,
                "last_dir": str(Path(self.current_file).parent) if self.current_file else self.saved_config.get("last_dir", ""),
                "geometry": self.geometry(),
                "use_patterns": bool(self.use_patterns_var.get()),
                "pattern_random": bool(self.pattern_random_var.get()),
                "pattern_step": self.pattern_step_var.get(),
                "pattern_min": self.pattern_min_var.get(),
                "pattern_max": self.pattern_max_var.get(),
                "selected_patterns": list(self.pattern_list.curselection()) if hasattr(self, "pattern_list") else [0],
                "use_end_patterns": bool(self.use_end_patterns_var.get()),
                "end_pattern_random": bool(self.end_pattern_random_var.get()),
                "end_pattern_step": self.end_pattern_step_var.get(),
                "end_pattern_min": self.end_pattern_min_var.get(),
                "end_pattern_max": self.end_pattern_max_var.get(),
                "selected_end_patterns": list(self.end_pattern_list.curselection()) if hasattr(self, "end_pattern_list") else [8],
                "video_gen_random": bool(self.video_gen_random_var.get()),
                "video_gen_step": self.video_gen_step_var.get(),
                "video_gen_min": self.video_gen_min_var.get(),
                "video_gen_max": self.video_gen_max_var.get(),
                "video_gen_block": self.video_gen_block_var.get(),
                "video_gen_start_fade": self.video_gen_start_fade_var.get(),
                "video_gen_end_fade": self.video_gen_end_fade_var.get(),
                "video_gen_start_strength": self.video_gen_start_strength_var.get(),
                "video_gen_end_strength": self.video_gen_end_strength_var.get(),
                "selected_video_gen_patterns": list(self.video_gen_pattern_list.curselection()) if hasattr(self, "video_gen_pattern_list") else [8],
                "end_fade_enabled": bool(self.end_fade_enabled_var.get()),
                "end_fade_duration": self.end_fade_duration_var.get(),
                "end_final_strength": self.end_final_strength_var.get(),
                "end_target_pos": self.end_target_pos_var.get(),
                "end_hold_ms": self.end_hold_ms_var.get(),
                "cockhero_bpm": self.cockhero_bpm_var.get(),
                "cockhero_offset": self.cockhero_offset_var.get(),
                "cockhero_beats_half": self.cockhero_beats_half_var.get(),
                "cockhero_min": self.cockhero_min_var.get(),
                "cockhero_max": self.cockhero_max_var.get(),
                "cockhero_accent_every": self.cockhero_accent_every_var.get(),
                "cockhero_accent_strength": self.cockhero_accent_strength_var.get(),
                "cockhero_normal_strength": self.cockhero_normal_strength_var.get(),
                "cockhero_start_fade": self.cockhero_start_fade_var.get(),
                "cockhero_end_fade": self.cockhero_end_fade_var.get(),
                "interface_mode": self.interface_mode.get(),
                "visual_roi_top": self.visual_roi_top_var.get(),
                "visual_roi_bottom": self.visual_roi_bottom_var.get(),
                "visual_threshold": self.visual_threshold_var.get(),
                "visual_min_interval": self.visual_min_interval_var.get(),
                "visual_sample_fps": self.visual_sample_fps_var.get(),
                "use_visual_beats": bool(self.use_visual_beats_var.get()),
                "visual_line_y": self.visual_line_y_var.get(),
                "visual_line_band": self.visual_line_band_var.get(),
                "visual_line_threshold": self.visual_line_threshold_var.get(),
                "visual_line_min_changed": self.visual_line_min_changed_var.get(),
                "visual_line_interval": self.visual_line_interval_var.get(),
                "visual_line_fps": self.visual_line_fps_var.get(),
                "visual_sync_offset": self.visual_sync_offset_var.get(),
                "visual_target_x": self.visual_target_x_var.get(),
                "visual_target_width": self.visual_target_width_var.get(),
                "visual_auto_target": bool(self.visual_auto_target_var.get()),
                "visual_ignore_right_half": bool(self.visual_ignore_right_half_var.get()),
                "visual_reverse_analysis": bool(self.visual_reverse_analysis_var.get()),
                "visual_merge_window": self.visual_merge_window_var.get(),
                "visual_auto_start": bool(self.visual_auto_start_var.get()),
                "visual_start_window": self.visual_start_window_var.get(),
                "visual_start_min_events": self.visual_start_min_events_var.get(),
                "visual_recovery": bool(self.visual_recovery_var.get()),
                "visual_recovery_gap": self.visual_recovery_gap_var.get(),
                "visual_recovery_factor": self.visual_recovery_factor_var.get(),
                "visual_generation_logic": self.visual_generation_logic_var.get(),
                "visual_episode_gap_ms": self.visual_episode_gap_var.get(),
                "use_line_events": bool(self.use_line_events_var.get()),
            }
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _on_close(self):
        try:
            self.mpv.stop()
        except Exception:
            pass
        self._save_app_config()
        self.destroy()

    def _set_status(self, text):
        self.status_var.set(str(text))

    # ------------------------------------------------------------
    # THEME
    # ------------------------------------------------------------
    def _configure_theme(self):
        self.COL_BG = "#090d10"
        self.COL_PANEL = "#11171c"
        self.COL_PANEL2 = "#182127"
        self.COL_BORDER = "#27333c"
        self.COL_TEXT = "#e6edf2"
        self.COL_MUTED = "#82939d"
        self.COL_GREEN = "#38e28b"
        self.COL_BLUE = "#46a8ff"
        self.COL_RED = "#ff5965"
        self.COL_GRID = "#1d2a31"

        self.configure(bg=self.COL_BG)

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(".", font=("Sans", 9))
        style.configure("TFrame", background=self.COL_BG)
        style.configure("Panel.TFrame", background=self.COL_PANEL)
        style.configure("Toolbar.TFrame", background=self.COL_PANEL2)
        style.configure("TLabel", background=self.COL_BG, foreground=self.COL_TEXT)
        style.configure("Panel.TLabel", background=self.COL_PANEL, foreground=self.COL_TEXT)
        style.configure("Toolbar.TLabel", background=self.COL_PANEL2, foreground=self.COL_TEXT)
        style.configure("Muted.TLabel", background=self.COL_BG, foreground=self.COL_MUTED)

        style.configure("TLabelframe", background=self.COL_PANEL, foreground=self.COL_TEXT, bordercolor=self.COL_BORDER)
        style.configure("TLabelframe.Label", background=self.COL_PANEL, foreground=self.COL_GREEN, font=("Sans", 9, "bold"))

        style.configure("TButton", background=self.COL_PANEL2, foreground=self.COL_TEXT, bordercolor=self.COL_BORDER, padding=(8, 4), width=0)
        style.map("TButton", background=[("active", "#22303a")])
        style.configure("Accent.TButton", background="#1c6f4a", foreground="#ffffff", padding=(8, 4))
        style.map("Accent.TButton", background=[("active", "#24895c")])

        style.configure("TEntry", fieldbackground="#0d1317", foreground=self.COL_TEXT, insertcolor=self.COL_TEXT, bordercolor=self.COL_BORDER)
        style.configure("TCombobox", fieldbackground="#0d1317", foreground=self.COL_TEXT)
        style.map("TCombobox", fieldbackground=[("readonly", "#0d1317")],
                  foreground=[("readonly", self.COL_TEXT)],
                  selectbackground=[("readonly", "#1c6f4a")],
                  selectforeground=[("readonly", "#ffffff")])
        style.configure("Treeview", background="#0d1317", fieldbackground="#0d1317",
                        foreground=self.COL_TEXT, rowheight=22)
        style.map("Treeview", background=[("selected", "#1c6f4a")],
                  foreground=[("selected", "#ffffff")])
        style.configure("TCheckbutton", background=self.COL_PANEL, foreground=self.COL_TEXT)
        style.configure("TRadiobutton", background=self.COL_PANEL, foreground=self.COL_TEXT)
        style.map("TRadiobutton", background=[("active", self.COL_PANEL2)])
        style.configure("Horizontal.TProgressbar", troughcolor="#0d1317", background=self.COL_GREEN)
        style.configure("TScrollbar", background=self.COL_PANEL2, troughcolor=self.COL_BG)
        style.configure("TNotebook", background=self.COL_BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=self.COL_PANEL2, foreground=self.COL_TEXT, padding=(8, 3))
        style.map("TNotebook.Tab", background=[("selected", self.COL_PANEL)])

    # ------------------------------------------------------------
    # BUILD UI
    # ------------------------------------------------------------
    def _scrollable_panel(self, parent):
        """Panneau dont le contenu ne peut pas agrandir les zones de travail."""
        panel = ttk.Frame(parent, style="Panel.TFrame")
        panel.rowconfigure(0, weight=1)
        panel.columnconfigure(0, weight=1)
        canvas = tk.Canvas(panel, width=360, height=1, bg=self.COL_PANEL,
                           highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(panel, orient="vertical", command=canvas.yview)
        horizontal = ttk.Scrollbar(panel, orient="horizontal", command=canvas.xview)
        body = ttk.Frame(canvas, style="Panel.TFrame")
        window = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        pending = [None]

        def layout():
            pending[0] = None
            width = max(1, canvas.winfo_width())
            needed = body.winfo_reqwidth()
            window_width = max(width, needed)
            if int(float(canvas.itemcget(window, "width"))) != window_width:
                canvas.itemconfigure(window, width=window_width)
            canvas.configure(scrollregion=canvas.bbox("all"))
            if body.winfo_reqheight() > canvas.winfo_height():
                vertical.grid(row=0, column=1, sticky="ns")
            else:
                vertical.grid_remove()
                canvas.yview_moveto(0)
            if needed > width:
                horizontal.grid(row=1, column=0, sticky="ew")
            else:
                horizontal.grid_remove()
                canvas.xview_moveto(0)

        def schedule(_event=None):
            if pending[0] is None:
                pending[0] = self.after(16, layout)

        body.bind("<Configure>", schedule)
        canvas.bind("<Configure>", schedule)
        record = (panel, canvas, body)
        self._scroll_panels.append(record)

        def cleanup(event):
            if event.widget is panel:
                if pending[0] is not None:
                    self.after_cancel(pending[0])
                if record in self._scroll_panels:
                    self._scroll_panels.remove(record)

        panel.bind("<Destroy>", cleanup)
        return panel, body

    def _scroll_settings(self, event):
        """Molette locale : conserve le défilement natif des listes et tableaux."""
        widget = event.widget
        if widget.winfo_class() in ("Listbox", "Treeview", "TCombobox", "TScrollbar"):
            return
        for panel, canvas, body in self._scroll_panels:
            if str(widget).startswith(str(panel) + "."):
                delta = -1 if getattr(event, "num", None) == 4 else 1
                if getattr(event, "delta", 0):
                    delta = -1 if event.delta > 0 else 1
                canvas.yview_scroll(delta * 3, "units")
                return

    def _reveal_setting(self, event):
        """La navigation au clavier amène le contrôle focalisé dans la vue."""
        widget = event.widget
        for panel, canvas, body in self._scroll_panels:
            if not str(widget).startswith(str(body) + ".") or not canvas.winfo_ismapped():
                continue
            y = widget.winfo_rooty() - body.winfo_rooty()
            top = canvas.canvasy(0)
            if y < top or y + widget.winfo_height() > top + canvas.winfo_height():
                target = y if y < top else y + widget.winfo_height() - canvas.winfo_height()
                canvas.yview_moveto(max(0, target) / max(1, body.winfo_height()))
            x = widget.winfo_rootx() - body.winfo_rootx()
            left = canvas.canvasx(0)
            if x < left or x + widget.winfo_width() > left + canvas.winfo_width():
                target = x if x < left else x + widget.winfo_width() - canvas.winfo_width()
                canvas.xview_moveto(max(0, target) / max(1, body.winfo_width()))
            break

    def _flow_controls(self, frame, widgets=None):
        """Barre qui passe sur plusieurs lignes au lieu de masquer ses boutons."""
        items = list(widgets if widgets is not None else frame.pack_slaves())
        if getattr(frame, "_flow_job", None):
            self.after_cancel(frame._flow_job)
        for widget in frame.winfo_children():
            widget.pack_forget()
            widget.grid_forget()
            widget.place_forget()
        frame.pack_propagate(False)
        frame._flow_job = None

        def layout():
            frame._flow_job = None
            width = max(1, frame.winfo_width())
            row = used = 0
            line_height = max((widget.winfo_reqheight() for widget in items), default=24) + 2
            for widget in items:
                needed = widget.winfo_reqwidth() + 6
                if used and used + needed > width:
                    row += 1
                    used = 0
                widget.place(x=used, y=row * line_height + 1,
                             width=min(needed - 6, width), height=line_height - 2)
                used += needed
            height = (row + 1) * line_height
            if int(frame.cget("height")) != height:
                frame.configure(height=height)

        def schedule(_event=None):
            if frame._flow_job is None:
                frame._flow_job = self.after(16, layout)

        frame.bind("<Configure>", schedule)
        schedule()

    def _fit_workspace(self, _event=None):
        """Initialise les séparateurs, puis protège les tailles utiles au redimensionnement."""
        if getattr(_event, "num", None) == 1:
            self._layout_dragging = False
            if _event.widget is self.workspace_paned:
                height = max(1, self.workspace_paned.winfo_height())
                self._workspace_ratios = tuple(self.workspace_paned.sashpos(i) / height for i in (0, 1))
        elif getattr(self, "_layout_dragging", False):
            return
        if getattr(self, "_layout_job", None):
            self.after_cancel(self._layout_job)
        delay = 25 if getattr(self, "_workspace_positioned", False) else 120
        self._layout_job = self.after(delay, self._position_workspace)

    def _start_layout_drag(self, _event=None):
        self._layout_dragging = True
        if getattr(self, "_layout_job", None):
            self.after_cancel(self._layout_job)
            self._layout_job = None

    def _position_workspace(self):
        self._layout_job = None
        if not getattr(self, "_workspace_ready", False):
            return
        height = self.workspace_paned.winfo_height()
        width = self.main_paned.winfo_width()
        if height < 612 or width < 700:
            return
        if not getattr(self, "_workspace_positioned", False):
            self.main_paned.sashpos(0, 390 if width >= 1500 else 350)
            self.episode_paned.sashpos(0, int(width * .37))
            self._workspace_positioned = True
        top_ratio, bottom_ratio = getattr(self, "_workspace_ratios", (.44, .73))
        first = max(200, min(height - 412, int(height * top_ratio)))
        second = max(first + 226, min(height - 186, int(height * bottom_ratio)))
        ep_width = self.episode_paned.winfo_width()
        for pane, index, value in (
            (self.workspace_paned, 0, first), (self.workspace_paned, 1, second),
            (self.main_paned, 0, max(300, min(width - 520, self.main_paned.sashpos(0)))),
            (self.episode_paned, 0, max(400, min(ep_width - 650, self.episode_paned.sashpos(0)))),
        ):
            if pane.sashpos(index) != value:
                pane.sashpos(index, value)

    def _initialize_workspace(self):
        # Attend la première mise en page des barres repliables et du gestionnaire de fenêtres.
        self._workspace_ready = True
        self._fit_workspace()

    def _sync_episode_selector_from_tree(self, _event=None):
        """Garde les deux sélecteurs du même épisode cohérents dans la nouvelle disposition."""
        selected = self.visual_episode_tree.selection()
        if selected and hasattr(self, "track_episode_select_var"):
            index = int(selected[0]) - 1
            if 0 <= index < len(self.visual_episodes):
                episode = self.visual_episodes[index]
                self.track_episode_select_var.set(
                    f"E{episode['index']} — {episode.get('name', 'Épisode ' + str(episode['index']))}"
                )

    def _build_ui(self):
        self._scroll_panels = []
        self.bind_all("<MouseWheel>", self._scroll_settings, add="+")
        self.bind_all("<Button-4>", self._scroll_settings, add="+")
        self.bind_all("<Button-5>", self._scroll_settings, add="+")
        self.bind_all("<FocusIn>", self._reveal_setting, add="+")
        # Menu principal
        menubar = tk.Menu(self, tearoff=False, bg=self.COL_PANEL2, fg=self.COL_TEXT,
                          activebackground="#22303a", activeforeground="#ffffff")
        mode_menu = tk.Menu(menubar, tearoff=False, bg=self.COL_PANEL2, fg=self.COL_TEXT,
                            activebackground="#22303a", activeforeground="#ffffff")

        mode_menu.add_radiobutton(
            label="CockHero Funscript Generator",
            variable=self.interface_mode,
            value="cockhero_visual",
            command=lambda: self.set_interface_mode("cockhero_visual")
        )
        mode_menu.add_separator()
        mode_menu.add_radiobutton(
            label="Optimiser un funscript existant",
            variable=self.interface_mode,
            value="optimisation",
            command=lambda: self.set_interface_mode("optimisation")
        )
        menubar.add_cascade(label="Mode", menu=mode_menu)

        file_menu = tk.Menu(menubar, tearoff=False, bg=self.COL_PANEL2, fg=self.COL_TEXT,
                            activebackground="#22303a", activeforeground="#ffffff")
        file_menu.add_command(label="Ouvrir un funscript", command=self.open_file)
        file_menu.add_command(label="Ouvrir une vidéo", command=self.choose_video)
        file_menu.add_separator()
        file_menu.add_command(label="Quitter", command=self._on_close)
        menubar.add_cascade(label="Fichier", menu=file_menu)


        self.config(menu=menubar)

        top = ttk.Frame(self, style="Toolbar.TFrame", padding=(6, 3))
        top.pack(fill="x")

        ttk.Label(top, text="COCKHERO FUNSCRIPT GENERATOR", style="Toolbar.TLabel",
                  font=("Sans", 10, "bold")).pack(side="left")
        ttk.Label(top, text=APP_VERSION, style="Toolbar.TLabel").pack(side="left", padx=(6, 10))
        self.action_toolbar = ttk.Frame(top, style="Toolbar.TFrame")
        self.action_toolbar.pack(side="left", fill="x", expand=True)
        top = self.action_toolbar

        self.btn_open_funscript = ttk.Button(top, text="Ouvrir funscript", style="Accent.TButton", command=self.open_file)
        self.btn_open_funscript.pack(side="left")

        self.btn_open_folder = ttk.Button(top, text="Ouvrir dossier", command=self.choose_folder)
        self.btn_open_folder.pack(side="left", padx=(6, 0))

        self.btn_create_video = ttk.Button(top, text="Créer depuis vidéo", command=self.create_from_video_dialog)
        self.btn_create_video.pack(side="left", padx=(6, 0))

        self.btn_preview = ttk.Button(top, text="Aperçu", command=self.generate_preview)
        self.btn_preview.pack(side="left", padx=(18, 0))

        self.btn_apply = ttk.Button(top, text="Appliquer", command=self.apply_to_file)
        self.btn_apply.pack(side="left", padx=(6, 0))

        self.btn_process_folder = ttk.Button(top, text="Traiter dossier", command=self.process_folder_gui)
        self.btn_process_folder.pack(side="left", padx=(6, 0))

        self.btn_cockhero_generate = ttk.Button(
            top,
            text="Générer CockHero",
            style="Accent.TButton",
            command=self.generate_cockhero_visual
        )
        # visible uniquement en mode CockHero

        self.btn_cockhero_open = ttk.Button(
            top,
            text="Ouvrir .funscript…",
            command=self.choose_funscript_for_editing
        )

        self.btn_cockhero_save = ttk.Button(
            top,
            text="Enregistrer .funscript…",
            command=self.save_generated_funscript
        )

        self.btn_cockhero_batch = ttk.Button(
            top,
            text="Lot de vidéos…",
            command=self.start_visual_batch
        )

        self.chk_batch_overwrite = ttk.Checkbutton(
            top,
            text="Écraser .funscript existants",
            variable=self.batch_overwrite_existing_var,
            command=self._save_app_config
        )
        # visible à côté de Générer dans le mode CockHero Visuel

        top_names = ttk.Frame(self, style="Toolbar.TFrame", padding=(6, 0))
        top_names.pack(fill="x")

        ttk.Label(
            top_names,
            textvariable=self.current_video_name_var,
            style="Toolbar.TLabel",
            anchor="w", width=1
        ).pack(side="left", fill="x", expand=True)

        ttk.Label(
            top_names,
            textvariable=self.current_funscript_name_var,
            style="Toolbar.TLabel",
            anchor="w", width=1
        ).pack(side="left", fill="x", expand=True)

        # Trois zones redimensionnables : vidéo/réglages, trame, épisodes/options.
        self.main_outer = ttk.Frame(self)
        self.main_outer.pack(fill="both", expand=True, padx=4, pady=3)
        self.workspace_paned = ttk.Panedwindow(self.main_outer, orient="vertical")
        self.workspace_paned.pack(fill="both", expand=True)

        self.main_paned = ttk.Panedwindow(self.workspace_paned, orient="horizontal", height=400)
        self.workspace_paned.add(self.main_paned, weight=4)
        self.timeline_panel = ttk.Frame(self.workspace_paned, style="Panel.TFrame", height=250)
        self.workspace_paned.add(self.timeline_panel, weight=3)
        self.episode_paned = ttk.Panedwindow(self.workspace_paned, orient="horizontal", height=210)
        self.workspace_paned.add(self.episode_paned, weight=2)
        main = self.main_paned

        # LEFT: notebook settings
        left = ttk.Frame(main, style="Panel.TFrame")
        main.add(left, weight=0)

        self.left_container = left
        self.notebook = ttk.Notebook(left)
        self.notebook.pack(fill="both", expand=True, padx=2, pady=2)
        notebook = self.notebook

        params_panel, params_tab = self._scrollable_panel(notebook)
        patterns_panel, patterns_tab = self._scrollable_panel(notebook)
        start_panel, start_tab = self._scrollable_panel(notebook)
        create_panel, create_tab = self._scrollable_panel(notebook)
        end_panel, end_tab = self._scrollable_panel(notebook)
        notebook.add(params_panel, text="Réglages")
        notebook.add(patterns_panel, text="Patterns")
        notebook.add(start_panel, text="Début")
        notebook.add(end_panel, text="Fin")
        notebook.add(create_panel, text="Créer")

        # Le défilement appartient à l'onglet entier, y compris ses boutons.
        self.params_frame = params_tab

        categories = {}
        for cat, name, label, typ, default, help_text in PARAMETERS:
            # start-specific controls go to dedicated start tab instead
            if cat == "Début":
                continue
            if cat not in categories:
                box = ttk.LabelFrame(self.params_frame, text=cat, padding=4)
                box.pack(fill="x", padx=2, pady=2)
                categories[cat] = box

            row = ttk.Frame(categories[cat], style="Panel.TFrame")
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=label, style="Panel.TLabel").pack(side="left", fill="x", expand=True)
            value = globals().get(name, default)
            var = tk.StringVar(value=str(value))
            self.param_vars[name] = (var, typ, default)
            ent = ttk.Entry(row, textvariable=var, width=10)
            ent.pack(side="right")
            ent.bind("<Return>", lambda e: self._auto_refresh())
            ent.bind("<FocusOut>", lambda e: self._auto_refresh())
            self._tooltip(ent, help_text)

        for cat, name, label in TOGGLES:
            if cat == "Début":
                continue
            if cat not in categories:
                box = ttk.LabelFrame(self.params_frame, text=cat, padding=4)
                box.pack(fill="x", padx=2, pady=2)
                categories[cat] = box
            var = tk.BooleanVar(value=bool(globals().get(name, False)))
            self.toggle_vars[name] = var
            ttk.Checkbutton(categories[cat], text=label, variable=var, command=self._auto_refresh).pack(anchor="w", pady=3)

        # Patterns tab
        pattern_top = ttk.LabelFrame(patterns_tab, text="Remplissage personnalisé des temps morts", padding=4)
        pattern_top.pack(fill="x", padx=3, pady=3)

        self.use_patterns_var.set(bool(self.saved_config.get("use_patterns", False)))
        self.pattern_random_var.set(bool(self.saved_config.get("pattern_random", True)))
        self.pattern_step_var.set(str(self.saved_config.get("pattern_step", "180")))
        self.pattern_min_var.set(str(self.saved_config.get("pattern_min", "10")))
        self.pattern_max_var.set(str(self.saved_config.get("pattern_max", "90")))

        ttk.Checkbutton(pattern_top, text="Utiliser les patterns sélectionnés dans les trous",
                        variable=self.use_patterns_var, command=self._auto_refresh).pack(anchor="w")
        ttk.Checkbutton(pattern_top, text="Choisir aléatoirement parmi les patterns sélectionnés",
                        variable=self.pattern_random_var, command=self._auto_refresh).pack(anchor="w", pady=(1,0))

        row = ttk.Frame(pattern_top, style="Panel.TFrame")
        row.pack(fill="x", pady=(3,2))
        ttk.Label(row, text="Pas (ms)", style="Panel.TLabel").pack(side="left")
        ttk.Entry(row, textvariable=self.pattern_step_var, width=7).pack(side="left", padx=(4,12))
        ttk.Label(row, text="Min", style="Panel.TLabel").pack(side="left")
        ttk.Entry(row, textvariable=self.pattern_min_var, width=6).pack(side="left", padx=(4,12))
        ttk.Label(row, text="Max", style="Panel.TLabel").pack(side="left")
        ttk.Entry(row, textvariable=self.pattern_max_var, width=6).pack(side="left", padx=(4,0))

        list_frame = ttk.Frame(patterns_tab, style="Panel.TFrame")
        list_frame.pack(fill="both", expand=True, padx=3, pady=(0,3))
        self.pattern_list = tk.Listbox(
            list_frame, selectmode="extended",
            bg="#0d1317", fg=self.COL_TEXT,
            selectbackground="#1c6f4a",
            selectforeground="#ffffff",
            highlightthickness=1,
            highlightbackground=self.COL_BORDER,
            activestyle="none",
            exportselection=False,
        )
        pscroll2 = ttk.Scrollbar(list_frame, orient="vertical", command=self.pattern_list.yview)
        self.pattern_list.configure(yscrollcommand=pscroll2.set)
        self.pattern_list.pack(side="left", fill="both", expand=True)
        pscroll2.pack(side="right", fill="y")

        for name in PATTERN_NAMES:
            self.pattern_list.insert("end", name)

        saved_sel = self.saved_config.get("selected_patterns", [0])
        for i in saved_sel:
            if isinstance(i, int) and 0 <= i < 50:
                self.pattern_list.selection_set(i)
        if not self.pattern_list.curselection():
            self.pattern_list.selection_set(0)
        self.pattern_list.bind("<<ListboxSelect>>", lambda e: self._auto_refresh())

        # Patterns de fin indépendants
        end_pattern_box = ttk.LabelFrame(patterns_tab, text="Patterns de fin de vidéo", padding=4)
        end_pattern_box.pack(fill="both", expand=True, padx=3, pady=(0,3))

        self.use_end_patterns_var.set(bool(self.saved_config.get("use_end_patterns", False)))
        self.end_pattern_random_var.set(bool(self.saved_config.get("end_pattern_random", True)))
        self.end_pattern_step_var.set(str(self.saved_config.get("end_pattern_step", "220")))
        self.end_pattern_min_var.set(str(self.saved_config.get("end_pattern_min", "10")))
        self.end_pattern_max_var.set(str(self.saved_config.get("end_pattern_max", "90")))

        ttk.Checkbutton(
            end_pattern_box,
            text="Utiliser des patterns personnalisés jusqu'à la fin de la vidéo",
            variable=self.use_end_patterns_var,
            command=self._auto_refresh
        ).pack(anchor="w")

        ttk.Checkbutton(
            end_pattern_box,
            text="Choisir aléatoirement parmi les patterns de fin sélectionnés",
            variable=self.end_pattern_random_var,
            command=self._auto_refresh
        ).pack(anchor="w", pady=(1,0))

        end_row = ttk.Frame(end_pattern_box, style="Panel.TFrame")
        end_row.pack(fill="x", pady=(3,2))
        ttk.Label(end_row, text="Pas (ms)", style="Panel.TLabel").pack(side="left")
        ttk.Entry(end_row, textvariable=self.end_pattern_step_var, width=7).pack(side="left", padx=(4,12))
        ttk.Label(end_row, text="Min", style="Panel.TLabel").pack(side="left")
        ttk.Entry(end_row, textvariable=self.end_pattern_min_var, width=6).pack(side="left", padx=(4,12))
        ttk.Label(end_row, text="Max", style="Panel.TLabel").pack(side="left")
        ttk.Entry(end_row, textvariable=self.end_pattern_max_var, width=6).pack(side="left", padx=(4,0))

        end_list_frame = ttk.Frame(end_pattern_box, style="Panel.TFrame")
        end_list_frame.pack(fill="both", expand=True, pady=(1,0))

        self.end_pattern_list = tk.Listbox(
            end_list_frame,
            height=9,
            selectmode="extended",
            bg="#0d1317",
            fg=self.COL_TEXT,
            selectbackground="#1c6f4a",
            selectforeground="#ffffff",
            highlightthickness=1,
            highlightbackground=self.COL_BORDER,
            activestyle="none",
            exportselection=False,
        )
        end_scroll = ttk.Scrollbar(
            end_list_frame, orient="vertical",
            command=self.end_pattern_list.yview
        )
        self.end_pattern_list.configure(yscrollcommand=end_scroll.set)
        self.end_pattern_list.pack(side="left", fill="both", expand=True)
        end_scroll.pack(side="right", fill="y")

        for name in PATTERN_NAMES:
            self.end_pattern_list.insert("end", name)

        saved_end_sel = self.saved_config.get("selected_end_patterns", [8])
        for i in saved_end_sel:
            if isinstance(i, int) and 0 <= i < 50:
                self.end_pattern_list.selection_set(i)
        if not self.end_pattern_list.curselection():
            self.end_pattern_list.selection_set(8)

        self.end_pattern_list.bind("<<ListboxSelect>>", lambda e: self._auto_refresh())

        # Start tab - explicit ramp control
        start_box = ttk.LabelFrame(start_tab, text="Montée progressive au début", padding=4)
        start_box.pack(fill="x", padx=3, pady=3)

        start_specs = [
            ("INITIAL_AMPLITUDE_SCALE", "Amplitude atteinte au premier mouvement", float, 0.25,
             "0.25 = 25 %. Tu peux augmenter ou diminuer la remontée."),
            ("INITIAL_SOURCE_WINDOW_MS", "Fenêtre source copiée (ms)", int, 20000,
             "Durée de mouvement servant de modèle pour remplir le début."),
            ("INITIAL_SPEED_RAMP_MS", "Durée de remontée vitesse (ms)", int, 10000,
             "Durée avant d'atteindre la vitesse normale."),
            ("INITIAL_STEP_MS_START", "Pas de départ très lent (ms)", int, 700,
             "Plus grand = départ plus lent."),
            ("INITIAL_STEP_MS_END", "Pas final (ms)", int, 120,
             "Vitesse finale du mouvement initial."),
            ("FIRST_ORIGINAL_RAMP_MS", "Retour à 100 % (ms)", int, 10000,
             "Durée de remontée du premier bloc original vers 100 %."),
        ]

        for name, label, typ, default, help_text in start_specs:
            row = ttk.Frame(start_box, style="Panel.TFrame")
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label, style="Panel.TLabel").pack(side="left", fill="x", expand=True)
            value = globals().get(name, default)
            var = tk.StringVar(value=str(value))
            self.param_vars[name] = (var, typ, default)
            ent = ttk.Entry(row, textvariable=var, width=10)
            ent.pack(side="right")
            ent.bind("<Return>", lambda e: self._auto_refresh())
            ent.bind("<FocusOut>", lambda e: self._auto_refresh())
            self._tooltip(ent, help_text)

        start_toggles = [
            ("PROGRESSIVE_INITIAL_GAP", "Remplir le vide initial progressivement"),
            ("SOFTEN_FIRST_ORIGINAL_BLOCK", "Adoucir le premier bloc original"),
        ]
        for name, label in start_toggles:
            var = tk.BooleanVar(value=bool(globals().get(name, True)))
            self.toggle_vars[name] = var
            ttk.Checkbutton(start_box, text=label, variable=var, command=self._auto_refresh).pack(anchor="w", pady=3)


        # End tab - contrôle détaillé de la fin
        end_control_box = ttk.LabelFrame(
            end_tab,
            text="Descente progressive à la fin",
            padding=4
        )
        end_control_box.pack(fill="x", padx=3, pady=3)

        self.end_fade_enabled_var.set(bool(self.saved_config.get("end_fade_enabled", True)))
        self.end_fade_duration_var.set(str(self.saved_config.get("end_fade_duration", "10000")))
        self.end_final_strength_var.set(str(self.saved_config.get("end_final_strength", "0.08")))
        self.end_target_pos_var.set(str(self.saved_config.get("end_target_pos", "50")))
        self.end_hold_ms_var.set(str(self.saved_config.get("end_hold_ms", "0")))

        ttk.Checkbutton(
            end_control_box,
            text="Activer la descente progressive de fin",
            variable=self.end_fade_enabled_var,
            command=self._auto_refresh
        ).pack(anchor="w", pady=(0,3))

        end_specs = [
            ("Durée de la descente (ms)", self.end_fade_duration_var,
             "Durée sur laquelle l'amplitude diminue progressivement avant la fin."),
            ("Force finale 0..1", self.end_final_strength_var,
             "0.08 = 8 % de l'amplitude à la toute fin."),
            ("Position cible finale 0..100", self.end_target_pos_var,
             "Position vers laquelle la trame se rapproche à la fin."),
            ("Maintien final (ms)", self.end_hold_ms_var,
             "Durée pendant laquelle la position cible reste fixe juste avant la fin."),
        ]

        for label, var, help_text in end_specs:
            row = ttk.Frame(end_control_box, style="Panel.TFrame")
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label, style="Panel.TLabel").pack(side="left", fill="x", expand=True)
            ent = ttk.Entry(row, textvariable=var, width=10)
            ent.pack(side="right")
            ent.bind("<Return>", lambda e: self._auto_refresh())
            ent.bind("<FocusOut>", lambda e: self._auto_refresh())
            self._tooltip(ent, help_text)

        ttk.Label(
            end_control_box,
            text="Ces réglages contrôlent la sortie finale indépendamment des patterns de fin.",
            style="Panel.TLabel",
            wraplength=320,
            justify="left"
        ).pack(anchor="w", pady=(8,0))

        # Create tab - generate a whole funscript from a video only
        create_box = ttk.LabelFrame(
            create_tab,
            text="Créer un funscript depuis une vidéo seule",
            padding=4
        )
        create_box.pack(fill="both", expand=True, padx=3, pady=3)

        ttk.Label(
            create_box,
            text="Le générateur utilise la durée de la vidéo et les patterns sélectionnés.",
            style="Panel.TLabel",
            wraplength=320,
            justify="left"
        ).pack(anchor="w", pady=(0,3))

        self.video_gen_random_var.set(bool(self.saved_config.get("video_gen_random", True)))
        self.video_gen_step_var.set(str(self.saved_config.get("video_gen_step", "180")))
        self.video_gen_min_var.set(str(self.saved_config.get("video_gen_min", "10")))
        self.video_gen_max_var.set(str(self.saved_config.get("video_gen_max", "90")))
        self.video_gen_block_var.set(str(self.saved_config.get("video_gen_block", "4000")))
        self.video_gen_start_fade_var.set(str(self.saved_config.get("video_gen_start_fade", "10000")))
        self.video_gen_end_fade_var.set(str(self.saved_config.get("video_gen_end_fade", "10000")))
        self.video_gen_start_strength_var.set(str(self.saved_config.get("video_gen_start_strength", "0.05")))
        self.video_gen_end_strength_var.set(str(self.saved_config.get("video_gen_end_strength", "0.08")))

        ttk.Checkbutton(
            create_box,
            text="Choisir aléatoirement parmi les patterns sélectionnés",
            variable=self.video_gen_random_var
        ).pack(anchor="w", pady=(0,6))

        gen_fields = [
            ("Pas entre points (ms)", self.video_gen_step_var),
            ("Position minimum", self.video_gen_min_var),
            ("Position maximum", self.video_gen_max_var),
            ("Durée d'un pattern (ms)", self.video_gen_block_var),
            ("Montée progressive début (ms)", self.video_gen_start_fade_var),
            ("Descente progressive fin (ms)", self.video_gen_end_fade_var),
            ("Force initiale 0..1", self.video_gen_start_strength_var),
            ("Force finale 0..1", self.video_gen_end_strength_var),
        ]

        for label, var in gen_fields:
            row = ttk.Frame(create_box, style="Panel.TFrame")
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label, style="Panel.TLabel").pack(side="left", fill="x", expand=True)
            ttk.Entry(row, textvariable=var, width=10).pack(side="right")

        ttk.Label(
            create_box,
            text="Patterns utilisés pour toute la vidéo",
            style="Panel.TLabel"
        ).pack(anchor="w", pady=(4,2))

        gen_list_frame = ttk.Frame(create_box, style="Panel.TFrame")
        gen_list_frame.pack(fill="both", expand=True)

        self.video_gen_pattern_list = tk.Listbox(
            gen_list_frame,
            selectmode="extended",
            bg="#0d1317",
            fg=self.COL_TEXT,
            selectbackground="#1c6f4a",
            selectforeground="#ffffff",
            highlightthickness=1,
            highlightbackground=self.COL_BORDER,
            activestyle="none",
            exportselection=False,
            height=12,
        )
        gen_scroll = ttk.Scrollbar(
            gen_list_frame,
            orient="vertical",
            command=self.video_gen_pattern_list.yview
        )
        self.video_gen_pattern_list.configure(yscrollcommand=gen_scroll.set)
        self.video_gen_pattern_list.pack(side="left", fill="both", expand=True)
        gen_scroll.pack(side="right", fill="y")

        for name in PATTERN_NAMES:
            self.video_gen_pattern_list.insert("end", name)

        saved_gen_sel = self.saved_config.get("selected_video_gen_patterns", [8])
        for i in saved_gen_sel:
            if isinstance(i, int) and 0 <= i < 50:
                self.video_gen_pattern_list.selection_set(i)

        if not self.video_gen_pattern_list.curselection():
            self.video_gen_pattern_list.selection_set(8)

        ttk.Button(
            create_box,
            text="Créer / prévisualiser depuis la vidéo",
            style="Accent.TButton",
            command=self.generate_from_current_video
        ).pack(fill="x", pady=(4,2))

        ttk.Button(
            create_box,
            text="Enregistrer le nouveau .funscript",
            command=self.save_generated_funscript
        ).pack(fill="x")


        # Mode CockHero Visuel dédié : toute forme qui arrive sur une ligne.
        self.visual_settings_panel, self.cockhero_visual_mode_frame = self._scrollable_panel(left)
        self.visual_left_section_var = tk.StringVar(value="Détection")
        self.visual_left_sections = {}

        vh = ttk.Frame(self.cockhero_visual_mode_frame, style="Panel.TFrame", padding=(3,0))
        vh.pack(fill="x")
        ttk.Label(
            vh,
            text="Réglages",
            style="Panel.TLabel",
            font=("Sans", 11, "bold")
        ).pack(side="left")

        section_combo = ttk.Combobox(
            vh,
            textvariable=self.visual_left_section_var,
            values=("Détection", "Génération / épisodes"),
            state="readonly",
            width=22
        )
        section_combo.pack(side="right")
        section_combo.bind("<<ComboboxSelected>>", lambda e: self._show_visual_left_section())

        line_box = ttk.LabelFrame(
            self.cockhero_visual_mode_frame,
            text="Détection",
            padding=5
        )
        line_box.pack(fill="x", padx=3, pady=(0,1))
        self.visual_left_sections["Détection"] = [line_box]

        detection_fields = ttk.Frame(line_box, style="Panel.TFrame")
        detection_fields.pack(fill="x")
        for column, (label, var) in enumerate([
            ("Centre piste %", self.visual_line_y_var),
            ("Hauteur %", self.visual_line_band_var),
        ]):
            ttk.Label(detection_fields, text=label, style="Panel.TLabel").grid(
                row=0, column=column * 2, sticky="w", padx=(0, 3))
            ttk.Entry(detection_fields, textvariable=var, width=5).grid(
                row=0, column=column * 2 + 1, sticky="ew", padx=(0, 6))
            detection_fields.columnconfigure(column * 2 + 1, weight=1)
        row = ttk.Frame(line_box, style="Panel.TFrame")
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Décalage synchro (ms)", style="Panel.TLabel").pack(side="left")
        ttk.Entry(row, textvariable=self.visual_sync_offset_var, width=9).pack(side="right")

        ttk.Checkbutton(
            line_box,
            text="Détection automatique de la cible",
            variable=self.visual_auto_target_var
        ).pack(anchor="w", pady=(2,1))

        detection_buttons = ttk.Frame(line_box, style="Panel.TFrame")
        detection_buttons.pack(fill="x", pady=2)
        ttk.Button(
            detection_buttons, text="Options avancées…",
            command=self._open_visual_advanced_options
        ).pack(side="left", fill="x", expand=True, padx=(0, 3))
        self.btn_detect_visual_line = ttk.Button(
            detection_buttons, text="Réanalyser (manuel)", style="Accent.TButton",
            command=self.detect_visual_line_events
        )
        self.btn_detect_visual_line.pack(side="left", fill="x", expand=True)

        ttk.Checkbutton(
            line_box,
            text="Inverser la phase depuis la fin (test seulement)",
            variable=self.visual_reverse_analysis_var
        ).pack(anchor="w", pady=(3,1))

        # Variables conservées pour le moteur, mais le panneau gauche
        # s'arrête volontairement après le bouton Analyser.
        self.visual_analysis_info_var = tk.StringVar(
            value="Analyse : 0.0 % — Prêt"
        )
        self.visual_analysis_percent_var = tk.StringVar(value="0.0 %")

        progress_box = ttk.LabelFrame(
            self.cockhero_visual_mode_frame,
            text="Progression de l'analyse",
            padding=4
        )
        self.visual_progress_box = progress_box
        # volontairement non ajouté à la section visible

        self.visual_analysis_progress = ttk.Progressbar(
            progress_box,
            variable=self.analysis_progress_var,
            maximum=100.0,
            mode="determinate"
        )
        self.visual_analysis_progress.pack(fill="x", pady=(0,6))

        ttk.Label(
            progress_box,
            textvariable=self.analysis_stage_var,
            style="Panel.TLabel",
            wraplength=285,
            justify="left"
        ).pack(anchor="w")

        self.btn_cancel_visual_analysis = ttk.Button(
            progress_box,
            text="Annuler l'analyse",
            command=self.cancel_visual_analysis,
            state="disabled"
        )
        self.btn_cancel_visual_analysis.pack(fill="x", pady=(7,0))

        gen_box = ttk.LabelFrame(
            self.cockhero_visual_mode_frame,
            text="Génération",
            padding=4
        )
        gen_box.pack(fill="x", padx=3, pady=(0,3))
        self.visual_left_sections["Génération / épisodes"] = [gen_box]

        ttk.Label(
            gen_box,
            text="Logique de génération des marqueurs :",
            style="Panel.TLabel"
        ).pack(anchor="w", pady=(0,4))

        ttk.Radiobutton(
            gen_box,
            text="Marqueur = sommet + creux au milieu (v4.6.2)",
            variable=self.visual_generation_logic_var,
            value="marker_peaks"
        ).pack(anchor="w")

        ttk.Radiobutton(
            gen_box,
            text="Marqueur = extrémité directe haut / bas",
            variable=self.visual_generation_logic_var,
            value="marker_alternating"
        ).pack(anchor="w", pady=(0,6))

        ttk.Label(
            gen_box,
            text=(
                "Tu peux changer de logique sans refaire l'analyse. "
                "Tu peux aussi ouvrir un .funscript existant et convertir sa logique."
            ),
            style="Panel.TLabel",
            wraplength=285,
            justify="left"
        ).pack(fill="x", pady=(0,6))

        convert_row = ttk.Frame(gen_box, style="Panel.TFrame")
        convert_row.pack(fill="x", pady=(2,6))

        ttk.Button(
            convert_row,
            text="Ouvrir un funscript à convertir",
            command=self.open_funscript_for_logic_conversion
        ).pack(side="left", fill="x", expand=True, padx=(0,2))

        ttk.Button(
            convert_row,
            text="Appliquer la logique",
            style="Accent.TButton",
            command=self.convert_loaded_funscript_logic
        ).pack(side="left", fill="x", expand=True, padx=(2,0))

        for label, var in [
            ("Position minimum", self.cockhero_min_var),
            ("Position maximum", self.cockhero_max_var),
            ("Accent tous les N passages", self.cockhero_accent_every_var),
            ("Force normale", self.cockhero_normal_strength_var),
            ("Force accent", self.cockhero_accent_strength_var),
            ("Montée début (ms)", self.cockhero_start_fade_var),
            ("Descente fin (ms)", self.cockhero_end_fade_var),
        ]:
            row = ttk.Frame(gen_box, style="Panel.TFrame")
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=label, style="Panel.TLabel").pack(side="left", fill="x", expand=True)
            ttk.Entry(row, textvariable=var, width=9).pack(side="right")

        episode_box = ttk.LabelFrame(
            self.episode_paned, text="Épisodes", padding=3
        )
        self.episode_table_box = episode_box
        self.episode_paned.add(episode_box, weight=2)
        self.track_tabs = ttk.Notebook(self.episode_paned)
        self.episode_paned.add(self.track_tabs, weight=3)

        gap_row = ttk.Frame(episode_box, style="Panel.TFrame")
        gap_row.pack(fill="x", pady=(0,4))
        ttk.Label(
            gap_row,
            text="Pause > (ms)",
            style="Panel.TLabel"
        ).pack(side="left", fill="x", expand=True)
        ttk.Entry(
            gap_row,
            textvariable=self.visual_episode_gap_var,
            width=8
        ).pack(side="right")

        ttk.Button(
            gap_row,
            text="Détecter / actualiser",
            command=self.rebuild_visual_episodes
        ).pack(side="right", padx=(6, 0))

        columns = (
            "num", "name", "start", "end", "count",
            "high", "low", "highdeg", "lowdeg", "offset", "mode"
        )
        tree_frame = ttk.Frame(episode_box, style="Panel.TFrame")
        tree_frame.pack(fill="both", expand=True)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.visual_episode_tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="headings",
            height=6,
            selectmode="browse"
        )

        headings = {
            "num": "#",
            "name": "Nom",
            "start": "Début",
            "end": "Fin",
            "count": "Pts",
            "high": "Haut",
            "low": "Bas",
            "highdeg": "Haut°",
            "lowdeg": "Bas°",
            "offset": "Offset",
            "mode": "Mode",
        }
        widths = {
            "num": 28,
            "name": 90,
            "start": 62,
            "end": 62,
            "count": 40,
            "high": 42,
            "low": 42,
            "highdeg": 48,
            "lowdeg": 48,
            "offset": 55,
            "mode": 72,
        }

        for col in columns:
            self.visual_episode_tree.heading(col, text=headings[col])
            self.visual_episode_tree.column(
                col,
                width=widths[col],
                minwidth=widths[col],
                anchor="center"
            )

        self.visual_episode_tree.grid(row=0, column=0, sticky="nsew")
        tree_y = ttk.Scrollbar(tree_frame, orient="vertical", command=self.visual_episode_tree.yview)
        tree_x = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.visual_episode_tree.xview)
        tree_y.grid(row=0, column=1, sticky="ns")
        tree_x.grid(row=1, column=0, sticky="ew")
        self.visual_episode_tree.configure(yscrollcommand=tree_y.set, xscrollcommand=tree_x.set)
        self.visual_episode_tree.bind(
            "<<TreeviewSelect>>",
            self._on_visual_episode_selected
        )
        self.visual_episode_tree.bind(
            "<<TreeviewSelect>>", self._sync_episode_selector_from_tree, add="+"
        )

        details_panel, details_body = self._scrollable_panel(self.track_tabs)
        self.track_tabs.add(details_panel, text="Réglages détaillés")
        editor = ttk.LabelFrame(
            details_body,
            text="Réglages de l'épisode sélectionné",
            padding=3
        )
        editor.pack(fill="x", pady=(4,2))

        name_row = ttk.Frame(editor, style="Panel.TFrame")
        name_row.pack(fill="x", pady=2)
        ttk.Label(name_row, text="Nom", style="Panel.TLabel").pack(side="left")
        ttk.Entry(
            name_row,
            textvariable=self.visual_episode_name_var
        ).pack(side="left", fill="x", expand=True, padx=(6,0))

        p_row = ttk.Frame(editor, style="Panel.TFrame")
        p_row.pack(fill="x", pady=2)

        ttk.Label(p_row, text="Haut %", style="Panel.TLabel").pack(side="left")
        high_entry = ttk.Entry(
            p_row,
            textvariable=self.visual_episode_high_var,
            width=6
        )
        high_entry.pack(side="left", padx=(3,8))

        ttk.Label(p_row, text="Bas %", style="Panel.TLabel").pack(side="left")
        low_entry = ttk.Entry(
            p_row,
            textvariable=self.visual_episode_low_var,
            width=6
        )
        low_entry.pack(side="left", padx=(3,8))

        ttk.Button(
            p_row,
            text="% → degrés",
            command=self._sync_episode_degrees_from_percent
        ).pack(side="left", padx=(2,0))

        d_row = ttk.Frame(editor, style="Panel.TFrame")
        d_row.pack(fill="x", pady=2)

        ttk.Label(d_row, text="Haut °", style="Panel.TLabel").pack(side="left")
        ttk.Entry(
            d_row,
            textvariable=self.visual_episode_high_deg_var,
            width=6
        ).pack(side="left", padx=(3,8))

        ttk.Label(d_row, text="Bas °", style="Panel.TLabel").pack(side="left")
        ttk.Entry(
            d_row,
            textvariable=self.visual_episode_low_deg_var,
            width=6
        ).pack(side="left", padx=(3,8))

        ttk.Button(
            d_row,
            text="degrés → %",
            command=self._sync_episode_percent_from_degrees
        ).pack(side="left", padx=(2,0))

        flags_row = ttk.Frame(editor, style="Panel.TFrame")
        flags_row.pack(fill="x", pady=2)

        ttk.Checkbutton(
            flags_row,
            text="Actif",
            variable=self.visual_episode_enabled_var
        ).pack(side="left")

        ttk.Checkbutton(
            flags_row,
            text="Inverser haut/bas",
            variable=self.visual_episode_invert_var
        ).pack(side="left", padx=(10,0))

        start_row = ttk.Frame(editor, style="Panel.TFrame")
        start_row.pack(fill="x", pady=2)

        ttk.Label(start_row, text="Départ", style="Panel.TLabel").pack(side="left")
        ttk.Radiobutton(
            start_row,
            text="Haut",
            value="high",
            variable=self.visual_episode_start_side_var
        ).pack(side="left", padx=(6,0))
        ttk.Radiobutton(
            start_row,
            text="Bas",
            value="low",
            variable=self.visual_episode_start_side_var
        ).pack(side="left", padx=(6,0))

        off_row = ttk.Frame(editor, style="Panel.TFrame")
        off_row.pack(fill="x", pady=2)

        ttk.Label(off_row, text="Offset local (ms)", style="Panel.TLabel").pack(side="left")
        ttk.Entry(
            off_row,
            textvariable=self.visual_episode_offset_var,
            width=8
        ).pack(side="left", padx=(6,12))

        ttk.Label(off_row, text="Pause après", style="Panel.TLabel").pack(side="left")
        ttk.Combobox(
            off_row,
            textvariable=self.visual_episode_pause_mode_var,
            values=("hold", "center", "low", "high"),
            width=8,
            state="readonly"
        ).pack(side="left", padx=(6,0))

        color_row = ttk.Frame(editor, style="Panel.TFrame")
        color_row.pack(fill="x", pady=2)

        ttk.Label(color_row, text="Couleur", style="Panel.TLabel").pack(side="left")
        ttk.Entry(
            color_row,
            textvariable=self.visual_episode_color_var,
            width=12
        ).pack(side="left", padx=(6,8))

        ttk.Button(
            color_row,
            text="Appliquer tous les réglages",
            command=self.apply_visual_episode_range
        ).pack(side="left", fill="x", expand=True)

        ttk.Button(
            editor,
            text="Appliquer Haut/Bas à tous les épisodes",
            command=self.apply_visual_episode_range_to_all
        ).pack(fill="x", pady=(4,0))

        self.btn_generate_visual_panel = ttk.Button(
            gen_box,
            text="Générer depuis les franchissements",
            style="Accent.TButton",
            command=self.generate_cockhero_visual
        )
        self.btn_generate_visual_panel.pack(fill="x", pady=(3,2))

        self.generation_progress_var = tk.DoubleVar(value=0.0)
        self.generation_status_var = tk.StringVar(
            value="Génération : prête"
        )

        ttk.Label(
            gen_box,
            textvariable=self.generation_status_var,
            style="Panel.TLabel",
            anchor="w"
        ).pack(fill="x", pady=(3,2))

        self.generation_progress_bar = ttk.Progressbar(
            gen_box,
            variable=self.generation_progress_var,
            maximum=100.0,
            mode="determinate"
        )
        self.generation_progress_bar.pack(fill="x", pady=(0,6))

        ttk.Label(
            gen_box,
            text="Ajustement temporel des événements détectés :",
            style="Panel.TLabel"
        ).pack(anchor="w", pady=(3,2))

        sync_row = ttk.Frame(gen_box, style="Panel.TFrame")
        sync_row.pack(fill="x", pady=(2,2))
        ttk.Button(
            sync_row,
            text="-100 ms",
            command=lambda: self.adjust_visual_sync(-100)
        ).pack(side="left", fill="x", expand=True, padx=(0,2))
        ttk.Button(
            sync_row,
            text="+100 ms",
            command=lambda: self.adjust_visual_sync(100)
        ).pack(side="left", fill="x", expand=True, padx=(2,0))

        sync_row2 = sync_row
        ttk.Button(
            sync_row2,
            text="-1 s",
            command=lambda: self.adjust_visual_sync(-1000)
        ).pack(side="left", fill="x", expand=True, padx=(0,2))
        ttk.Button(
            sync_row2,
            text="+1 s",
            command=lambda: self.adjust_visual_sync(1000)
        ).pack(side="left", fill="x", expand=True, padx=(2,0))

        sync_row3 = sync_row
        ttk.Button(
            sync_row3,
            text="-5 s",
            command=lambda: self.adjust_visual_sync(-5000)
        ).pack(side="left", fill="x", expand=True, padx=(0,2))
        ttk.Button(
            sync_row3,
            text="+5 s",
            command=lambda: self.adjust_visual_sync(5000)
        ).pack(side="left", fill="x", expand=True, padx=(2,0))

        self._flow_controls(sync_row)

        self.visual_sync_live_var = tk.StringVar(
            value=f"Offset actuel : {self.visual_sync_offset_var.get()} ms"
        )
        ttk.Label(
            gen_box,
            textvariable=self.visual_sync_live_var,
            style="Panel.TLabel"
        ).pack(anchor="w", pady=(3,2))

        ttk.Button(
            gen_box,
            text="Réinitialiser synchro calibrée (-35 ms)",
            command=lambda: self._set_visual_sync_calibrated()
        ).pack(fill="x", pady=(4,2))

        self.btn_save_visual_panel = ttk.Button(
            gen_box,
            text="💾 Enregistrer le .funscript…",
            style="Accent.TButton",
            command=self.save_generated_funscript
        )
        self.btn_save_visual_panel.pack(fill="x", pady=(4,2))

        video_box = ttk.Frame(self.cockhero_visual_mode_frame, style="Panel.TFrame")
        video_box.pack(fill="x", padx=3, pady=(0,2))
        self.visual_video_box = video_box
        ttk.Button(
            video_box,
            text="Ouvrir une vidéo",
            command=self.choose_video
        ).pack(fill="x")

        # RIGHT
        self.right_panel = ttk.Frame(main)
        right = self.right_panel
        main.add(right, weight=1)

        # Vidéo en haut du panneau droit.
        # La TRAME COMPLÈTE est déplacée plus bas sur toute la largeur de l'écran.
        upper = ttk.Frame(right, style="Panel.TFrame")
        upper.pack(fill="both", expand=True)

        video_box = ttk.LabelFrame(upper, text="VIDÉO", padding=6)
        video_box.pack(fill="both", expand=True)

        video_display = ttk.Frame(video_box, style="Panel.TFrame")
        video_display.pack(fill="both", expand=True)

        self.video_canvas = tk.Canvas(
            video_display,
            height=1,
            bg="#000000",
            highlightthickness=0
        )
        self.video_canvas.pack(side="left", fill="both", expand=True)

        # L'indicateur suit la vidéo sans réserver une colonne sur toute la fenêtre.
        self.motion_rail = ttk.LabelFrame(video_display, text="Mvt", padding=2)
        self.motion_rail.pack(side="right", fill="y", padx=(3, 0))

        self.motion_canvas = tk.Canvas(
            self.motion_rail,
            width=68,
            bg="#090d10",
            highlightthickness=0
        )
        self.motion_canvas.pack(fill="both", expand=True)
        self.motion_canvas.bind(
            "<Configure>",
            lambda e: self._draw_motion_indicator()
        )

        # État d'analyse compact dans les réglages : aucune superposition vidéo.
        self.video_analysis_overlay = tk.Frame(
            self.cockhero_visual_mode_frame,
            bg="#0d1317",
            highlightbackground="#38d9a9",
            highlightthickness=1
        )
        self.video_analysis_overlay.pack(fill="x", padx=3, pady=3)

        overlay_top = tk.Frame(
            self.video_analysis_overlay,
            bg="#0d1317"
        )
        overlay_top.pack(fill="x", padx=7, pady=(4,1))

        tk.Label(
            overlay_top,
            text="ANALYSE",
            bg="#0d1317",
            fg="#38d9a9",
            font=("Sans", 9, "bold")
        ).pack(side="left")

        tk.Label(
            overlay_top,
            textvariable=self.visual_analysis_percent_var,
            bg="#0d1317",
            fg="#ffffff",
            font=("Sans", 8)
        ).pack(side="left", padx=(8,0))

        self.video_analysis_cancel_button = ttk.Button(
            overlay_top,
            text="Annuler",
            command=self.cancel_visual_analysis,
            width=8,
            state="disabled"
        )
        self.video_analysis_cancel_button.pack(side="right")

        self.video_analysis_progress = ttk.Progressbar(
            self.video_analysis_overlay,
            variable=self.analysis_progress_var,
            maximum=100.0,
            mode="determinate"
        )
        self.video_analysis_progress.pack(
            fill="x",
            padx=7,
            pady=(1,1)
        )

        tk.Label(
            self.video_analysis_overlay,
            textvariable=self.analysis_stage_var,
            wraplength=325, justify="left",
            bg="#0d1317",
            fg="#aab7c4",
            anchor="w",
            font=("Sans", 8)
        ).pack(fill="x", padx=7, pady=(0,3))

        tk.Label(
            self.video_analysis_overlay,
            textvariable=self.visual_audio_status_var,
            wraplength=325, justify="left",
            bg="#0d1317",
            fg="#38d9a9",
            anchor="w",
            font=("Sans", 8, "bold")
        ).pack(fill="x", padx=7, pady=(0,3))

        vcontrols = ttk.Frame(video_box, style="Panel.TFrame")
        vcontrols.pack(fill="x", pady=(2,0), before=video_display)

        ttk.Button(
            vcontrols,
            text="Vidéo…",
            command=self.choose_video
        ).pack(side="left")

        ttk.Button(
            vcontrols,
            text="▶ / ❚❚",
            command=self.toggle_video
        ).pack(side="left", padx=(5,0))

        # Navigation fine et grossière de la vidéo.
        ttk.Button(
            vcontrols,
            text="−100 ms",
            command=lambda: self.seek_relative(-0.1)
        ).pack(side="left", padx=(5,0))

        ttk.Button(
            vcontrols,
            text="+100 ms",
            command=lambda: self.seek_relative(0.1)
        ).pack(side="left", padx=(3,0))

        ttk.Button(
            vcontrols,
            text="−1 s",
            command=lambda: self.seek_relative(-1)
        ).pack(side="left", padx=(5,0))

        ttk.Button(
            vcontrols,
            text="+1 s",
            command=lambda: self.seek_relative(1)
        ).pack(side="left", padx=(3,0))

        ttk.Button(
            vcontrols,
            text="−5 s",
            command=lambda: self.seek_relative(-5)
        ).pack(side="left", padx=(5,0))

        ttk.Button(
            vcontrols,
            text="+5 s",
            command=lambda: self.seek_relative(5)
        ).pack(side="left", padx=(3,0))

        ttk.Label(
            vcontrols,
            textvariable=self.video_var,
            style="Panel.TLabel", width=18, anchor="e"
        ).pack(side="right")

        self.seek_scale = ttk.Scale(vcontrols, from_=0, to=1000, orient="horizontal", command=self._seek_scale_changed)
        self.seek_scale.pack(side="left", fill="x", expand=True, padx=6)

        # À partir d'ici, la trame sort du panneau droit :
        # elle utilise toute la largeur de la fenêtre, sous le panneau gauche + vidéo.
        lower_full = self.timeline_panel
        right = lower_full

        graph_tools = ttk.Frame(right, style="Panel.TFrame", padding=(5,2))
        graph_tools.pack(fill="x", pady=0)
        ttk.Checkbutton(graph_tools, text="Original", variable=self.show_original, command=self.draw_all).pack(side="left")
        ttk.Checkbutton(graph_tools, text="Optimisé", variable=self.show_preview, command=self.draw_all).pack(side="left", padx=(8,0))
        ttk.Checkbutton(graph_tools, text="Aperçu automatique", variable=self.auto_preview).pack(side="left", padx=(16,0))
        ttk.Checkbutton(graph_tools, text="Événements détectés", variable=self.show_detection_events, command=self.draw_all).pack(side="left", padx=(16,0))
        ttk.Checkbutton(
            graph_tools,
            text="Suivre lecture",
            variable=self.follow_detail_var
        ).pack(side="left", padx=(16,0))
        ttk.Button(graph_tools, text="Tout voir", command=self.zoom_reset).pack(side="right")
        ttk.Button(graph_tools, text="Zoom -", command=self.zoom_out).pack(side="right", padx=(4,0))
        ttk.Button(graph_tools, text="Zoom +", command=self.zoom_in).pack(side="right", padx=(4,0))

        detail_box = ttk.Frame(right, style="Panel.TFrame", padding=(3, 0))
        # La trame absorbe la hauteur attribuée par son séparateur.
        detail_box.pack(fill="both", expand=True)

        edit_bar = graph_tools

        ttk.Checkbutton(
            edit_bar,
            text="Trame éditable",
            variable=self.track_edit_enabled_var
        ).pack(side="left")

        ttk.Label(edit_bar, text="Mode :", style="Panel.TLabel").pack(side="left", padx=(10,3))
        ttk.Combobox(
            edit_bar,
            textvariable=self.track_edit_mode_var,
            values=("points", "shift", "lecteur vidéo"),
            state="readonly",
            width=13
        ).pack(side="left")

        ttk.Label(
            edit_bar,
            textvariable=self.track_sync_offset_ms_var,
            style="Panel.TLabel"
        ).pack(side="left", padx=(10,0))

        ttk.Label(
            edit_bar,
            text=(
                "Glisser : modifier / naviguer • Double-clic : ajouter • Clic droit : menu"
            ),
            style="Panel.TLabel"
        ).pack(side="left", padx=(12,0))

        ttk.Button(
            edit_bar,
            text="Annuler sélection",
            command=self._clear_track_edit_selection
        ).pack(side="right")
        self.graph = tk.Canvas(detail_box, height=180, bg="#090d10", highlightthickness=0)
        self.graph.pack(fill="both", expand=True)

        self.graph.bind("<Button-1>", self._mode_graph_press)
        self.graph.bind("<B1-Motion>", self._mode_graph_drag)
        self.graph.bind("<ButtonRelease-1>", self._mode_graph_release)
        self.graph.bind("<Double-Button-1>", self._track_edit_add)
        self.graph.bind(
            "<Button-3>",
            lambda e: self._show_track_context_menu(e, self.graph, "detail")
        )
        self.graph.bind("<Configure>", lambda e: self.draw_detail())

        try:
            self.track_edit_mode_var.trace_add(
                "write",
                self._update_track_mode_cursor
            )
            self.after_idle(self._update_track_mode_cursor)
        except Exception:
            pass
        self.graph.bind("<MouseWheel>", self._mouse_zoom)
        self.graph.bind("<Button-4>", lambda e: self._mouse_zoom_linux(1, e))
        self.graph.bind("<Button-5>", lambda e: self._mouse_zoom_linux(-1, e))
        # IMPORTANT :
        # ne pas réassigner Button-1/B1-Motion ici.
        # _mode_graph_press/_drag/_release gèrent maintenant :
        # points = édition, shift = déplacement global, navigate = vidéo.

        stats = ttk.Frame(right, style="Panel.TFrame", padding=(3,0))
        stats.pack(side="bottom", fill="x", before=detail_box)
        self.stats_var = tk.StringVar(value="0 point | durée 0:00")
        ttk.Label(stats, textvariable=self.stats_var, style="Panel.TLabel").pack(side="left")
        ttk.Label(stats, text="Vert = funscript   Magenta = visuel   Cyan = ligne   Blanc = lecture",
                  style="Panel.TLabel").pack(side="right")

        # Pas de seconde copie de la trame.
        # Canvas caché conservé uniquement pour compatibilité interne.
        self.overview = tk.Canvas(
            right,
            width=1,
            height=1,
            bg="#090d10",
            highlightthickness=0
        )

        # Tableau et commandes d'épisode partagent la troisième zone redimensionnable.
        track_tabs = self.track_tabs

        # ----- Onglet Synchronisation -----
        sync_panel, sync_tab = self._scrollable_panel(track_tabs)
        track_tabs.add(sync_panel, text="Synchronisation")

        sync_row = ttk.Frame(sync_tab)
        sync_row.pack(fill="x", pady=1)

        ttk.Label(sync_row, text="Mode :").pack(side="left")
        ttk.Combobox(
            sync_row,
            textvariable=self.track_edit_mode_var,
            values=("points", "shift", "navigate"),
            state="readonly",
            width=11
        ).pack(side="left", padx=(4,12))

        ttk.Label(
            sync_row,
            textvariable=self.track_sync_offset_ms_var
        ).pack(side="left", padx=(0,12))

        for txt, delta in (
            ("-1 s", -1000),
            ("-100 ms", -100),
            ("+100 ms", 100),
            ("+1 s", 1000),
        ):
            ttk.Button(
                sync_row,
                text=txt,
                command=lambda d=delta: self._context_shift_track(d)
            ).pack(side="left", padx=2)

        ttk.Button(
            sync_row,
            text="Reset",
            command=self._context_reset_shift
        ).pack(side="left", padx=(6,0))

        ttk.Checkbutton(
            sync_row,
            text="Verrouiller",
            variable=self.track_locked_var
        ).pack(side="right")

        ttk.Label(
            sync_tab,
            text=(
                "points = édition des points  |  shift = déplacer toute la trame  |  "
                "navigate = faire défiler la vidéo avec la trame"
            )
        ).pack(anchor="w", pady=(1,0))

        # ----- Onglet Épisode -----
        episode_panel, episode_tab = self._scrollable_panel(track_tabs)
        track_tabs.add(episode_panel, text="Épisode")

        self.track_episode_select_var = tk.StringVar(value="")
        ep_top = ttk.Frame(episode_tab)
        ep_top.pack(fill="x", pady=1)

        ep_selection = ttk.Frame(ep_top)
        ep_selection.pack(side="left")
        ttk.Label(ep_selection, text="Épisode :").pack(side="left")
        self.track_episode_combo = ttk.Combobox(
            ep_selection,
            textvariable=self.track_episode_select_var,
            state="readonly",
            width=16
        )
        self.track_episode_combo.pack(side="left", padx=(5,12))
        self.track_episode_combo.bind(
            "<<ComboboxSelected>>",
            self._track_episode_option_selected
        )

        ep_name = ttk.Frame(ep_top)
        ep_name.pack(side="left")
        ttk.Label(ep_name, text="Nom :").pack(side="left")
        ttk.Entry(
            ep_name,
            textvariable=self.visual_episode_name_var,
            width=16
        ).pack(side="left", padx=(4,0))

        ep_pattern = ttk.Frame(ep_top)
        ep_pattern.pack(side="left")
        ttk.Label(ep_pattern, text="Amplitude fixe :").pack(side="left", padx=(12,3))
        self.visual_episode_pattern_combo = ttk.Combobox(
            ep_pattern,
            textvariable=self.visual_episode_pattern_var,
            values=EPISODE_PATTERN_NAMES,
            state="readonly",
            width=24
        )
        self.visual_episode_pattern_combo.pack(side="left", padx=(3,0))
        self.visual_episode_pattern_combo.bind(
            "<<ComboboxSelected>>", self._on_visual_episode_pattern_selected
        )

        ep_values = ttk.Frame(episode_tab)
        ep_values.pack(fill="x", pady=1)

        for label, var in (
            ("Haut %", self.visual_episode_high_var),
            ("Bas %", self.visual_episode_low_var),
            ("Haut °", self.visual_episode_high_deg_var),
            ("Bas °", self.visual_episode_low_deg_var),
            ("Offset local", self.visual_episode_offset_var),
        ):
            ttk.Label(ep_values, text=label).pack(side="left", padx=(7,2))
            ttk.Entry(ep_values, textvariable=var, width=6).pack(side="left")

        ep_flags = ttk.Frame(episode_tab)
        ep_flags.pack(fill="x", pady=1)

        ttk.Checkbutton(
            ep_flags,
            text="Actif",
            variable=self.visual_episode_enabled_var
        ).pack(side="left")
        ttk.Checkbutton(
            ep_flags,
            text="Inverser",
            variable=self.visual_episode_invert_var
        ).pack(side="left", padx=(10,0))

        ttk.Label(ep_flags, text="Départ :").pack(side="left", padx=(12,3))
        ttk.Radiobutton(
            ep_flags,
            text="Haut",
            value="high",
            variable=self.visual_episode_start_side_var
        ).pack(side="left")
        ttk.Radiobutton(
            ep_flags,
            text="Bas",
            value="low",
            variable=self.visual_episode_start_side_var
        ).pack(side="left")

        ttk.Label(ep_flags, text="Pause :").pack(side="left", padx=(12,3))
        ttk.Combobox(
            ep_flags,
            textvariable=self.visual_episode_pause_mode_var,
            values=("hold", "center", "low", "high"),
            state="readonly",
            width=8
        ).pack(side="left")

        ttk.Button(
            ep_flags,
            text="Supprimer",
            command=self.delete_selected_visual_episode
        ).pack(side="right", padx=(6,0))

        ttk.Button(
            ep_flags,
            text="Appliquer",
            command=self.apply_visual_episode_range
        ).pack(side="right")

        # ----- Onglet Édition -----
        edit_panel, edit_tab = self._scrollable_panel(track_tabs)
        track_tabs.add(edit_panel, text="Édition")

        ttk.Checkbutton(
            edit_tab,
            text="Trame éditable",
            variable=self.track_edit_enabled_var
        ).pack(side="left")

        ttk.Button(
            edit_tab,
            text="Annuler sélection",
            command=self._clear_track_edit_selection
        ).pack(side="left", padx=(12,4))

        ttk.Button(
            edit_tab,
            text="Tout voir",
            command=self.zoom_reset
        ).pack(side="left", padx=4)

        ttk.Label(
            edit_tab,
            text=(
                "Clic droit sur la trame : menu complet • "
                "Double-clic : ajouter • Glisser : déplacer"
            )
        ).pack(side="left", padx=(14,0))

        track_tabs.insert(0, episode_panel)
        track_tabs.select(episode_panel)
        # Les barres se replient ; les onglets défilants conservent tous les contrôles.
        self._flow_controls(graph_tools)
        self._flow_controls(ep_top)
        self._flow_controls(ep_values)
        self._flow_controls(ep_flags)
        self._flow_controls(sync_row)
        self._flow_controls(edit_tab)
        for pane in (self.workspace_paned, self.main_paned, self.episode_paned):
            pane.bind("<Configure>", self._fit_workspace)
            pane.bind("<ButtonPress-1>", self._start_layout_drag)
            pane.bind("<ButtonRelease-1>", self._fit_workspace)
        self.after(350, self._initialize_workspace)

        try:
            self.after_idle(self._show_visual_left_section)
        except Exception:
            pass

        bottom = ttk.Frame(self, style="Toolbar.TFrame", padding=(6,2))
        bottom.pack(side="bottom", fill="x", before=self.main_outer)
        ttk.Label(bottom, textvariable=self.status_var, style="Toolbar.TLabel").pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(bottom, mode="determinate", length=250)
        self.progress.pack(side="right")

        # Applique le dernier mode mémorisé après construction complète de l'interface.
        self.after(50, lambda: self.set_interface_mode("cockhero_visual", save=False))


    # ------------------------------------------------------------
    # MODE D'INTERFACE
    # ------------------------------------------------------------
    def set_interface_mode(self, mode, save=True):
        mode = "cockhero_visual" if str(mode).lower() in ("cockhero_visual", "cockhero") else "optimisation"
        self.interface_mode.set(mode)
        self.notebook.pack_forget()
        self.visual_settings_panel.pack_forget()
        if mode == "cockhero_visual":
            self.visual_settings_panel.pack(fill="both", expand=True)
            self.btn_cockhero_generate.configure(text="Générer CockHero Visuel",
                                                 command=self.generate_cockhero_visual)
            widgets = [self.btn_cockhero_generate, self.btn_cockhero_open,
                       self.btn_cockhero_save, self.btn_cockhero_batch, self.chk_batch_overwrite]
            self.title(f"CockHero Funscript Generator - Analyse visuelle - {APP_VERSION}")
            self.status_var.set("CockHero Visuel — détection de toute forme qui atteint la ligne en bas de l'écran.")
        else:
            self.notebook.pack(fill="both", expand=True)
            widgets = [self.btn_open_funscript, self.btn_open_folder, self.btn_create_video,
                       self.btn_preview, self.btn_apply, self.btn_process_folder]
            self.title(f"Funscript Optimisator (mode optionnel) - {APP_VERSION}")
            self.status_var.set("Mode optimisation classique.")
        self._flow_controls(self.action_toolbar, widgets)
        if save:
            self._save_app_config()


    # ------------------------------------------------------------
    # ENVIRONNEMENT AUDIO / LIBROSA
    # ------------------------------------------------------------

    # ------------------------------------------------------------
    # GENERIC UI
    # ------------------------------------------------------------
    def _tooltip(self, widget, text):
        popup = {"win": None}
        def show(_):
            if popup["win"] is not None:
                return
            win = tk.Toplevel(widget)
            win.overrideredirect(True)
            win.configure(bg=self.COL_BORDER)
            x = widget.winfo_rootx() + 10
            y = widget.winfo_rooty() + widget.winfo_height() + 5
            win.geometry(f"+{x}+{y}")
            tk.Label(win, text=text, bg=self.COL_PANEL2, fg=self.COL_TEXT,
                     padx=8, pady=5, wraplength=340, justify="left").pack(padx=1, pady=1)
            popup["win"] = win
        def hide(_):
            if popup["win"] is not None:
                popup["win"].destroy()
                popup["win"] = None
        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)

    def _auto_refresh(self):
        if self.auto_preview.get() and self.current_file:
            self.after(120, self.generate_preview)

    def apply_parameters(self):
        errors = []
        for name, (var, typ, default) in self.param_vars.items():
            try:
                globals()[name] = typ(var.get().strip())
            except Exception:
                errors.append(name)

        for name, var in self.toggle_vars.items():
            globals()[name] = bool(var.get())

        globals()["FALLBACK_MIN_RUN_POINTS"] = int(globals()["FALLBACK_MIN_SAME_HEIGHT_POINTS"]) * 2
        globals()["FALLBACK_MAX_POINT_GAP_MS"] = int(globals()["DEADTIME_MS"])

        # Custom patterns
        try:
            globals()["USE_CUSTOM_GAP_PATTERNS"] = bool(self.use_patterns_var.get())
            globals()["CUSTOM_GAP_PATTERN_RANDOM"] = bool(self.pattern_random_var.get())
            globals()["CUSTOM_GAP_PATTERN_STEP_MS"] = int(self.pattern_step_var.get())
            globals()["CUSTOM_GAP_PATTERN_MIN"] = int(self.pattern_min_var.get())
            globals()["CUSTOM_GAP_PATTERN_MAX"] = int(self.pattern_max_var.get())
            globals()["CUSTOM_GAP_SELECTED"] = list(self.pattern_list.curselection()) or [0]

            globals()["USE_CUSTOM_END_PATTERNS"] = bool(self.use_end_patterns_var.get())
            globals()["CUSTOM_END_PATTERN_RANDOM"] = bool(self.end_pattern_random_var.get())
            globals()["CUSTOM_END_PATTERN_STEP_MS"] = int(self.end_pattern_step_var.get())
            globals()["CUSTOM_END_PATTERN_MIN"] = int(self.end_pattern_min_var.get())
            globals()["CUSTOM_END_PATTERN_MAX"] = int(self.end_pattern_max_var.get())
            globals()["CUSTOM_END_SELECTED"] = list(self.end_pattern_list.curselection()) or [8]

            globals()["END_FILL_FADE_ENABLED"] = bool(self.end_fade_enabled_var.get())
            globals()["END_FILL_FINAL_STRENGTH"] = max(0.0, min(1.0, float(self.end_final_strength_var.get())))
            globals()["CUSTOM_END_FADE_DURATION_MS"] = max(0, int(self.end_fade_duration_var.get()))
            globals()["CUSTOM_END_TARGET_POS"] = max(0, min(100, int(self.end_target_pos_var.get())))
            globals()["CUSTOM_END_HOLD_MS"] = max(0, int(self.end_hold_ms_var.get()))
        except Exception:
            errors.append("Patterns / Fin")

        if errors:
            messagebox.showerror("Paramètres invalides", "Valeur invalide :\n" + "\n".join(errors))
            return False
        return True

    def reset_defaults(self):
        for name, (var, typ, default) in self.param_vars.items():
            var.set(str(default))
        defaults = {
            "REMOVE_OLD_FALLBACK_RUNS": True,
            "AUTO_DETECT_REAL_MOTION_START": True,
            "MIX_PREVIOUS_NEXT_CYCLES": True,
            "PROGRESSIVE_INITIAL_GAP": True,
            "SOFTEN_FIRST_ORIGINAL_BLOCK": True,
            "SMOOTH_GAP_TRANSITIONS": True,
            "END_FILL_FADE_ENABLED": True,
        }
        for name, var in self.toggle_vars.items():
            var.set(defaults.get(name, True))
        self.use_patterns_var.set(False)
        self.pattern_random_var.set(True)
        self.pattern_step_var.set("180")
        self.pattern_min_var.set("10")
        self.pattern_max_var.set("90")
        self.pattern_list.selection_clear(0, "end")
        self.pattern_list.selection_set(0)

        self.use_end_patterns_var.set(False)
        self.end_pattern_random_var.set(True)
        self.end_pattern_step_var.set("220")
        self.end_pattern_min_var.set("10")
        self.end_pattern_max_var.set("90")
        self.end_pattern_list.selection_clear(0, "end")
        self.end_pattern_list.selection_set(8)

        self.end_fade_enabled_var.set(True)
        self.end_fade_duration_var.set("10000")
        self.end_final_strength_var.set("0.08")
        self.end_target_pos_var.set("50")
        self.end_hold_ms_var.set("0")

        self._auto_refresh()

    # ------------------------------------------------------------
    # FILES
    # ------------------------------------------------------------
    def _initial_dir(self):
        if self.current_file:
            return str(Path(self.current_file).parent)
        last_dir = self.saved_config.get("last_dir")
        if last_dir and Path(last_dir).exists():
            return last_dir
        return os.getcwd()

    def _extract_cockhero_markers_from_data(self, data):
        """
        Retrouve les marqueurs d'un funscript.

        Priorité :
        1) métadonnées écrites par ce programme;
        2) détection de la logique "sommet + creux au milieu";
        3) sinon, chaque point du funscript est considéré comme une extrémité.
        """
        metadata = data.get("metadata", {}) if isinstance(data, dict) else {}
        chmeta = metadata.get("cockheroGenerator", {}) if isinstance(metadata, dict) else {}
        markers = chmeta.get("markers") if isinstance(chmeta, dict) else None
        logic = chmeta.get("logic") if isinstance(chmeta, dict) else None

        if isinstance(markers, list) and markers:
            clean = sorted({max(0, int(x)) for x in markers})
            return clean, logic or "marker_peaks", "métadonnées CockHero"

        actions = normalize_actions(data.get("actions", []))
        if len(actions) < 2:
            return [], None, "aucun marqueur"

        # Retire les points de maintien ajoutés au début/à la fin.
        work = [dict(a) for a in actions]
        if len(work) >= 2 and work[0]["at"] == 0 and work[0]["pos"] == work[1]["pos"]:
            work = work[1:]
        if len(work) >= 2 and work[-1]["pos"] == work[-2]["pos"]:
            work = work[:-1]

        if len(work) < 3:
            return [int(a["at"]) for a in work], "marker_alternating", "points du funscript"

        positions = [int(a["pos"]) for a in work]
        pmin, pmax = min(positions), max(positions)
        mid = (pmin + pmax) / 2.0
        high_threshold = mid + (pmax - pmin) * 0.20
        low_threshold = mid - (pmax - pmin) * 0.20

        high_indices = [i for i,a in enumerate(work) if a["pos"] >= high_threshold]
        low_indices = [i for i,a in enumerate(work) if a["pos"] <= low_threshold]

        midpoint_lows = 0
        tested_lows = 0
        for i in low_indices:
            if i <= 0 or i >= len(work)-1:
                continue
            prev_a, cur_a, next_a = work[i-1], work[i], work[i+1]
            if prev_a["pos"] >= high_threshold and next_a["pos"] >= high_threshold:
                tested_lows += 1
                expected = (int(prev_a["at"]) + int(next_a["at"])) / 2.0
                span = max(1, int(next_a["at"]) - int(prev_a["at"]))
                if abs(int(cur_a["at"]) - expected) <= max(40.0, span * 0.18):
                    midpoint_lows += 1

        ratio = midpoint_lows / tested_lows if tested_lows else 0.0

        if ratio >= 0.65 and high_indices:
            markers = [int(work[i]["at"]) for i in high_indices]
            return sorted(set(markers)), "marker_peaks", "sommets + creux intermédiaires"

        return sorted({int(a["at"]) for a in work}), "marker_alternating", "extrémités haut/bas"

    def open_funscript_for_logic_conversion(self):
        path = filedialog.askopenfilename(
            title="Ouvrir un funscript à convertir",
            initialdir=self._initial_dir(),
            filetypes=[("Funscript", "*.funscript"), ("Tous les fichiers", "*.*")]
        )
        if not path:
            return
        self.load_file(path)
        self.set_interface_mode("cockhero_visual")

    def convert_loaded_funscript_logic(self):
        if not self.original_data or not self.original_data.get("actions"):
            messagebox.showinfo(
                "Conversion",
                "Ouvre d'abord un .funscript à convertir."
            )
            return

        markers = list(self.loaded_marker_times_ms)
        if not markers:
            markers, source_logic, source_desc = self._extract_cockhero_markers_from_data(
                self.original_data
            )
            self.loaded_marker_times_ms = markers
            self.loaded_marker_source_logic = source_logic
        else:
            source_logic = self.loaded_marker_source_logic
            source_desc = "marqueurs déjà chargés"

        if not markers:
            messagebox.showwarning("Conversion", "Aucun marqueur exploitable trouvé.")
            return

        actions_in = normalize_actions(self.original_data.get("actions", []))
        duration_ms = max(int(a["at"]) for a in actions_in)
        observed_min = min(int(a["pos"]) for a in actions_in)
        observed_max = max(int(a["pos"]) for a in actions_in)

        # Conserve l'amplitude du fichier ouvert.
        self.cockhero_min_var.set(str(observed_min))
        self.cockhero_max_var.set(str(observed_max))

        target_logic = self.visual_generation_logic_var.get()
        actions = generate_cockhero_visual_full_range(
            duration_ms=duration_ms,
            event_times_ms=markers,
            min_pos=observed_min,
            max_pos=observed_max,
            logic=target_logic,
        )

        converted = copy.deepcopy(self.original_data)
        converted["actions"] = actions
        metadata = converted.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
            converted["metadata"] = metadata
        metadata["cockheroGenerator"] = {
            "version": APP_VERSION,
            "logic": target_logic,
            "markers": markers,
        }

        self.preview_data = converted
        logic_name = (
            "sommet + creux au milieu"
            if target_logic == "marker_peaks"
            else "extrémités directes haut/bas"
        )
        self.file_var.set(
            f"APERÇU CONVERTI — {Path(self.current_file).name if self.current_file else 'funscript'}"
        )
        self.stats_var.set(
            f"{len(markers)} marqueurs → {len(actions)} points | logique : {logic_name}"
        )
        self.status_var.set(
            f"Conversion prête : {logic_name}. Clique Enregistrer pour créer le nouveau .funscript."
        )
        self.draw_all()

    def open_file(self):
        path = filedialog.askopenfilename(
            title="Choisir un funscript",
            initialdir=self._initial_dir(),
            filetypes=[("Funscript", "*.funscript"), ("Tous les fichiers", "*.*")]
        )
        if path:
            self.load_file(path)

    def choose_folder(self):
        folder = filedialog.askdirectory(title="Choisir le dossier", initialdir=self._initial_dir())
        if not folder:
            return
        global CURRENT_DIR
        CURRENT_DIR = folder
        self.saved_config["last_dir"] = folder
        files = sorted(Path(folder).glob("*.funscript"))
        self.status_var.set(f"{len(files)} funscript(s) dans {folder}")
        if files:
            self.load_file(str(files[0]))

    def choose_funscript_for_editing(self):
        """Ouvre un funscript existant pour reprendre son édition."""
        path = filedialog.askopenfilename(
            title="Ouvrir un funscript pour continuer l'édition",
            initialdir=self._initial_dir(),
            filetypes=[
                ("Funscript", "*.funscript"),
                ("JSON", "*.json"),
                ("Tous les fichiers", "*.*"),
            ],
        )
        if not path:
            return
        self.load_file(path, continue_editing=True)

    def load_file(self, path, continue_editing=False):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            actions = normalize_actions(data.get("actions", []))
            if not actions:
                raise ValueError("aucune action valide")

            self.current_file = str(path)
            self.current_funscript_name_var.set(
                f"FUNSCRIPT : {Path(path).name}"
            )
            self.original_data = copy.deepcopy(data)
            self.original_data["actions"] = actions

            # En reprise d'édition, le fichier chargé devient directement la
            # trame de travail. Ainsi les déplacements, ajouts, suppressions,
            # épisodes et corrections souris modifient cette copie éditable.
            if continue_editing:
                self.preview_data = copy.deepcopy(self.original_data)
            else:
                self.preview_data = None

            (
                self.loaded_marker_times_ms,
                self.loaded_marker_source_logic,
                marker_source_desc
            ) = self._extract_cockhero_markers_from_data(self.original_data)

            global CURRENT_DIR
            CURRENT_DIR = str(Path(path).parent)
            self.saved_config["last_file"] = self.current_file
            self.saved_config["last_dir"] = CURRENT_DIR

            self.file_var.set(f"SAUVEGARDÉ — {Path(path).name}")
            self.zoom_start = 0.0
            self.zoom_end = 1.0

            duration = max(int(a["at"]) for a in actions)
            self.stats_var.set(f"{len(actions)} points   |   durée {self._format_time(duration)}")
            if continue_editing:
                self.status_var.set(
                    f"Édition reprise — {Path(path).name} — "
                    f"{len(actions)} points chargés. "
                    "La trame peut être modifiée puis enregistrée."
                )
            else:
                self.status_var.set(
                    f"Fichier chargé — {len(self.loaded_marker_times_ms)} marqueurs "
                    f"interprétés ({marker_source_desc})."
                )

            self._auto_find_video()

            # En reprise d'édition, restaurer d'abord les épisodes exacts
            # sauvegardés dans les métadonnées. Si le fichier est ancien et
            # n'en contient pas, les reconstruire depuis les marqueurs.
            if continue_editing:
                restored = self._restore_visual_episodes_from_metadata(
                    self.original_data
                )

                if restored:
                    # Ne pas utiliser E1 restauré comme vérité absolue :
                    # les anciennes métadonnées peuvent contenir un faux E1.
                    # On cherche le premier groupe réellement soutenu dans
                    # les actions elles-mêmes.
                    try:
                        removed_leading, first_stable_start = (
                            self._trim_before_first_sustained_group()
                        )

                        if removed_leading:
                            self._rebuild_episodes_from_action_gaps(1500)
                            self.status_var.set(
                                f"Édition reprise — faux départ supprimé : "
                                f"{removed_leading} point(s) avant "
                                f"{self._format_time(first_stable_start)}."
                            )
                    except Exception:
                        pass

                elif self.loaded_marker_times_ms:
                    try:
                        self.rebuild_visual_episodes(
                            self.loaded_marker_times_ms
                        )
                    except Exception:
                        pass

                    # Pour un ancien fichier sans métadonnées d'épisodes,
                    # conserver les heuristiques comme solution de repli.
                    try:
                        sparse_removed = self._remove_leading_sparse_block(
                            scan_window_ms=45000,
                            abnormal_gap_ms=1200,
                            stable_min_actions=30,
                            stable_window_ms=5000,
                        )

                        bad_groups, bad_actions = self._remove_leading_track_anomalies(
                            max_gap_ms=1500,
                            min_duration_ms=7000,
                            min_actions=24,
                            min_density_hz=2.2,
                        )

                        total_removed = sparse_removed + bad_actions

                        if total_removed:
                            self._rebuild_episodes_from_action_gaps(1500)
                            self.status_var.set(
                                f"Édition reprise — anomalies de début supprimées : "
                                f"{total_removed} point(s) retiré(s)."
                            )
                    except Exception:
                        pass

            self._save_app_config()

            if continue_editing:
                self.status_var.set(
                    f"Édition reprise — {Path(path).name} — "
                    f"{len(actions)} points — "
                    f"{len(self.visual_episodes)} épisode(s) restauré(s)."
                )

            self.draw_all()

        except Exception as exc:
            messagebox.showerror("Erreur", f"Impossible de charger le fichier :\n{exc}")

    def _auto_find_video(self):
        if not self.current_file:
            return
        base = Path(self.current_file).with_suffix("")
        for ext in VIDEO_EXTENSIONS:
            candidate = Path(str(base) + ext)
            if candidate.exists():
                self.load_video(str(candidate), preserve_funscript=True)
                return
        last_video = self.saved_config.get("last_video")
        if last_video and Path(last_video).exists():
            self.load_video(last_video, preserve_funscript=True)

    # ------------------------------------------------------------
    # VIDEO
    # ------------------------------------------------------------
    def choose_video(self):
        path = filedialog.askopenfilename(
            title="Choisir la vidéo",
            initialdir=self._initial_dir(),
            filetypes=[("Vidéos", "*.mp4 *.mkv *.avi *.mov *.webm"), ("Tous les fichiers", "*.*")]
        )
        if path:
            self.load_video(path)

    def load_video(self, path, preserve_funscript=False):
        self.current_video = str(path)
        self.video_var.set(Path(path).name)
        self.current_video_name_var.set(
            f"VIDÉO : {Path(path).name}"
        )
        self.saved_config["last_video"] = self.current_video

        # Une vidéo choisie seule invalide les données précédentes.
        # Par contre, lorsqu'elle est chargée automatiquement avec un
        # funscript existant, il faut absolument conserver la trame.
        if not preserve_funscript:
            self.detected_visual_times_ms = []
            self.detected_line_times_ms = []
            self.preview_data = None
            self.original_data = None
            self.visual_detection_status_var.set("Aucun indicateur visuel analysé")
            self.visual_line_status_var.set("Ligne visuelle non analysée")
            self.file_var.set(f"Vidéo : {Path(path).name}")
            self.current_funscript_name_var.set("FUNSCRIPT : aucun — non enregistré")

        self.mpv.start(self.current_video)
        self._save_app_config()
        self.draw_all()

        # load_video() n'effectue aucune génération de points.
        # Le nombre de points provient du funscript éventuellement déjà chargé.
        if preserve_funscript:
            try:
                actions_loaded = self._editable_actions()
                self.status_var.set(
                    f"Vidéo associée chargée — {len(actions_loaded)} points conservés."
                )
            except Exception:
                self.status_var.set("Vidéo associée chargée — trame conservée.")
        else:
            self.status_var.set(f"Vidéo chargée : {Path(path).name}")

        try:
            self.btn_generate_visual_panel.configure(state="normal")
        except Exception:
            pass

    def toggle_video(self):
        self.mpv.toggle_pause()

    def seek_relative(self, seconds):
        current = self.mpv.time_pos()
        if current is not None:
            self.mpv.seek_absolute(max(0.0, current + float(seconds)))

    def _seek_scale_changed(self, value):
        if self._updating_seek_scale:
            return
        duration = self.mpv.duration()
        if not duration or duration <= 0:
            return
        try:
            ratio = float(value) / 1000.0
            self.mpv.seek_absolute(max(0.0, min(duration, ratio * duration)))
        except Exception:
            pass

    def _tick_video(self):
        try:
            pos = self.mpv.time_pos()
            dur = self.mpv.duration()
            if pos is not None:
                self.playhead_ms = int(pos * 1000)

                if dur and dur > 0:
                    self._updating_seek_scale = True
                    self.seek_scale.set(max(0, min(1000, (pos / dur) * 1000)))
                    self._updating_seek_scale = False

                    if self.follow_detail_var.get():
                        self._follow_detail_playhead(int(float(dur) * 1000))

                # Position funscript interpolée pour l'animation verticale.
                self.motion_last_pos = self.motion_pos
                self.motion_pos = self._position_at_time(self.playhead_ms)

                self.draw_playheads_only()
                self._draw_motion_indicator()
        except Exception:
            pass

        # 10 Hz : animation nettement plus fluide que l'ancien 5 Hz.
        self.after(100, self._tick_video)


    # ------------------------------------------------------------
    # CREATION DEPUIS VIDEO SEULE
    # ------------------------------------------------------------
    def create_from_video_dialog(self):
        path = filedialog.askopenfilename(
            title="Choisir une vidéo",
            initialdir=self._initial_dir(),
            filetypes=[
                ("Vidéos", "*.mp4 *.mkv *.avi *.mov *.webm"),
                ("Tous les fichiers", "*.*"),
            ],
        )
        if not path:
            return
        self.load_video(path)
        self.current_file = None
        self.original_data = None
        self.preview_data = None
        self.file_var.set("Nouveau funscript")
        self.zoom_start = 0.0
        self.zoom_end = 1.0
        self.generate_from_current_video()

    def _video_duration_ms_for_generation(self):
        if not self.current_video:
            return None

        # D'abord via mpv si disponible
        try:
            d = self.mpv.duration()
            if d and d > 0:
                return int(float(d) * 1000)
        except Exception:
            pass

        # Sinon via ffprobe
        try:
            result = subprocess.check_output(
                [
                    "ffprobe",
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    self.current_video,
                ],
                stderr=subprocess.DEVNULL,
            )
            return int(float(result.decode().strip()) * 1000)
        except Exception:
            return None

    def generate_from_current_video(self):
        if not self.current_video:
            self.choose_video()
            if not self.current_video:
                try:
                    self.btn_generate_visual_panel.configure(state="normal")
                except Exception:
                    pass
                self._set_generation_progress(0.0, "Prête")
                return

        duration_ms = self._video_duration_ms_for_generation()
        if not duration_ms:
            messagebox.showerror(
                "Durée vidéo",
                "Impossible de déterminer la durée de la vidéo.\nVérifie que ffprobe/mpv est installé."
            )
            return

        try:
            selected = list(self.video_gen_pattern_list.curselection()) or [8]
            actions = generate_funscript_from_video_patterns(
                duration_ms=duration_ms,
                pattern_indices=selected,
                step_ms=int(self.video_gen_step_var.get()),
                min_pos=int(self.video_gen_min_var.get()),
                max_pos=int(self.video_gen_max_var.get()),
                random_patterns=bool(self.video_gen_random_var.get()),
                block_ms=int(self.video_gen_block_var.get()),
                start_fade_ms=int(self.video_gen_start_fade_var.get()),
                end_fade_ms=int(self.video_gen_end_fade_var.get()),
                start_strength=float(self.video_gen_start_strength_var.get()),
                end_strength=float(self.video_gen_end_strength_var.get()),
            )

            # Même contrôle de fin que pour l'optimisation d'un funscript existant.
            globals()["END_FILL_FADE_ENABLED"] = bool(self.end_fade_enabled_var.get())
            globals()["END_FILL_FINAL_STRENGTH"] = max(0.0, min(1.0, float(self.end_final_strength_var.get())))
            globals()["CUSTOM_END_FADE_DURATION_MS"] = max(0, int(self.end_fade_duration_var.get()))
            globals()["CUSTOM_END_TARGET_POS"] = max(0, min(100, int(self.end_target_pos_var.get())))
            globals()["CUSTOM_END_HOLD_MS"] = max(0, int(self.end_hold_ms_var.get()))
            actions = apply_custom_end_control(actions, duration_ms)
        except Exception as exc:
            messagebox.showerror("Paramètres de génération", str(exc))
            return

        generated = {
            "version": "1.0",
            "inverted": False,
            "range": 100,
            "actions": actions,
        }

        self.original_data = None
        self.preview_data = generated
        self.file_var.set(f"APERÇU NON SAUVEGARDÉ — {Path(self.current_video).stem}")
        self.stats_var.set(
            f"Généré {len(actions)} points   |   durée {self._format_time(duration_ms)}"
        )
        self.status_var.set(
            "Funscript généré depuis la vidéo — prévisualise puis enregistre si le résultat te convient."
        )
        self.zoom_start = 0.0
        self.zoom_end = 1.0
        self._save_app_config()
        self.draw_all()

    def save_generated_funscript(self):
        # Une trame générée OU un funscript rouvert en édition peut être sauvé.
        data_to_save = self.preview_data if self.preview_data else self.original_data
        if not data_to_save:
            messagebox.showinfo(
                "Enregistrer",
                "Génère ou ouvre d'abord un funscript."
            )
            return

        if self.current_video:
            default_path = str(Path(self.current_video).with_suffix(".funscript"))
        elif self.current_file:
            base = Path(self.current_file)
            default_path = str(base.with_name(base.stem + "_converti.funscript"))
        else:
            default_path = str(Path(self._initial_dir()) / "CockHero_converti.funscript")

        path = filedialog.asksaveasfilename(
            title="Enregistrer le funscript",
            initialdir=str(Path(default_path).parent),
            initialfile=Path(default_path).name,
            defaultextension=".funscript",
            filetypes=[("Funscript", "*.funscript")],
        )
        if not path:
            return

        try:
            # Actualiser les épisodes selon les trous de la trame.
            # Un trou > 1,5 s est une séparation d'épisode, pas une diagonale.
            self._rebuild_episodes_from_action_gaps(1500)

            # Sauvegarder l'état courant des épisodes afin qu'une prochaine
            # ouverture retrouve exactement l'édition en cours.
            metadata = data_to_save.setdefault("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
                data_to_save["metadata"] = metadata

            chmeta = metadata.setdefault("cockheroGenerator", {})
            if not isinstance(chmeta, dict):
                chmeta = {}
                metadata["cockheroGenerator"] = chmeta

            chmeta["version"] = APP_VERSION
            chmeta["logic"] = self.visual_generation_logic_var.get()
            chmeta["episodeGapMs"] = int(float(self.visual_episode_gap_var.get()))

            if self.loaded_marker_times_ms:
                chmeta["markers"] = list(self.loaded_marker_times_ms)
            elif self.detected_line_times_ms:
                chmeta["markers"] = list(self.detected_line_times_ms)

            chmeta["episodes"] = [
                {
                    "number": ep["index"],
                    "name": ep.get("name", f"Épisode {ep['index']}"),
                    "start": int(ep["start"]),
                    "end": int(ep["end"]),
                    "count": int(ep.get("count", len(ep.get("events", [])))),
                    "high": int(ep["high"]),
                    "low": int(ep["low"]),
                    "highDeg": int(ep.get("high_deg", round(ep["high"] * 1.8))),
                    "lowDeg": int(ep.get("low_deg", round(ep["low"] * 1.8))),
                    "enabled": bool(ep.get("enabled", True)),
                    "invert": bool(ep.get("invert", False)),
                    "startSide": ep.get("start_side", "high"),
                    "offsetMs": int(ep.get("offset_ms", 0)),
                    "pauseMode": ep.get("pause_mode", "hold"),
                    "color": ep.get("color", "#4dabf7"),
                }
                for ep in self.visual_episodes
            ]

            # Le preview_data est la seule source sauvegardée. En mode CockHero
            # visuel il a déjà été reconstruit à partir des événements nettoyés.
            if (
                self.interface_mode.get() == "cockhero_visual"
                and self.visual_started_count > 0
            ):
                self.status_var.set(
                    f"Sauvegarde de la trame nettoyée : "
                    f"{self.visual_raw_count} → {self.visual_merged_count} → "
                    f"{self.visual_started_count} événements."
                )

            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    data_to_save,
                    f,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )

            self.current_file = str(path)
            self.current_funscript_name_var.set(
                f"FUNSCRIPT : {Path(path).name}"
            )
            self.original_data = copy.deepcopy(data_to_save)
            # Conserver une copie éditable après sauvegarde : l'utilisateur
            # peut poursuivre immédiatement l'édition et sauvegarder à nouveau.
            self.preview_data = copy.deepcopy(data_to_save)
            self.file_var.set(f"SAUVEGARDÉ — {Path(path).name}")
            self.saved_config["last_file"] = self.current_file
            self.saved_config["last_dir"] = str(Path(path).parent)
            self._save_app_config()
            self.status_var.set(f"Nouveau funscript enregistré : {Path(path).name}")
            self.draw_all()

        except Exception as exc:
            messagebox.showerror("Enregistrement", str(exc))




    def _open_visual_advanced_options(self):
        """Fenêtre secondaire pour les réglages rarement utilisés."""
        win = tk.Toplevel(self)
        win.title("CockHero — Options avancées de détection")
        win.transient(self)
        win.geometry("460x520")
        win.minsize(380, 300)
        panel, body = self._scrollable_panel(win)
        panel.pack(fill="both", expand=True, padx=5, pady=5)

        fields = [
            ("Seuil différence pixel", self.visual_line_threshold_var),
            ("% pixels changés minimum", self.visual_line_min_changed_var),
            ("Intervalle minimum (ms)", self.visual_line_interval_var),
            ("Images analysées / sec", self.visual_line_fps_var),
            ("Position cible X (%)", self.visual_target_x_var),
            ("Largeur cible (%)", self.visual_target_width_var),
            ("Fusionner pics < (ms)", self.visual_merge_window_var),
            ("Fenêtre démarrage (ms)", self.visual_start_window_var),
            ("Événements min. démarrage", self.visual_start_min_events_var),
            ("Trou minimum à examiner (ms)", self.visual_recovery_gap_var),
            ("Seuil récupération (x)", self.visual_recovery_factor_var),
        ]

        for label, var in fields:
            row = ttk.Frame(body)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=label).pack(side="left", fill="x", expand=True)
            ttk.Entry(row, textvariable=var, width=10).pack(side="right")

        ttk.Separator(body).pack(fill="x", pady=8)

        ttk.Checkbutton(
            body,
            text="Ignorer la moitié droite de la piste",
            variable=self.visual_ignore_right_half_var
        ).pack(anchor="w", pady=3)

        ttk.Checkbutton(
            body,
            text="Détecter automatiquement le vrai début",
            variable=self.visual_auto_start_var
        ).pack(anchor="w", pady=3)

        ttk.Checkbutton(
            body,
            text="Récupérer prudemment les indicateurs manqués",
            variable=self.visual_recovery_var
        ).pack(anchor="w", pady=3)

        ttk.Checkbutton(
            body,
            text="Utiliser uniquement les franchissements nettoyés",
            variable=self.use_line_events_var
        ).pack(anchor="w", pady=3)

        ttk.Button(
            body,
            text="Fermer",
            command=win.destroy
        ).pack(fill="x", pady=(14,0))

    def _show_visual_left_section(self):
        selected = self.visual_left_section_var.get()
        for widgets in self.visual_left_sections.values():
            for widget in widgets:
                widget.pack_forget()
        for widget in self.visual_left_sections.get(selected, []):
            widget.pack(fill="x", padx=3, pady=2, before=self.visual_video_box)
        for panel, canvas, body in self._scroll_panels:
            if panel is self.visual_settings_panel:
                canvas.yview_moveto(0)
                break

    # ------------------------------------------------------------
    # COCKHERO VISUEL - LIGNE DE DETECTION
    # ------------------------------------------------------------

    def _set_analysis_ui(self, running, stage=None, progress=None):
        try:
            if hasattr(self, "video_analysis_cancel_button"):
                self.video_analysis_cancel_button.configure(
                    state="normal" if running else "disabled"
                )
        except Exception:
            pass
        self.analysis_running = bool(running)
        if stage is not None:
            self.analysis_stage_var.set(stage)
        if progress is not None:
            value = max(0.0, min(100.0, float(progress)))
            self.analysis_progress_var.set(value)
            try:
                self.visual_analysis_percent_var.set(f"{value:5.1f} %")
            except Exception:
                pass

        try:
            value = float(self.analysis_progress_var.get())
            stage_now = str(self.analysis_stage_var.get())
            self.visual_analysis_info_var.set(
                f"Analyse : {value:5.1f} % — {stage_now}"
            )
        except Exception:
            pass

        try:
            self.btn_detect_visual_line.configure(state="disabled" if running else "normal")
        except Exception:
            pass
        try:
            self.btn_cancel_visual_analysis.configure(state="normal" if running else "disabled")
        except Exception:
            pass

    def cancel_visual_analysis(self):
        if self.analysis_running:
            self.analysis_cancel_requested = True
            self.analysis_stage_var.set("Annulation demandée…")
            self.status_var.set("CockHero Visuel : annulation de l'analyse en cours…")

    def _poll_visual_analysis_queue(self):
        try:
            while True:
                kind, payload = self.analysis_queue.get_nowait()

                if kind == "progress":
                    pct, stage = payload
                    pct = max(0.0, min(100.0, float(pct)))
                    self.analysis_progress_var.set(pct)
                    try:
                        self.visual_analysis_percent_var.set(f"{pct:5.1f} %")
                    except Exception:
                        pass
                    self.analysis_stage_var.set(stage)
                    try:
                        self.visual_analysis_info_var.set(
                            f"Analyse : {pct:5.1f} % — {stage}"
                        )
                    except Exception:
                        pass
                    self.status_var.set(f"CockHero Visuel : {stage}")
                    try:
                        self.update_idletasks()
                    except Exception:
                        pass

                elif kind == "done":
                    (
                        events, line_y, actual_band, effective_changed, target_x_found,
                        raw_event_count, merged_event_count, started_event_count,
                        recovered_count, audio_beat_count, audio_matches,
                        audio_offset_ms, audio_phase_steps, audio_correction_applied
                    ) = payload
                    self.detected_line_times_ms = events
                    self.visual_raw_count = raw_event_count
                    self.visual_merged_count = merged_event_count
                    self.visual_started_count = started_event_count
                    self.visual_target_x_var.set(f"{target_x_found:.1f}")
                    self.use_line_events_var.set(bool(events))
                    audio_diag = (
                        f" | AUDIO: {audio_beat_count} beats, "
                        f"{audio_matches} correspondances, "
                        f"phase {audio_phase_steps:+d} pas, "
                        f"décalage {audio_offset_ms:+.0f} ms, "
                        + ("CORRECTION APPLIQUÉE" if audio_correction_applied
                           else "aucune correction")
                    )
                    if audio_beat_count > 0:
                        self.visual_audio_status_var.set(
                            f"AUDIO : ACTIF | Beats : {audio_beat_count} | "
                            f"Correspondances : {audio_matches} | "
                            f"Phase : {audio_phase_steps:+d} pas | "
                            f"Décalage : {audio_offset_ms:+.0f} ms | "
                            f"Correction : {'OUI' if audio_correction_applied else 'NON'}"
                        )
                    else:
                        self.visual_audio_status_var.set(
                            "AUDIO : AUCUN BEAT DÉTECTÉ | Beats : 0 | "
                            "Correspondances : 0 | Phase : +0 pas | Correction : NON"
                        )
                    self.visual_line_status_var.set(
                        f"Bruts {raw_event_count} → fusion {merged_event_count} → "
                        f"récupérés +{recovered_count} → final {started_event_count} | "
                        f"cible X {target_x_found:.1f}%"
                        + (" | moitié droite ignorée" if self.visual_ignore_right_half_var.get() else "")
                        + audio_diag
                    )
                    self.analysis_stage_var.set(
                        f"Terminé — {len(events)} passages détectés."
                    )
                    self.analysis_progress_var.set(100.0)
                    try:
                        self.visual_analysis_percent_var.set("100.0 %")
                        self.visual_analysis_info_var.set("Analyse : 100.0 % — Terminée")
                    except Exception:
                        pass
                    self.status_var.set(
                        f"Analyse terminée — AUDIO: {audio_beat_count} beats | "
                        f"{audio_matches} correspondances | phase {audio_phase_steps:+d} pas | "
                        f"décalage {audio_offset_ms:+.0f} ms | "
                        + ("correction appliquée." if audio_correction_applied
                           else "aucune correction audio.")
                    )
                    self._set_analysis_ui(False)
                    self.rebuild_visual_episodes(events)
                    self._save_app_config()
                    self.draw_all()

                    # v4.14.6 : enchaînement automatique analyse -> génération.
                    if self.visual_auto_generate_pending:
                        self.visual_auto_generate_pending = False
                        if self.batch_running:
                            self.after(80, self._batch_after_analysis)
                        else:
                            self.after(80, self.generate_cockhero_visual)
                    return

                elif kind == "cancelled":
                    self.analysis_stage_var.set("Analyse annulée.")
                    self.status_var.set("CockHero Visuel : analyse annulée.")
                    self._set_analysis_ui(False)
                    return

                elif kind == "error":
                    self.analysis_stage_var.set("Échec de l'analyse.")
                    self._set_analysis_ui(False)
                    messagebox.showerror("CockHero Visuel", str(payload))
                    return
        except queue.Empty:
            pass

        if self.analysis_running:
            self.after(50, self._poll_visual_analysis_queue)

    def _visual_analysis_worker(
        self, video_path, line_y, band_pct, pixel_threshold,
        min_changed_pct, min_interval, fps, sync_offset_ms=0,
        target_x_pct=50.0, target_width_pct=3.0, auto_target=True,
        merge_window_ms=170, auto_start=True,
        start_window_ms=5000, start_min_events=8,
        recovery_enabled=True, recovery_gap_ms=650,
        recovery_factor=0.82,
        ignore_right_half=True,
        reverse_from_end=False
    ):
        raw_path = None
        try:
            self.analysis_queue.put(("progress", (2.0, "Lecture des informations vidéo…")))

            probe = subprocess.check_output(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height,duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    video_path,
                ],
                stderr=subprocess.DEVNULL,
                text=True
            ).strip().splitlines()

            if len(probe) < 2:
                raise RuntimeError("Impossible de lire les dimensions de la vidéo.")

            sw = int(float(probe[0]))
            sh = int(float(probe[1]))
            duration_s = 0.0
            if len(probe) >= 3:
                try:
                    duration_s = max(0.0, float(probe[2]))
                except Exception:
                    duration_s = 0.0

            if self.analysis_cancel_requested:
                self.analysis_queue.put(("cancelled", None))
                return

            top_pct = max(0.0, line_y - band_pct / 2.0)
            bottom_pct = min(100.0, line_y + band_pct / 2.0)
            actual_band = max(0.5, bottom_pct - top_pct)

            out_w = 480
            crop_h = max(2, int(round(sh * actual_band / 100.0)))
            out_h = max(2, int(round(crop_h * out_w / max(1, sw))))

            vf = (
                f"crop=iw:ih*{actual_band/100.0}:0:ih*{top_pct/100.0},"
                f"scale={out_w}:-1,format=gray,fps={fps}"
            )

            fd, raw_path = tempfile.mkstemp(prefix="funscript_line_", suffix=".raw")
            os.close(fd)

            self.analysis_queue.put(("progress", (8.0, "Extraction de la bande au bas de l'écran…")))

            # Extraction en arrière-plan. L'interface reste libre.
            proc = subprocess.Popen(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", video_path,
                    "-an", "-vf", vf,
                    "-f", "rawvideo", "-pix_fmt", "gray", raw_path
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True
            )

            # Comme ffmpeg n'expose pas ici sa progression facilement, on affiche
            # une progression douce estimée jusqu'à la fin de l'extraction.
            estimate = 10.0
            while proc.poll() is None:
                if self.analysis_cancel_requested:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    self.analysis_queue.put(("cancelled", None))
                    return
                estimate = min(38.0, estimate + 0.7)
                self.analysis_queue.put((
                    "progress",
                    (estimate, "Extraction des images de la ligne de détection…")
                ))
                import time
                time.sleep(0.25)

            stderr_text = proc.stderr.read() if proc.stderr else ""
            if proc.returncode != 0:
                raise RuntimeError(
                    "FFmpeg n'a pas pu analyser la bande de détection.\n\n" +
                    stderr_text[-1200:]
                )

            if self.analysis_cancel_requested:
                self.analysis_queue.put(("cancelled", None))
                return

            self.analysis_queue.put(("progress", (42.0, "Chargement des images extraites…")))
            frame_size = out_w * out_h
            data = Path(raw_path).read_bytes()
            frame_count = len(data) // frame_size
            if frame_count < 3:
                raise RuntimeError("Pas assez d'images pour effectuer la détection.")

            # --------------------------------------------------------
            # Moteur v3.8 — détecteur "porte visuelle".
            #
            # Calibré sur les deux couples vidéo + funscript Flux fournis.
            # Les tests de référence montrent qu'une bande très basse
            # (environ 94–100 % de la hauteur) et une porte verticale vers
            # 45 % de la largeur donnent une signature forte au moment exact
            # où les indicateurs passent.
            #
            # On ne cherche ni beat audio, ni forme géométrique particulière.
            # On mesure seulement le changement temporel dans cette porte.
            # --------------------------------------------------------
            frames = [
                data[i*frame_size:(i+1)*frame_size]
                for i in range(frame_count)
            ]

            # Masque horizontal dur : la moitié droite peut être rendue
            # totalement invisible au moteur d'analyse.
            visible_x1 = out_w
            if ignore_right_half:
                visible_x1 = max(1, int(round(out_w * 0.50)))

            if ignore_right_half:
                # La frontière centrale devient la porte de déclenchement.
                target_center = max(0, visible_x1 - 1)
                target_half = max(1, int(round(out_w * 0.006)))
            else:
                target_center = int(round(out_w * target_x_pct / 100.0))
                target_center = max(0, min(out_w - 1, target_center))
                target_half = max(
                    2,
                    int(round(out_w * target_width_pct / 200.0))
                )

            # L'ancienne recherche automatique de cible reste disponible,
            # mais est désactivée par défaut. Si demandée, on choisit la
            # position la plus "impulsionnelle" entre 35 et 60 % de largeur.
            if auto_target and not ignore_right_half:
                self.analysis_queue.put((
                    "progress",
                    (45.0, "Recherche de la meilleure porte visuelle…")
                ))

                search_x0 = int(out_w * 0.20)
                search_x1 = min(
                    visible_x1,
                    int(out_w * 0.50)
                )
                step_x = max(2, int(out_w * 0.01))
                best_x = target_center
                best_quality = -1.0

                # Sous-échantillonnage temporel pour cette recherche seulement.
                sample_step = max(1, frame_count // 900)

                for xc in range(search_x0, search_x1, step_x):
                    half = max(2, target_half)
                    x0 = max(0, xc - half)
                    x1 = min(out_w, xc + half + 1)
                    vals = []
                    prev = None

                    for fi in range(0, frame_count, sample_step):
                        fr = frames[fi]
                        strip = np.frombuffer(
                            b''.join(
                                fr[y*out_w + x0:y*out_w + x1]
                                for y in range(out_h)
                            ),
                            dtype=np.uint8
                        )

                        if prev is not None and strip.size == prev.size:
                            vals.append(float(np.mean(
                                np.abs(strip.astype(np.int16) - prev.astype(np.int16))
                            )))
                        prev = strip

                    if len(vals) >= 10:
                        a = np.asarray(vals, dtype=float)
                        med = float(np.median(a))
                        p90 = float(np.percentile(a, 90))
                        p99 = float(np.percentile(a, 99))
                        # Une bonne porte présente des pics nets au-dessus du bruit.
                        quality = (p90 - med) + 0.35 * (p99 - p90)
                        # léger biais vers la région apprise ~45 %
                        quality *= 1.0 - 0.12 * abs((xc / out_w) - 0.45)

                        if quality > best_quality:
                            best_quality = quality
                            best_x = xc

                target_center = best_x

            if ignore_right_half:
                # Seulement une très fine bande immédiatement à gauche du centre.
                target_x1 = visible_x1
                target_x0 = max(0, target_x1 - max(2, target_half * 2))
                target_pct_found = 50.0
            else:
                target_x0 = max(0, target_center - target_half)
                target_x1 = min(
                    out_w,
                    target_center + target_half + 1
                )
                target_pct_found = 100.0 * target_center / max(1, out_w)

            self.analysis_queue.put((
                "progress",
                (
                    50.0,
                    f"Porte visuelle X {target_pct_found:.1f}% — "
                    + ("porte centrale X=50 % — moitié droite invisible — " if ignore_right_half else "")
                    + "mesure des franchissements…"
                )
            ))

            # Signal image-par-image dans la porte.
            scores = []
            prev = frames[0]
            progress_step = max(1, frame_count // 100)

            for i in range(1, frame_count):
                if self.analysis_cancel_requested:
                    self.analysis_queue.put(("cancelled", None))
                    return

                cur = frames[i]
                diff_sum = 0
                count = 0

                # Toute la hauteur de la ROI est déjà limitée à la piste basse.
                for y in range(out_h):
                    base = y * out_w
                    for x in range(target_x0, target_x1):
                        j = base + x
                        diff_sum += abs(int(cur[j]) - int(prev[j]))
                        count += 1

                scores.append(diff_sum / max(1, count))
                prev = cur

                if i % progress_step == 0 or i == frame_count - 1:
                    frac = i / max(1, frame_count - 1)
                    pct = 50.0 + frac * 37.0
                    self.analysis_queue.put((
                        "progress",
                        (
                            pct,
                            f"Mesure porte {target_pct_found:.1f}% — "
                            f"image {i}/{frame_count-1}"
                        )
                    ))

            if not scores:
                raise RuntimeError("Aucun signal temporel détecté dans la piste.")

            score_array = np.asarray(scores, dtype=float)

            # Le percentile 67,5 a été obtenu sur les deux références Flux :
            # Part 1 ~87 % F1 sur le segment test,
            # Part 2 ~96 % F1 sur le segment test (tolérance ±100 ms).
            # Le champ de sensibilité ajuste légèrement ce percentile.
            sensitivity = float(min_changed_pct)
            percentile = 67.5 + (sensitivity - 3.0) * 2.5
            percentile = max(52.5, min(87.5, percentile))
            threshold = float(np.percentile(score_array, percentile))

            self.analysis_queue.put((
                "progress",
                (
                    90.0,
                    f"Recherche des maxima locaux — seuil P{percentile:.1f}…"
                )
            ))

            min_frames = max(1, int(round((min_interval / 1000.0) * fps)))

            # Détecteur temporel stable : maxima locaux dans la porte centrale.
            candidates = []
            for i in range(1, len(score_array) - 1):
                s = score_array[i]
                if (
                    s >= threshold
                    and s >= score_array[i - 1]
                    and s > score_array[i + 1]
                ):
                    candidates.append(i)

            selected = []
            for idx in candidates:
                if not selected:
                    selected.append(idx)
                elif idx - selected[-1] >= min_frames:
                    selected.append(idx)
                elif score_array[idx] > score_array[selected[-1]]:
                    selected[-1] = idx

            raw_events = [
                int(round((idx + 1) * 1000.0 / fps))
                for idx in selected
            ]
            raw_event_count = len(raw_events)

            # ----------------------------------------------------
            # v4.1 - nettoyage des doubles détections
            # ----------------------------------------------------
            # Plusieurs maxima très proches peuvent appartenir au même
            # indicateur. On regroupe les événements qui tombent dans une
            # même fenêtre temporelle et on garde le pic le plus fort.
            events = []
            if raw_events:
                groups = []
                current_group = [selected[0]]

                for idx in selected[1:]:
                    prev_idx = current_group[-1]
                    dt_ms = int(round((idx - prev_idx) * 1000.0 / fps))

                    if dt_ms < merge_window_ms:
                        current_group.append(idx)
                    else:
                        groups.append(current_group)
                        current_group = [idx]

                if current_group:
                    groups.append(current_group)

                cleaned_indices = []
                for group in groups:
                    best_idx = max(group, key=lambda k: score_array[k])
                    cleaned_indices.append(best_idx)

                events = [
                    int(round((idx + 1) * 1000.0 / fps))
                    for idx in cleaned_indices
                ]

            merged_event_count = len(events)

            # v4.14.6 - conserver la force visuelle de chaque passage.
            # Jusqu'ici, toute cette information était perdue après conversion
            # en simples timestamps, ce qui empêchait de reconnaître une vraie
            # transition de pattern.
            event_visual_strength = {}
            if raw_events:
                for idx in cleaned_indices:
                    t = int(round((idx + 1) * 1000.0 / fps))
                    event_visual_strength[t] = float(score_array[idx])

            # ----------------------------------------------------
            # v4.11.3 - verrouillage du vrai début de la piste
            # ----------------------------------------------------
            # Un écran noir ou une transition d'introduction ne doit jamais
            # produire les premiers va-et-vient. On valide le départ seulement
            # quand la porte de détection est réellement visible ET qu'une
            # activité soutenue commence. Le comptage démarre alors à ce point.
            if auto_start and events:
                gate_brightness = []
                for fr in frames:
                    total = 0
                    count_px = 0
                    for y in range(out_h):
                        base = y * out_w
                        for x in range(target_x0, target_x1):
                            total += int(fr[base + x])
                            count_px += 1
                    gate_brightness.append(total / max(1, count_px))

                # Seuil volontairement bas : noir = rejeté, piste même sombre = acceptée.
                visible_threshold = max(4.0, float(np.percentile(
                    np.asarray(gate_brightness, dtype=float), 35
                )) * 0.35)

                valid_start_index = None
                required = max(3, int(start_min_events))
                for i, t0 in enumerate(events):
                    limit = t0 + int(start_window_ms)
                    cluster = [t for t in events[i:] if t <= limit]
                    if len(cluster) < required:
                        continue

                    fi = max(0, min(frame_count - 1, int(round(t0 * fps / 1000.0))))
                    look_frames = max(1, int(round(min(700.0, start_window_ms) * fps / 1000.0)))
                    b0 = max(0, fi - look_frames // 4)
                    b1 = min(frame_count, fi + look_frames)
                    local_brightness = (
                        float(np.mean(gate_brightness[b0:b1]))
                        if b1 > b0 else gate_brightness[fi]
                    )
                    if local_brightness >= visible_threshold:
                        valid_start_index = i
                        break

                if valid_start_index is not None:
                    events = events[valid_start_index:]
                else:
                    events = []

                # v4.11.7 - test audio pur en renfort du visuel.
                # IMPORTANT : aucune compensation fixe de +3 pas n'est appliquée.
                # Le visuel fournit les événements. L'audio ne les recale que
                # lorsqu'au moins 8 correspondances fiables sont trouvées.
                audio_beats = []
                audio_offset_ms = 0.0
                audio_matches = 0
                audio_phase_steps = 0
                audio_phase_score = 0.0
                audio_correction_applied = False
                try:
                    video_for_audio = getattr(self, "current_video", None)

                    audio_beats = detect_audio_beats(
                        video_for_audio,
                        min_gap_ms=160,
                        sensitivity=4.2
                    )
                    audio_phase_steps, audio_offset_ms, audio_matches, audio_phase_score = (
                        estimate_visual_audio_phase(
                            events,
                            audio_beats,
                            max_steps=8,
                            tolerance_ms=85
                        )
                    )
                except Exception:
                    audio_beats = []
                    audio_offset_ms = 0.0
                    audio_matches = 0
                    audio_phase_steps = 0
                    audio_phase_score = 0.0

                if audio_matches >= 8 and audio_phase_steps != 0:
                    events = [
                        max(0, int(round(t + audio_offset_ms)))
                        for t in events
                    ]
                    audio_correction_applied = True

                # Valeurs conservées pour diagnostic et affichage futur.
                self.visual_audio_beats = audio_beats
                self.visual_audio_offset_ms = audio_offset_ms
                self.visual_audio_matches = audio_matches
                self.visual_audio_phase_steps = audio_phase_steps
                self.visual_audio_phase_score = audio_phase_score
                self.visual_audio_correction_applied = audio_correction_applied

            # ----------------------------------------------------
            # v4.5 - récupération prudente des indicateurs manqués
            # ----------------------------------------------------
            # Ne modifie PAS le détecteur principal. On examine seulement les
            # trous anormalement longs entre deux événements déjà fiables.
            # Dans chaque trou, au maximum un maximum local sous le seuil
            # principal peut être réintégré.
            recovered_count = 0

            if recovery_enabled and len(events) >= 2:
                recovered = list(events)
                lower_threshold = threshold * recovery_factor
                guard_ms = max(110, min(220, merge_window_ms))

                additions = []
                for left_ms, right_ms in zip(events, events[1:]):
                    gap = right_ms - left_ms
                    if gap < recovery_gap_ms:
                        continue

                    # Convertit la zone temporelle en indices du signal.
                    lo_ms = left_ms + guard_ms
                    hi_ms = right_ms - guard_ms
                    if hi_ms <= lo_ms:
                        continue

                    lo_idx = max(1, int(round(lo_ms * fps / 1000.0)) - 1)
                    hi_idx = min(
                        len(score_array) - 2,
                        int(round(hi_ms * fps / 1000.0)) - 1
                    )
                    if hi_idx <= lo_idx:
                        continue

                    best_idx = None
                    best_score = lower_threshold

                    for idx in range(lo_idx, hi_idx + 1):
                        s = score_array[idx]
                        if (
                            s >= lower_threshold
                            and s >= score_array[idx - 1]
                            and s > score_array[idx + 1]
                            and s > best_score
                        ):
                            best_idx = idx
                            best_score = s

                    if best_idx is not None:
                        t = int(round((best_idx + 1) * 1000.0 / fps))

                        # Un seul événement récupéré par grand trou, et jamais
                        # trop près d'un événement déjà accepté.
                        if all(abs(t - e) >= guard_ms for e in events):
                            additions.append(t)

                if additions:
                    recovered.extend(additions)
                    recovered = sorted(set(recovered))
                    recovered_count = len(recovered) - len(events)
                    events = recovered

            # ----------------------------------------------------
            # v4.11.2 - analyse expérimentale depuis la fin
            # ----------------------------------------------------
            # La détection image reste identique. Seule la reconstruction
            # temporelle est inversée : les intervalles détectés sont
            # réappliqués en partant du dernier passage fiable. Cela permet
            # de tester si le décalage observé vient de l'ancrage au début.
            if reverse_from_end and len(events) >= 2:
                intervals = [
                    max(1, events[i + 1] - events[i])
                    for i in range(len(events) - 1)
                ]
                anchor_end = events[-1]
                rebuilt = [anchor_end]
                cursor = anchor_end
                for dt in reversed(intervals):
                    cursor -= dt
                    rebuilt.append(cursor)
                events = sorted(max(0, int(t)) for t in rebuilt)

            started_event_count = len(events)
            effective_changed = threshold

            # Ajustement fin de synchronisation. Les valeurs sont bornées
            # à la durée de la vidéo.
            if sync_offset_ms:
                max_ms = int(duration_s * 1000.0) if duration_s > 0 else 2**31 - 1
                events = [
                    max(0, min(max_ms, int(t + sync_offset_ms)))
                    for t in events
                ]

            # Associer la force visuelle au timestamp final le plus proche.
            # La segmentation pourra ainsi distinguer une transition de motif
            # d'un simple changement de vitesse.
            final_strength = {}
            if event_visual_strength and events:
                original_items = sorted(event_visual_strength.items())
                for t in events:
                    # Compense les corrections temporelles globales déjà appliquées.
                    source_t = int(t - sync_offset_ms)
                    nearest_t, strength = min(
                        original_items, key=lambda kv: abs(kv[0] - source_t)
                    )
                    if abs(nearest_t - source_t) <= max(500, merge_window_ms * 2):
                        final_strength[int(t)] = float(strength)

            self.visual_event_strength = final_strength

            self.analysis_queue.put((
                "done",
                (
                    events, line_y, actual_band, effective_changed,
                    100.0 * target_center / max(1, out_w),
                    raw_event_count, merged_event_count, started_event_count,
                    recovered_count,
                    len(getattr(self, "visual_audio_beats", []) or []),
                    int(getattr(self, "visual_audio_matches", 0) or 0),
                    float(getattr(self, "visual_audio_offset_ms", 0.0) or 0.0),
                    int(getattr(self, "visual_audio_phase_steps", 0) or 0),
                    bool(getattr(self, "visual_audio_correction_applied", False))
                )
            ))

        except Exception as exc:
            self.analysis_queue.put(("error", str(exc)))
        finally:
            if raw_path:
                try:
                    os.unlink(raw_path)
                except Exception:
                    pass

    def detect_visual_line_events(self):
        """
        Lance l'analyse visuelle dans un thread séparé.
        La fenêtre reste réactive et la progression est affichée en temps réel.
        """
        if self.analysis_running:
            return

        if not self.current_video:
            self.choose_video()
            if not self.current_video:
                return

        try:
            line_y = max(1.0, min(99.0, float(self.visual_line_y_var.get())))
            band_pct = max(1.0, min(30.0, float(self.visual_line_band_var.get())))
            pixel_threshold = max(1.0, min(255.0, float(self.visual_line_threshold_var.get())))
            min_changed_pct = max(0.1, min(100.0, float(self.visual_line_min_changed_var.get())))
            min_interval = max(50, int(self.visual_line_interval_var.get()))
            fps = max(4.0, min(60.0, float(self.visual_line_fps_var.get())))
            sync_offset_ms = int(self.visual_sync_offset_var.get())
            target_x_pct = max(0.0, min(100.0, float(self.visual_target_x_var.get())))
            target_width_pct = max(0.5, min(20.0, float(self.visual_target_width_var.get())))
            auto_target = bool(self.visual_auto_target_var.get())
            ignore_right_half = bool(self.visual_ignore_right_half_var.get())
            reverse_from_end = bool(self.visual_reverse_analysis_var.get())

            if ignore_right_half:
                self.visual_target_x_var.set("50.0")
                target_x_pct = 50.0
                auto_target = False

            merge_window_ms = max(80, min(1000, int(self.visual_merge_window_var.get())))
            auto_start = bool(self.visual_auto_start_var.get())
            start_window_ms = max(1000, min(30000, int(self.visual_start_window_var.get())))
            start_min_events = max(2, min(100, int(self.visual_start_min_events_var.get())))
            recovery_enabled = bool(self.visual_recovery_var.get())
            recovery_gap_ms = max(350, min(3000, int(self.visual_recovery_gap_var.get())))
            recovery_factor = max(0.55, min(0.98, float(self.visual_recovery_factor_var.get())))
        except Exception as exc:
            messagebox.showerror("CockHero Visuel", f"Paramètre invalide : {exc}")
            return

        self.analysis_cancel_requested = False
        self.analysis_progress_var.set(0.0)
        try:
            self.visual_analysis_percent_var.set("0.0 %")
            self.visual_analysis_info_var.set(
                "Analyse : 0.0 % — Préparation…"
            )
            self.visual_audio_status_var.set(
                "AUDIO : ANALYSE… | Beats : 0 | Correspondances : 0 | Offset : +0 ms | Correction : NON"
            )
        except Exception:
            pass
        self.visual_line_status_var.set(
            "Analyse en cours depuis la fin…"
            if reverse_from_end else
            "Analyse en cours…"
        )
        self._set_analysis_ui(True, "Préparation de l'analyse…", 0.0)

        worker = threading.Thread(
            target=self._visual_analysis_worker,
            args=(
                self.current_video,
                line_y,
                band_pct,
                pixel_threshold,
                min_changed_pct,
                min_interval,
                fps,
                sync_offset_ms,
                target_x_pct,
                target_width_pct,
                auto_target,
                merge_window_ms,
                auto_start,
                start_window_ms,
                start_min_events,
                recovery_enabled,
                recovery_gap_ms,
                recovery_factor,
                ignore_right_half,
                reverse_from_end
            ),
            daemon=True
        )
        worker.start()
        self.after(50, self._poll_visual_analysis_queue)

    def adjust_visual_sync(self, delta_ms):
        """
        Décale immédiatement les événements déjà détectés sans relancer
        toute l'analyse vidéo. Permet de caler rapidement le hit à l'œil.
        """
        try:
            current = int(self.visual_sync_offset_var.get())
        except Exception:
            current = 0

        new_value = current + int(delta_ms)
        self.visual_sync_offset_var.set(str(new_value))
        try:
            self.visual_sync_live_var.set(f"Offset actuel : {new_value:+d} ms")
        except Exception:
            pass

        if self.detected_line_times_ms:
            self.detected_line_times_ms = [
                max(0, int(t + delta_ms))
                for t in self.detected_line_times_ms
            ]
            self.visual_line_status_var.set(
                f"{len(self.detected_line_times_ms)} pics détectés | offset {new_value} ms"
            )
            self.status_var.set(
                f"Synchronisation visuelle ajustée à {new_value:+d} ms — "
                "régénère l'aperçu CockHero."
            )
            self.draw_all()
        else:
            self.status_var.set(
                f"Offset visuel réglé à {new_value:+d} ms. Lance l'analyse."
            )

        self._save_app_config()

    def _final_clean_visual_events(self, events):
        """
        Nettoyage final appliqué juste avant la génération du funscript.
        Il ne dépend pas du moteur de détection et garantit que la sauvegarde
        utilise bien les événements nettoyés.
        """
        clean = sorted({max(0, int(t)) for t in events})
        raw_count = len(clean)

        try:
            merge_ms = max(80, min(1000, int(self.visual_merge_window_var.get())))
            auto_start = bool(self.visual_auto_start_var.get())
            start_window_ms = max(1000, min(30000, int(self.visual_start_window_var.get())))
            start_min_events = max(2, min(100, int(self.visual_start_min_events_var.get())))
        except Exception:
            merge_ms = 170
            auto_start = True
            start_window_ms = 5000
            start_min_events = 8

        # Fusion robuste : dans chaque groupe d'événements rapprochés,
        # conserve le temps médian plutôt que le premier ou le dernier.
        merged = []
        if clean:
            group = [clean[0]]
            for t in clean[1:]:
                # Important : compare au début du groupe pour éviter un effet
                # "chaîne" qui fusionnerait toute une longue séquence.
                if t - group[0] < merge_ms:
                    group.append(t)
                else:
                    merged.append(group[len(group) // 2])
                    group = [t]
            if group:
                merged.append(group[len(group) // 2])

        merged_count = len(merged)

        # Détection du vrai début : cherche une fenêtre avec activité soutenue.
        started = merged
        if auto_start and len(merged) >= start_min_events:
            start_idx = None
            j = 0
            for i, t0 in enumerate(merged):
                while j < len(merged) and merged[j] <= t0 + start_window_ms:
                    j += 1
                count = j - i
                if count >= start_min_events:
                    start_idx = i
                    break

            if start_idx is not None and start_idx > 0:
                started = merged[start_idx:]

        return started, raw_count, merged_count, len(started)

    def _trim_false_leading_event_groups(
        self,
        events,
        min_duration_ms=6500,
        min_events=18,
    ):
        """
        Supprime les petits groupes parasites situés avant le premier
        épisode réellement soutenu.

        Important : ce nettoyage travaille sur les MARQUEURS avant la
        génération du funscript. Ainsi aucun va-et-vient ni grand raccord
        ne peut être créé dans la zone rejetée.
        """
        groups = self._segment_visual_episodes(events)
        if not groups:
            return [], 0, None

        # Chercher le premier groupe suffisamment soutenu.
        valid_index = None

        for i, group in enumerate(groups):
            if not group:
                continue

            start = int(group[0])
            end = int(group[-1])
            duration = max(0, end - start)
            count = len(group)

            if duration >= int(min_duration_ms) and count >= int(min_events):
                valid_index = i
                break

        # Si aucun groupe ne passe le seuil, ne rien supprimer.
        if valid_index is None or valid_index == 0:
            return sorted(int(t) for t in events), 0, (
                int(groups[0][0]) if groups and groups[0] else None
            )

        first_valid = int(groups[valid_index][0])

        cleaned = [
            int(t) for t in events
            if int(t) >= first_valid
        ]

        removed = len(events) - len(cleaned)

        return cleaned, removed, first_valid

    def _trim_episode_false_starts(self, group):
        """
        Nettoie les limites d'un épisode sans toucher à la synchronisation.

        Un début/une fin n'est conservé que lorsqu'il appartient à une
        séquence cohérente d'au moins 3 passages. Les détections isolées
        placées avant ou après le rythme réel sont supprimées.
        """
        import statistics

        g = sorted(int(t) for t in group)
        if len(g) < 5:
            return g

        intervals = [g[i + 1] - g[i] for i in range(len(g) - 1)]
        positive = [d for d in intervals if d > 0]
        if not positive:
            return g

        # Le tempo de référence est calculé avec la moitié la plus rapide
        # des intervalles afin qu'un faux départ ou une pause ne l'étire pas.
        ordered = sorted(positive)
        sample = ordered[:max(3, (len(ordered) + 1) // 2)]
        tempo = float(statistics.median(sample))

        # Tolérance volontairement large : on cherche seulement à éliminer
        # les événements isolés aux bords, pas les variations de rythme.
        coherent_max = max(350.0, min(1800.0, tempo * 2.75))

        def pair_ok(a, b):
            d = b - a
            return 0 < d <= coherent_max

        # Premier endroit où 3 passages consécutifs forment une vraie séquence.
        first = 0
        found_start = False
        for i in range(0, len(g) - 2):
            if pair_ok(g[i], g[i + 1]) and pair_ok(g[i + 1], g[i + 2]):
                first = i
                found_start = True
                break

        # Dernier endroit où 3 passages consécutifs forment une vraie séquence.
        last = len(g) - 1
        found_end = False
        for i in range(len(g) - 1, 1, -1):
            if pair_ok(g[i - 2], g[i - 1]) and pair_ok(g[i - 1], g[i]):
                last = i
                found_end = True
                break

        if not found_start or not found_end or last < first:
            return g

        return g[first:last + 1]

    def _trim_episode_with_audio(self, group):
        """
        Nettoyage prudent des limites d'épisode.

        v4.14.6 :
        - aucune coupure simplement parce que le tempo change ;
        - rapide -> lent et lent -> rapide restent dans le même épisode ;
        - l'audio ne peut jamais supprimer à lui seul une longue portion ;
        - on ne retire qu'une petite queue terminale clairement résiduelle ;
        - sécurité stricte contre les suppressions de plusieurs secondes/minutes.
        """
        import bisect

        g = sorted(int(t) for t in group)
        if len(g) < 8:
            return g

        beats = sorted(int(t) for t in (getattr(self, "visual_audio_beats", []) or []))
        matches_total = int(getattr(self, "visual_audio_matches", 0) or 0)
        if len(beats) < 3 or matches_total < 8:
            return g

        tolerance_ms = 120

        def audio_match(t):
            p = bisect.bisect_left(beats, t)
            return any(
                0 <= j < len(beats) and abs(beats[j] - t) <= tolerance_ms
                for j in (p - 1, p)
            )

        matched = [audio_match(t) for t in g]

        # Chercher uniquement une petite queue terminale sans soutien audio.
        # On remonte depuis la fin jusqu'au dernier groupe de confirmations.
        last_supported = None
        for i in range(len(g) - 1, -1, -1):
            lo = max(0, i - 4)
            if sum(matched[lo:i + 1]) >= 3:
                last_supported = i
                break

        if last_supported is None or last_supported >= len(g) - 1:
            return g

        tail_ms = g[-1] - g[last_supported]
        tail_points = len(g) - 1 - last_supported

        # GARDE-FOU : ce filtre n'a jamais le droit d'effacer une longue zone.
        # Une queue doit être courte et minoritaire pour être supprimée.
        if tail_ms > 8000:
            return g
        if tail_points > max(24, int(len(g) * 0.12)):
            return g

        # Il faut aussi une absence audio réellement soutenue dans la queue.
        tail = matched[last_supported + 1:]
        if len(tail) < 4:
            return g
        if sum(tail) > max(1, int(len(tail) * 0.20)):
            return g

        kept = g[:last_supported + 1]
        return kept if len(kept) >= 6 else g

    def _split_episode_by_visual_structure(self, group):
        """
        Découpe seulement lorsqu'une vraie transition VISUELLE est observée.

        Le tempo n'est pas le déclencheur. On compare la force des passages
        avant/après une jonction et on exige en plus une petite cassure
        temporelle. Les marqueurs sont conservés : on crée deux épisodes.
        """
        import statistics

        g = sorted(int(t) for t in group)
        strengths = getattr(self, "visual_event_strength", {}) or {}
        if len(g) < 24 or len(strengths) < 12:
            return [g]

        vals = []
        for t in g:
            # force du passage le plus proche
            candidates = [(abs(int(k)-t), float(v)) for k,v in strengths.items()
                          if abs(int(k)-t) <= 500]
            vals.append(min(candidates)[1] if candidates else None)

        WIN = 6
        cuts = []
        for i in range(WIN, len(g)-WIN):
            left = [x for x in vals[i-WIN:i] if x is not None]
            right = [x for x in vals[i:i+WIN] if x is not None]
            if len(left) < 4 or len(right) < 4:
                continue

            ml = float(statistics.median(left))
            mr = float(statistics.median(right))
            if min(ml,mr) <= 0:
                continue
            strength_ratio = max(ml,mr)/min(ml,mr)

            # Une transition doit être visuellement nette.
            if strength_ratio < 1.55:
                continue

            # Et présenter une jonction temporelle identifiable, sans exiger
            # que le nouveau tempo soit différent.
            before_dt = [g[j]-g[j-1] for j in range(max(1,i-5), i)]
            if not before_dt:
                continue
            tempo = float(statistics.median(before_dt))
            bridge = g[i]-g[i-1]
            if bridge < max(tempo*1.30, tempo+90.0):
                continue

            cuts.append(i)

        if not cuts:
            return [g]

        # Éviter plusieurs coupures autour de la même transition.
        selected=[]
        for i in cuts:
            if i < 10 or len(g)-i < 10:
                continue
            if not selected or i-selected[-1] >= 12:
                selected.append(i)

        if not selected:
            return [g]

        parts=[]
        s=0
        for i in selected:
            parts.append(g[s:i])
            s=i
        parts.append(g[s:])

        if any(len(p)<10 for p in parts):
            return [g]
        if sum(map(len,parts)) != len(g):
            return [g]
        return parts

    def _find_start_candidates_readonly(self, episodes):
        """
        v4.14.6 — analyse des débuts en lecture seule.

        Retourne des candidats de début sans supprimer, déplacer, fusionner
        ou modifier un seul événement détecté ni un seul épisode.
        """
        candidates = []
        for i, ep in enumerate(episodes):
            if not ep:
                continue
            candidates.append({
                "episode_index": i,
                "start_ms": int(ep[0]),
                "end_ms": int(ep[-1]),
                "passages": len(ep),
            })
        return candidates

    def _diagnose_short_post_boundary_block(self, previous, current):
        """
        v4.14.6 — diagnostic seulement, aucune suppression.

        Après une frontière déjà créée par le moteur, signale une reprise
        courte (<20 passages) séparée par un vrai plat. Cette fonction ne
        modifie jamais les points ni les épisodes.
        """
        import statistics
        if not previous or not current:
            return False
        if len(previous) < 20 or len(current) >= 20:
            return False

        recent = [
            previous[j] - previous[j - 1]
            for j in range(max(1, len(previous) - 12), len(previous))
        ]
        recent = [d for d in recent if 40 <= d <= 2500]
        if len(recent) < 5:
            return False

        step = float(statistics.median(recent))
        gap = int(current[0]) - int(previous[-1])
        return gap >= max(step * 2.5, step + 300.0)

    def _split_on_terminal_long_diagonal(self, group):
        """Après >=20 pas normaux, une grande diagonale devient une frontière dure."""
        import statistics
        g=sorted(int(t) for t in group)
        if len(g)<24:
            return [g]
        parts=[]; start=0; i=20
        while i<len(g):
            recent=[g[j]-g[j-1] for j in range(max(start+1,i-20),i)
                    if 40 <= g[j]-g[j-1] <= 2500]
            if len(recent)>=18:
                step=float(statistics.median(recent))
                gap=g[i]-g[i-1]
                if gap >= step*2.50 and gap >= step+220.0 and i-start>=21 and len(g)-i>=3:
                    parts.append(g[start:i]); start=i; i=start+20; continue
            i+=1
        parts.append(g[start:])
        return parts if sum(map(len,parts))==len(g) else [g]

    def _merge_visual_micro_episodes(self, episodes):
        """
        v4.14.6 — fusion après détection.

        La v4.12.9 sait détecter la transition recherchée, mais sur-segmente.
        On conserve donc ses frontières, puis on fusionne seulement les petits
        épisodes voisins lorsque la frontière entre eux n'est pas suffisamment
        forte.

        Important :
        - aucune donnée n'est supprimée ;
        - une frontière forte reste intacte ;
        - les micro-fragments produits par de petites fluctuations sont réunis.
        """
        import statistics

        if not episodes or len(episodes) < 2:
            return episodes

        strengths = getattr(self, "visual_event_strength", {}) or {}
        strength_items = sorted((int(k), float(v)) for k, v in strengths.items())

        def nearest_strength(t):
            if not strength_items:
                return None
            # Recherche simple et robuste ; le nombre de points par frontière
            # est faible par rapport au nombre total de marqueurs.
            k, v = min(strength_items, key=lambda kv: abs(kv[0] - int(t)))
            return v if abs(k - int(t)) <= 500 else None

        def median_strength(part, side):
            if not part:
                return None
            sample = part[-8:] if side == "left" else part[:8]
            vals = [nearest_strength(t) for t in sample]
            vals = [v for v in vals if v is not None]
            return float(statistics.median(vals)) if len(vals) >= 4 else None

        def median_tempo(part, side):
            if len(part) < 4:
                return None
            sample = part[-10:] if side == "left" else part[:10]
            d = [sample[i] - sample[i-1] for i in range(1, len(sample))]
            d = [x for x in d if 40 <= x <= 2500]
            return float(statistics.median(d)) if len(d) >= 3 else None

        def boundary_is_strong(left, right):
            if not left or not right:
                return False
            hard = getattr(self, "visual_hard_episode_boundaries", set()) or set()
            if (int(left[-1]), int(right[0])) in hard:
                return True

            gap = int(right[0]) - int(left[-1])
            tl = median_tempo(left, "left")
            tr = median_tempo(right, "right")
            sl = median_strength(left, "left")
            sr = median_strength(right, "right")

            tempo_ref = min(x for x in (tl, tr) if x is not None) if (tl is not None or tr is not None) else 0.0
            gap_ratio = (gap / max(1.0, tempo_ref)) if tempo_ref else 1.0

            visual_ratio = 1.0
            if sl is not None and sr is not None and min(sl, sr) > 0:
                visual_ratio = max(sl, sr) / min(sl, sr)

            # Une frontière détectée par v4.12.9 est préservée si elle présente
            # simultanément une vraie cassure temporelle et une vraie différence
            # de structure visuelle. C'est précisément le type de frontière que
            # nous cherchons autour de 8:25.
            return gap_ratio >= 1.28 and visual_ratio >= 1.45

        merged = [list(episodes[0])]

        for current in episodes[1:]:
            current = list(current)
            previous = merged[-1]

            if boundary_is_strong(previous, current):
                merged.append(current)
                continue

            # Frontière faible : fusion. Cela réduit les dizaines/centaines de
            # micro-épisodes de v4.12.9 sans toucher aux frontières fortes.
            previous.extend(current)
            previous.sort()

        # Deuxième passe : éliminer les micro-épisodes isolés restants
        # (< 2 s ou < 8 passages), sauf s'ils sont encadrés par deux frontières
        # fortes. On fusionne vers le voisin temporel le plus naturel.
        changed = True
        while changed and len(merged) > 1:
            changed = False
            for i, part in enumerate(list(merged)):
                duration = (part[-1] - part[0]) if len(part) >= 2 else 0
                if len(part) >= 8 and duration >= 2000:
                    continue

                strong_left = i > 0 and boundary_is_strong(merged[i-1], part)
                strong_right = i + 1 < len(merged) and boundary_is_strong(part, merged[i+1])

                if strong_left and strong_right:
                    continue

                if i == 0:
                    merged[1] = sorted(part + merged[1])
                    del merged[0]
                elif i == len(merged) - 1:
                    merged[i-1] = sorted(merged[i-1] + part)
                    del merged[i]
                else:
                    gap_left = part[0] - merged[i-1][-1]
                    gap_right = merged[i+1][0] - part[-1]
                    if gap_left <= gap_right:
                        merged[i-1] = sorted(merged[i-1] + part)
                        del merged[i]
                    else:
                        merged[i+1] = sorted(part + merged[i+1])
                        del merged[i]
                changed = True
                break

        return merged

    def _segment_visual_episodes(self, events):
        """
        Découpe les marqueurs en épisodes séparés par une pause > seuil,
        puis retire automatiquement les faux départs/fins de CHAQUE épisode.
        """
        events = sorted(int(t) for t in events)
        if not events:
            return []

        try:
            gap_ms = max(500, int(float(self.visual_episode_gap_var.get())))
        except Exception:
            gap_ms = 3000

        raw_episodes = []
        current = [events[0]]

        for t in events[1:]:
            if t - current[-1] > gap_ms:
                raw_episodes.append(current)
                current = [t]
            else:
                current.append(t)

        if current:
            raw_episodes.append(current)

        episodes = []
        for group in raw_episodes:
            # 1) Nettoyage géométrique des faux départs/fins.
            cleaned = self._trim_episode_false_starts(group)

            # 2) Lorsque l'audio est fiable, il confirme les limites réelles.
            #    Cela supprime notamment les marqueurs visuels qui continuent
            #    après la fin réelle d'un épisode.
            cleaned = self._trim_episode_with_audio(cleaned)

            if cleaned:
                # v4.14.6 : le détecteur fournit maintenant la structure
                # visuelle des passages. Une vraie transition crée un nouvel
                # épisode sans supprimer les marqueurs qui suivent.
                episodes.extend(self._split_episode_by_visual_structure(cleaned))

        # v4.14.6 : une grande diagonale après >=20 pas normaux est
        # une frontière dure. Elle ne peut jamais être refusionnée.
        split_episodes = []
        hard_boundaries = set()
        for ep in episodes:
            parts = self._split_on_terminal_long_diagonal(ep)
            for j in range(len(parts)-1):
                if parts[j] and parts[j+1]:
                    hard_boundaries.add((int(parts[j][-1]), int(parts[j+1][0])))
            split_episodes.extend(parts)

        self.visual_hard_episode_boundaries = hard_boundaries
        episodes = self._merge_visual_micro_episodes(split_episodes)

        # Diagnostic local uniquement : aucune donnée n'est retirée.
        self.visual_short_post_boundary_blocks = []
        for i in range(1, len(episodes)):
            if self._diagnose_short_post_boundary_block(episodes[i-1], episodes[i]):
                self.visual_short_post_boundary_blocks.append(
                    (int(episodes[i][0]), len(episodes[i]))
                )

        # v4.14.6 : analyse des débuts strictement en lecture seule.
        # IMPORTANT : episodes est retourné exactement tel quel.
        self.visual_start_candidates = self._find_start_candidates_readonly(episodes)

        return episodes

    def _default_episode_range(self, index):
        """Progression par paliers ENTRE les épisodes, jamais dans un épisode."""
        high, low = _EPISODE_AMPLITUDES[min(max(0, int(index)), len(_EPISODE_AMPLITUDES)-1)]
        return int(high), int(low)

    def _restore_visual_episodes_from_metadata(self, data):
        """Restaure exactement les épisodes enregistrés dans le .funscript."""
        try:
            metadata = data.get("metadata", {}) if isinstance(data, dict) else {}
            chmeta = metadata.get("cockheroGenerator", {}) if isinstance(metadata, dict) else {}
            saved = chmeta.get("episodes", []) if isinstance(chmeta, dict) else []

            if not isinstance(saved, list) or not saved:
                return False

            markers = list(self.loaded_marker_times_ms or [])
            episodes = []

            for i, raw in enumerate(saved):
                if not isinstance(raw, dict):
                    continue

                start = int(raw.get("start", 0))
                end = int(raw.get("end", start))

                # Reconstitue les événements appartenant à cette zone.
                events = [
                    int(t) for t in markers
                    if start <= int(t) <= end
                ]

                high = int(raw.get("high", 100))
                low = int(raw.get("low", 25))

                episodes.append({
                    "index": i + 1,
                    "name": raw.get("name", f"Épisode {i+1}"),
                    "start": start,
                    "end": end,
                    "events": events,
                    "count": int(raw.get("count", len(events))),
                    "high": high,
                    "low": low,
                    "high_deg": int(raw.get("highDeg", round(high * 1.8))),
                    "low_deg": int(raw.get("lowDeg", round(low * 1.8))),
                    "enabled": bool(raw.get("enabled", True)),
                    "invert": bool(raw.get("invert", False)),
                    "start_side": raw.get("startSide", "high"),
                    "offset_ms": int(raw.get("offsetMs", 0)),
                    "pause_mode": raw.get("pauseMode", "hold"),
                    "pattern_index": _fixed_episode_pattern_index(raw.get("patternIndex", 0)),
                    "color": raw.get(
                        "color",
                        self.visual_episode_palette[i % len(self.visual_episode_palette)]
                    ),
                })

            if not episodes:
                return False

            self.visual_episodes = episodes

            try:
                gap = chmeta.get("episodeGapMs")
                if gap is not None:
                    self.visual_episode_gap_var.set(str(int(gap)))
            except Exception:
                pass

            self._refresh_visual_episode_tree()
            return True

        except Exception:
            return False

    def rebuild_visual_episodes(self, events=None):
        """Reconstruit le tableau des épisodes depuis les marqueurs actifs."""
        if events is None:
            events = self.detected_line_times_ms

        groups = self._segment_visual_episodes(events)

        old_by_bounds = {}
        for ep in getattr(self, "visual_episodes", []):
            old_by_bounds[(ep["start"], ep["end"])] = ep

        episodes = []
        for i, group in enumerate(groups):
            start = int(group[0])
            end = int(group[-1])

            old = old_by_bounds.get((start, end))
            if old:
                high = old["high"]
                low = old["low"]
            else:
                high, low = self._default_episode_range(i)

            if old:
                name = old.get("name", f"Épisode {i+1}")
                enabled = bool(old.get("enabled", True))
                invert = bool(old.get("invert", False))
                start_side = old.get("start_side", "high")
                offset_ms = int(old.get("offset_ms", 0))
                pause_mode = old.get("pause_mode", "hold")
                pattern_index = _fixed_episode_pattern_index(old.get("pattern_index", 0))
                color = old.get(
                    "color",
                    self.visual_episode_palette[i % len(self.visual_episode_palette)]
                )
            else:
                name = f"Épisode {i+1}"
                enabled = True
                invert = False
                start_side = "high"
                offset_ms = 0
                pause_mode = "hold"
                pattern_index = min(i, len(_EPISODE_AMPLITUDES) - 1)
                color = self.visual_episode_palette[i % len(self.visual_episode_palette)]

            episodes.append({
                "index": i + 1,
                "name": name,
                "start": start,
                "end": end,
                "events": list(group),
                "count": len(group),
                "high": int(high),
                "low": int(low),
                "high_deg": int(round(int(high) * 1.8)),
                "low_deg": int(round(int(low) * 1.8)),
                "enabled": enabled,
                "invert": invert,
                "start_side": start_side,
                "offset_ms": offset_ms,
                "pause_mode": pause_mode,
                "pattern_index": pattern_index,
                "color": color,
            })

        self.visual_episodes = episodes
        self._refresh_visual_episode_tree()
        return episodes

    def _selected_visual_episode_index(self):
        """Retourne l'index 0-based de l'épisode sélectionné."""
        # Priorité au sélecteur visible dans l'onglet Épisode.
        try:
            if hasattr(self, "track_episode_select_var"):
                value = self.track_episode_select_var.get().strip()
                if value:
                    number = int(value.split()[0].replace("E", ""))
                    idx = number - 1
                    if 0 <= idx < len(self.visual_episodes):
                        return idx
        except Exception:
            pass

        # Repli : sélection du Treeview historique.
        try:
            tree = getattr(self, "visual_episode_tree", None)
            if tree is not None:
                sel = tree.selection()
                if sel:
                    idx = int(sel[0]) - 1
                    if 0 <= idx < len(self.visual_episodes):
                        return idx
        except Exception:
            pass

        return None

    def delete_selected_visual_episode(self, ask_confirmation=True):
        """Supprime l'épisode choisi, ses actions et ses marqueurs source."""
        idx = self._selected_visual_episode_index()
        if idx is None:
            messagebox.showinfo(
                "Supprimer un épisode",
                "Sélectionne d'abord un épisode."
            )
            return

        ep = self.visual_episodes[idx]
        name = ep.get("name", f"Épisode {idx + 1}")

        if ask_confirmation:
            ok = messagebox.askyesno(
                "Supprimer un épisode",
                (
                    f"Supprimer {name} (E{idx + 1}) ?\n\n"
                    "Tous les points de cet épisode seront retirés de la trame "
                    "ainsi que ses marqueurs détectés."
                )
            )
            if not ok:
                return

        start = int(ep.get("start", 0))
        end = int(ep.get("end", start))
        offset_ms = int(ep.get("offset_ms", 0))
        shifted_start = max(0, start + offset_ms)
        shifted_end = max(shifted_start, end + offset_ms)

        # 1. Supprimer TOUS les va-et-vient/actions de cet épisode.
        actions = self._editable_actions()
        removed_actions_count = 0

        if actions:
            before_count = len(actions)
            actions[:] = [
                a for a in actions
                if not (
                    shifted_start <= int(a.get("at", -1)) <= shifted_end
                )
            ]
            removed_actions_count = before_count - len(actions)

            # Après la coupure, ne créer aucun raccord artificiel.
            # Le trou devient une séparation visible entre épisodes.
            self._rebuild_episodes_from_action_gaps(1500)
        else:
            pass

        # 2. Supprimer les marqueurs source afin que l'épisode ne réapparaisse
        # pas lors d'une nouvelle génération depuis la même analyse.
        try:
            self.detected_line_times_ms = [
                int(t) for t in self.detected_line_times_ms
                if not (start <= int(t) <= end)
            ]
        except Exception:
            pass

        try:
            self.loaded_marker_times_ms = [
                int(t) for t in self.loaded_marker_times_ms
                if not (start <= int(t) <= end)
            ]
        except Exception:
            pass

        # 3. Retirer et renuméroter les épisodes restants.
        del self.visual_episodes[idx]

        for i, item in enumerate(self.visual_episodes):
            item["index"] = i + 1

        # 4. Rafraîchir les sélecteurs.
        self._refresh_visual_episode_tree()

        try:
            if hasattr(self, "track_episode_combo"):
                values = [
                    f"E{ep2['index']} — {ep2.get('name', 'Épisode ' + str(ep2['index']))}"
                    for ep2 in self.visual_episodes
                ]
                self.track_episode_combo.configure(values=values)
                if values:
                    new_idx = min(idx, len(values) - 1)
                    self.track_episode_select_var.set(values[new_idx])
                    self._track_episode_option_selected()
                else:
                    self.track_episode_select_var.set("")
        except Exception:
            pass

        self.track_edit_selected_index = None
        self._rebuild_episodes_from_action_gaps(1500)
        self.status_var.set(
            f"{name} supprimé — {removed_actions_count} point(s) retiré(s) — "
            f"{len(self.visual_episodes)} épisode(s) restant(s). "
            "Aucun raccord à travers les trous > 1,5 s."
        )
        self.draw_all()

    def _delete_episode_at_context_position(self):
        """Supprime l'épisode situé sous le clic droit de la trame."""
        at, _ = self._context_xy_to_action()
        if at is None:
            return

        chosen = None
        for i, ep in enumerate(self.visual_episodes):
            start = int(ep.get("start", 0)) + int(ep.get("offset_ms", 0))
            end = int(ep.get("end", start)) + int(ep.get("offset_ms", 0))
            if start <= int(at) <= end:
                chosen = i
                break

        if chosen is None:
            messagebox.showinfo(
                "Supprimer un épisode",
                "Aucun épisode sous le pointeur."
            )
            return

        try:
            if hasattr(self, "track_episode_select_var"):
                ep = self.visual_episodes[chosen]
                self.track_episode_select_var.set(
                    f"E{ep['index']} — {ep.get('name', 'Épisode ' + str(ep['index']))}"
                )
        except Exception:
            pass

        self.delete_selected_visual_episode(ask_confirmation=True)

    def _track_episode_option_selected(self, _event=None):
        value = self.track_episode_select_var.get().strip()
        if not value:
            return
        try:
            number = int(value.split()[0].replace("E", ""))
            idx = number - 1
            if 0 <= idx < len(self.visual_episodes):
                ep = self.visual_episodes[idx]
                self.visual_episode_high_var.set(str(ep["high"]))
                self.visual_episode_low_var.set(str(ep["low"]))
                self.visual_episode_high_deg_var.set(
                    str(ep.get("high_deg", int(round(ep["high"] * 1.8))))
                )
                self.visual_episode_low_deg_var.set(
                    str(ep.get("low_deg", int(round(ep["low"] * 1.8))))
                )
                self.visual_episode_name_var.set(
                    ep.get("name", f"Épisode {number}")
                )
                self.visual_episode_enabled_var.set(
                    bool(ep.get("enabled", True))
                )
                self.visual_episode_invert_var.set(
                    bool(ep.get("invert", False))
                )
                self.visual_episode_start_side_var.set(
                    ep.get("start_side", "high")
                )
                self.visual_episode_offset_var.set(
                    str(ep.get("offset_ms", 0))
                )
                self.visual_episode_pause_mode_var.set(
                    ep.get("pause_mode", "hold")
                )
                _pi = _fixed_episode_pattern_index(ep.get("pattern_index", 0))
                self.visual_episode_pattern_var.set(EPISODE_PATTERN_NAMES[_pi])
                try:
                    self.visual_episode_tree.selection_set(str(number))
                    self.visual_episode_tree.focus(str(number))
                except Exception:
                    pass
        except Exception:
            pass

    def _refresh_visual_episode_tree(self):
        tree = getattr(self, "visual_episode_tree", None)
        if tree is None:
            return

        for item in tree.get_children():
            tree.delete(item)

        for ep in self.visual_episodes:
            tag = f"ep_{ep['index']}"
            try:
                tree.tag_configure(
                    tag,
                    foreground=ep["color"]
                )
            except Exception:
                pass

            tree.insert(
                "",
                "end",
                iid=str(ep["index"]),
                values=(
                    ep["index"],
                    ep.get("name", f"Épisode {ep['index']}"),
                    self._format_time(ep["start"]),
                    self._format_time(ep["end"]),
                    ep["count"],
                    ep["high"],
                    ep["low"],
                    ep.get("high_deg", int(round(ep["high"] * 1.8))),
                    ep.get("low_deg", int(round(ep["low"] * 1.8))),
                    f"{ep.get('offset_ms', 0):+d}",
                    (
                        ("OFF" if not ep.get("enabled", True) else "")
                        or ("INV" if ep.get("invert", False) else ep.get("start_side", "high").upper())
                    ),
                ),
                tags=(tag,)
            )

        try:
            if hasattr(self, "track_episode_combo"):
                values = [
                    f"E{ep['index']} — {ep.get('name', 'Épisode ' + str(ep['index']))}"
                    for ep in self.visual_episodes
                ]
                self.track_episode_combo.configure(values=values)
                if values and not self.track_episode_select_var.get():
                    self.track_episode_select_var.set(values[0])
        except Exception:
            pass

        if self.visual_episodes:
            try:
                tree.selection_set("1")
                tree.focus("1")
                self._on_visual_episode_selected()
            except Exception:
                pass

    def _on_visual_episode_selected(self, _event=None):
        tree = getattr(self, "visual_episode_tree", None)
        if tree is None:
            return

        sel = tree.selection()
        if not sel:
            return

        try:
            idx = int(sel[0]) - 1
            ep = self.visual_episodes[idx]
        except Exception:
            return

        self.visual_episode_high_var.set(str(ep["high"]))
        self.visual_episode_low_var.set(str(ep["low"]))
        self.visual_episode_high_deg_var.set(
            str(ep.get("high_deg", int(round(ep["high"] * 1.8))))
        )
        self.visual_episode_low_deg_var.set(
            str(ep.get("low_deg", int(round(ep["low"] * 1.8))))
        )
        self.visual_episode_name_var.set(ep.get("name", f"Épisode {ep['index']}"))
        self.visual_episode_enabled_var.set(bool(ep.get("enabled", True)))
        self.visual_episode_invert_var.set(bool(ep.get("invert", False)))
        self.visual_episode_start_side_var.set(ep.get("start_side", "high"))
        self.visual_episode_offset_var.set(str(ep.get("offset_ms", 0)))
        self.visual_episode_pause_mode_var.set(ep.get("pause_mode", "hold"))
        _pi = _fixed_episode_pattern_index(ep.get("pattern_index", 0))
        self.visual_episode_pattern_var.set(EPISODE_PATTERN_NAMES[_pi])
        self.visual_episode_color_var.set(ep.get("color", "#ff6b6b"))

    def _on_visual_episode_pattern_selected(self, _event=None):
        """Prépare la plage fixe choisie, encore modifiable avant application."""
        pattern_index = EPISODE_PATTERN_NAMES.index(self.visual_episode_pattern_var.get())
        high, low = _episode_pattern_range(pattern_index)
        self.visual_episode_high_var.set(str(high))
        self.visual_episode_low_var.set(str(low))
        self._sync_episode_degrees_from_percent()

    def _sync_episode_degrees_from_percent(self):
        try:
            high = max(0, min(100, int(float(self.visual_episode_high_var.get()))))
            low = max(0, min(100, int(float(self.visual_episode_low_var.get()))))
            self.visual_episode_high_deg_var.set(str(int(round(high * 1.8))))
            self.visual_episode_low_deg_var.set(str(int(round(low * 1.8))))
        except Exception:
            pass

    def _sync_episode_percent_from_degrees(self):
        try:
            high_deg = max(0, min(180, int(float(self.visual_episode_high_deg_var.get()))))
            low_deg = max(0, min(180, int(float(self.visual_episode_low_deg_var.get()))))
            self.visual_episode_high_var.set(str(int(round(high_deg / 1.8))))
            self.visual_episode_low_var.set(str(int(round(low_deg / 1.8))))
        except Exception:
            pass

    def apply_visual_episode_range(self):
        """Applique tous les réglages à l'épisode sélectionné."""
        tree = getattr(self, "visual_episode_tree", None)
        if tree is None:
            return

        sel = tree.selection()

        if not sel and hasattr(self, "track_episode_select_var"):
            value = self.track_episode_select_var.get().strip()
            try:
                number = int(value.split()[0].replace("E", ""))
                sel = (str(number),)
            except Exception:
                pass

        if not sel:
            messagebox.showinfo("Épisodes", "Sélectionne d'abord un épisode.")
            return

        try:
            idx = int(sel[0]) - 1
            high = max(0, min(100, int(float(self.visual_episode_high_var.get()))))
            low = max(0, min(100, int(float(self.visual_episode_low_var.get()))))
            high_deg = max(0, min(180, int(float(self.visual_episode_high_deg_var.get()))))
            low_deg = max(0, min(180, int(float(self.visual_episode_low_deg_var.get()))))
            offset_ms = max(-10000, min(10000, int(float(self.visual_episode_offset_var.get()))))
        except Exception:
            messagebox.showerror("Épisodes", "Une des valeurs numériques est invalide.")
            return

        # Si l'utilisateur a modifié les degrés, ils deviennent prioritaires.
        high_from_deg = int(round(high_deg / 1.8))
        low_from_deg = int(round(low_deg / 1.8))

        # Si les deux paires ne correspondent pas, utiliser les degrés.
        if high_from_deg != high or low_from_deg != low:
            high = high_from_deg
            low = low_from_deg

        if low > high:
            low, high = high, low
            low_deg, high_deg = high_deg, low_deg

        ep = self.visual_episodes[idx]

        try:
            pattern_index = EPISODE_PATTERN_NAMES.index(self.visual_episode_pattern_var.get())
        except Exception:
            pattern_index = _fixed_episode_pattern_index(ep.get("pattern_index", 0))

        # Le preset fournit sa plage de base. Les champs Haut/Bas restent
        # entièrement éditables après sélection du preset.
        ep["pattern_index"] = pattern_index
        ep["name"] = self.visual_episode_name_var.get().strip() or f"Épisode {idx+1}"
        ep["high"] = high
        ep["low"] = low
        ep["high_deg"] = int(round(high * 1.8))
        ep["low_deg"] = int(round(low * 1.8))
        ep["enabled"] = bool(self.visual_episode_enabled_var.get())
        ep["invert"] = bool(self.visual_episode_invert_var.get())
        ep["start_side"] = self.visual_episode_start_side_var.get()
        ep["offset_ms"] = offset_ms
        ep["pause_mode"] = self.visual_episode_pause_mode_var.get()
        ep["color"] = self.visual_episode_color_var.get().strip() or ep["color"]

        self._on_visual_episode_selected()
        self._refresh_visual_episode_tree()

        try:
            self.visual_episode_tree.selection_set(str(idx + 1))
        except Exception:
            pass

        self.status_var.set(
            f"{ep['name']} : {ep['high']}/{ep['low']} "
            f"({ep['high_deg']}°/{ep['low_deg']}°), offset {ep['offset_ms']:+d} ms."
        )

    def apply_visual_episode_range_to_all(self):
        """Applique la plage courante à tous les épisodes."""
        try:
            high = max(0, min(100, int(float(self.visual_episode_high_var.get()))))
            low = max(0, min(100, int(float(self.visual_episode_low_var.get()))))
        except Exception:
            messagebox.showerror("Épisodes", "Valeurs Haut/Bas invalides.")
            return

        if low > high:
            low, high = high, low

        for ep in self.visual_episodes:
            ep["high"] = high
            ep["low"] = low

        self._refresh_visual_episode_tree()
        self.status_var.set(
            f"Tous les épisodes : plage {high} / {low}."
        )

    def _generate_visual_episode_actions(
        self,
        episode,
        logic,
        progress_cb=None,
    ):
        """Génère un épisode avec un rythme constant et un motif 1/4/8 temps fixe."""
        if not episode.get("enabled", True):
            return []

        offset_ms = int(episode.get("offset_ms", 0))
        events = [max(0, int(t) + offset_ms) for t in episode["events"]]
        if not events:
            return []

        pattern_index = _fixed_episode_pattern_index(episode.get("pattern_index", 0))
        preset = EPISODE_PATTERN_PRESETS[pattern_index]
        ranges = list(preset.get("ranges") or [(episode["high"], episode["low"])])

        # Profil automatique : motif relatif fixe de 4/8 temps. Les nombres sont
        # des écarts appliqués au BAS de l'épisode. Exemple avec bas=50 et
        # [0, 0, -30, 0] => 100-50 | 100-50 | 100-20 | 100-50.
        # Le même bloc est ensuite répété sans changement pendant tout l'épisode.
        internal_pattern = episode.get("internal_pattern")
        if internal_pattern:
            base_high = int(episode["high"])
            base_low = int(episode["low"])
            ranges = [
                (base_high, max(0, min(base_high, base_low + int(delta))))
                for delta in internal_pattern
            ]
        elif len(ranges) == 1:
            # Pour les presets Fixe, les champs Haut/Bas éditables restent prioritaires.
            ranges = [(int(episode["high"]), int(episode["low"]))]

        if episode.get("invert", False):
            ranges = [(low, high) for high, low in ranges]

        start_high = episode.get("start_side", "high") != "low"
        reverse_mode = bool(self.visual_reverse_analysis_var.get())
        actions = []
        total = max(1, len(events))

        def cycle_range(cycle_index):
            return ranges[int(cycle_index) % len(ranges)]

        if logic == "marker_alternating":
            # Deux marqueurs = un cycle/temps. Le motif change seulement à la
            # frontière du cycle et garde exactement la même cadence de marqueurs.
            source = list(reversed(events)) if reverse_mode else list(events)
            tmp = []
            for i, t in enumerate(source):
                high, low = cycle_range(i // 2)
                is_high = (i % 2 == 0) if start_high else (i % 2 != 0)
                tmp.append({"at": int(t), "pos": int(high if is_high else low)})
                if progress_cb:
                    progress_cb((i + 1) / total)
            actions = sorted(tmp, key=lambda a: int(a["at"]))

        else:
            # marker_peaks : chaque intervalle entre deux marqueurs est un temps
            # complet. Le sommet est au marqueur et le creux au milieu.
            source = list(reversed(events)) if reverse_mode else list(events)
            tmp = []
            for i, t in enumerate(source):
                high, low = cycle_range(i)
                primary = high if start_high else low
                secondary = low if start_high else high
                tmp.append({"at": int(t), "pos": int(primary)})

                if i < len(source) - 1:
                    other_t = int(source[i + 1])
                    if reverse_mode:
                        mid_t = int(round((other_t + int(t)) / 2.0))
                        if other_t < mid_t < int(t):
                            tmp.append({"at": mid_t, "pos": int(secondary)})
                    else:
                        mid_t = int(round((int(t) + other_t) / 2.0))
                        if int(t) < mid_t < other_t:
                            tmp.append({"at": mid_t, "pos": int(secondary)})

                if progress_cb:
                    progress_cb((i + 1) / total)
            actions = sorted(tmp, key=lambda a: int(a["at"]))

        return actions

    def generate_visual_actions_by_episode(self, duration_ms, logic, progress_cb=None):
        """
        Génère la trame épisode par épisode.
        Une pause > seuil reste plate jusqu'au prochain épisode.
        """
        if not self.visual_episodes:
            self.rebuild_visual_episodes()

        all_actions = []
        total_markers = sum(ep["count"] for ep in self.visual_episodes) or 1
        done = 0

        for epi, ep in enumerate(self.visual_episodes):
            def local_cb(frac, ep_count=ep["count"]):
                if progress_cb:
                    progress_cb((done + frac * ep_count) / total_markers)

            ep_actions = self._generate_visual_episode_actions(
                ep,
                logic,
                progress_cb=local_cb
            )

            if not ep_actions:
                done += ep["count"]
                continue

            # Première position stable au début de la vidéo.
            if not all_actions and ep_actions[0]["at"] > 0:
                all_actions.append({
                    "at": 0,
                    "pos": int(ep_actions[0]["pos"])
                })

            # Comportement pendant la pause avant le prochain épisode.
            if all_actions and ep_actions[0]["at"] - all_actions[-1]["at"] > 1:
                hold_t = ep_actions[0]["at"] - 1
                if hold_t > all_actions[-1]["at"]:
                    pause_mode = ep.get("pause_mode", "hold")
                    if pause_mode == "center":
                        pause_pos = int(round((ep["high"] + ep["low"]) / 2.0))
                    elif pause_mode == "low":
                        pause_pos = int(ep["low"])
                    elif pause_mode == "high":
                        pause_pos = int(ep["high"])
                    else:
                        pause_pos = int(all_actions[-1]["pos"])

                    all_actions.append({
                        "at": int(hold_t),
                        "pos": pause_pos
                    })

            all_actions.extend(ep_actions)
            done += ep["count"]

        if all_actions and all_actions[-1]["at"] < duration_ms:
            all_actions.append({
                "at": int(duration_ms),
                "pos": int(all_actions[-1]["pos"])
            })

        return normalize_actions(all_actions)

    # ------------------------------------------------------------
    # v4.14.6 — 100 PROFILS ADAPTATIFS + TRAITEMENT PAR LOT
    # ------------------------------------------------------------
    def _profile_pool_file(self):
        return self.config_dir / "cockhero_profile_pool.json"

    def _load_profile_pool(self):
        """Pioche persistante : aucun profil répété avant les 100."""
        import random
        path = self._profile_pool_file()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            remaining = [int(x) for x in data.get("remaining", []) if 1 <= int(x) <= 100]
            if remaining:
                return remaining
        except Exception:
            pass
        remaining = list(range(1, 101))
        random.SystemRandom().shuffle(remaining)
        return remaining

    def _next_unique_profile_id(self):
        import random
        remaining = self._load_profile_pool()
        if not remaining:
            remaining = list(range(1, 101))
            random.SystemRandom().shuffle(remaining)
        profile_id = int(remaining.pop(0))
        try:
            self._profile_pool_file().write_text(
                json.dumps({"remaining": remaining}, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass
        return profile_id

    def _episode_progress_target(self, duration_ms):
        """Retourne l'épisode dont le milieu est le plus proche de 65 % de la vidéo."""
        if not self.visual_episodes:
            return 0, max(1, int(duration_ms * 0.65))
        target_ms = max(1, int(duration_ms * 0.65))
        best_i = min(
            range(len(self.visual_episodes)),
            key=lambda i: abs(
                ((int(self.visual_episodes[i].get("start", 0)) +
                  int(self.visual_episodes[i].get("end", 0))) // 2) - target_ms
            )
        )
        ep = self.visual_episodes[best_i]
        target_mid = max(1, (int(ep.get("start", 0)) + int(ep.get("end", 0))) // 2)
        return best_i, target_mid

    def _adaptive_profile_values(self, profile_id, episode_mid_ms, target_mid_ms):
        """Progression 100-50 -> 100-0, atteinte à l'épisode le plus proche de 65 %."""
        import math
        pid = max(1, min(100, int(profile_id)))
        x = max(0.0, min(1.0, float(episode_mid_ms) / max(1.0, float(target_mid_ms))))
        family = (pid - 1) % 10
        variant = (pid - 1) // 10
        target = x

        if family == 0:
            p = target
        elif family == 1:
            p = target ** (1.08 + 0.025 * variant)
        elif family == 2:
            p = target ** (0.86 + 0.012 * variant)
        elif family == 3:
            p = target * target * (3.0 - 2.0 * target)
        elif family == 4:
            steps = 5 + (variant % 5)
            p = round(target * steps) / steps
        elif family == 5:
            p = target + 0.055 * math.sin((2 + variant % 3) * math.pi * target) * (1-target)
        elif family == 6:
            p = target + 0.045 * math.sin((3 + variant % 4) * math.pi * target) * (1-target)
        elif family == 7:
            p = 0.65 * target + 0.35 * (target ** (1.35 + variant * 0.02))
        elif family == 8:
            p = 0.55 * target + 0.45 * (target ** 0.82)
        else:
            p = 0.5 * target + 0.5 * (target * target * (3.0 - 2.0 * target))

        p = max(0.0, min(1.0, p))
        low = max(0, min(50, int(round(50.0 * (1.0 - p)))))
        return 100, low

    def _apply_adaptive_profile(self, profile_id, duration_ms=None):
        """Progression entre épisodes + motif fixe répété dans chaque épisode."""
        n = len(self.visual_episodes)
        if n <= 0:
            return
        if not duration_ms:
            duration_ms = self._video_duration_ms_for_generation() or max(
                int(ep.get("end", 0)) for ep in self.visual_episodes
            )
        target_i, target_mid = self._episode_progress_target(duration_ms)

        # Motifs relatifs. Un seul motif est choisi par épisode et répété à l'identique.
        motifs = (
            [0, 0, -30, 0],
            [0, 0, -20, 0],
            [0, 0, 0, -30, 0, 0, -20, 0],
            [0, -20, 0, -20],
        )

        for i, ep in enumerate(self.visual_episodes):
            mid = (int(ep.get("start", 0)) + int(ep.get("end", 0))) // 2
            high, low = self._adaptive_profile_values(profile_id, mid, target_mid)
            # L'épisode cible (le plus proche de 65 %) doit atteindre exactement 100-0.
            if i >= target_i:
                high, low = 100, 0
            ep["high"] = high
            ep["low"] = low
            ep["high_deg"] = int(round(high * 1.8))
            ep["low_deg"] = int(round(low * 1.8))
            ep["pattern_index"] = 0
            ep["profile_id"] = int(profile_id)

            # Avant 100-0 : variations rythmiques fixes 4T/8T, jamais aléatoires
            # à l'intérieur de l'épisode. À 100-0, conserver la pleine plage.
            if low > 0:
                ep["internal_pattern"] = list(motifs[(i + int(profile_id)) % len(motifs)])
            else:
                ep.pop("internal_pattern", None)

        self._refresh_visual_episode_tree()

    def _write_preview_next_to_video(self):
        """Sauvegarde automatique du funscript dans le même dossier que la vidéo."""
        if not self.current_video or not self.preview_data:
            raise RuntimeError("Aucune vidéo/trame à sauvegarder.")
        out = Path(self.current_video).with_suffix(".funscript")
        out.write_text(
            json.dumps(self.preview_data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        return out

    def _open_batch_progress_window(self):
        if self.batch_progress_window and self.batch_progress_window.winfo_exists():
            return
        win = tk.Toplevel(self)
        self.batch_progress_window = win
        win.title("CockHero — Traitement par lot")
        win.geometry("760x240")
        win.minsize(480, 220)
        win.transient(self)
        box = ttk.Frame(win, padding=6)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="TRAITEMENT PAR LOT", font=("Sans", 13, "bold")).pack(anchor="w")
        ttk.Label(box, textvariable=self.batch_status_var).pack(anchor="w", pady=(12,4))
        ttk.Progressbar(box, variable=self.batch_video_progress_var, maximum=100).pack(fill="x")
        ttk.Label(box, textvariable=self.batch_detail_var).pack(anchor="w", pady=(14,4))
        ttk.Progressbar(box, variable=self.batch_total_progress_var, maximum=100).pack(fill="x")
        ttk.Button(box, text="Annuler après la vidéo actuelle",
                   command=self._cancel_batch).pack(anchor="e", pady=(12,0))

    def _cancel_batch(self):
        self.batch_running = False
        self.batch_status_var.set("Annulation demandée — arrêt après l'étape actuelle.")

    def start_visual_batch(self):
        """Sélectionne plusieurs vidéos et lance analyse + profil + sauvegarde."""
        if self.analysis_running or self.batch_running:
            messagebox.showinfo("Traitement par lot", "Une analyse est déjà en cours.")
            return

        paths = filedialog.askopenfilenames(
            title="Sélectionner les vidéos CockHero à traiter",
            initialdir=self._initial_dir(),
            filetypes=[
                ("Vidéos", "*.mp4 *.mkv *.avi *.mov *.webm *.m4v"),
                ("Tous les fichiers", "*.*"),
            ],
        )
        if not paths:
            return

        self.batch_videos = list(paths)
        self.batch_index = 0
        self.batch_results = []
        self.batch_running = True
        self._open_batch_progress_window()
        self._start_next_batch_video()

    def _start_next_batch_video(self):
        if not self.batch_running or self.batch_index >= len(self.batch_videos):
            self._finish_batch()
            return

        video = self.batch_videos[self.batch_index]
        self.current_video = video
        self.video_var.set(Path(video).name)
        self.current_video_name_var.set(f"VIDÉO : {Path(video).name}")

        # v4.14.6 : ne jamais écraser silencieusement un funscript existant.
        existing_funscript = Path(video).with_suffix(".funscript")
        if existing_funscript.exists() and not self.batch_overwrite_existing_var.get():
            total = len(self.batch_videos)
            self.batch_video_progress_var.set(100)
            self.batch_status_var.set(
                f"Vidéo {self.batch_index + 1} / {total} — {Path(video).name}"
            )
            self.batch_detail_var.set(
                f"IGNORÉE — {existing_funscript.name} existe déjà"
            )
            self.batch_results.append(
                (video, None, f"Funscript existant : {existing_funscript}", None)
            )
            self.batch_index += 1
            self.batch_total_progress_var.set(
                100.0 * self.batch_index / max(1, total)
            )
            self.after(180, self._start_next_batch_video)
            return

        self.detected_line_times_ms = []
        self.visual_episodes = []
        self.preview_data = None
        self.batch_profile_id = self._next_unique_profile_id()

        total = len(self.batch_videos)
        self.batch_video_progress_var.set(0)
        self.batch_total_progress_var.set(100.0 * self.batch_index / max(1, total))
        self.batch_status_var.set(
            f"Vidéo {self.batch_index + 1} / {total} — {Path(video).name}"
        )
        self.batch_detail_var.set(
            f"Étape 1/4 : analyse visuelle/audio — profil réservé {self.batch_profile_id:03d}/100"
        )

        self.visual_auto_generate_pending = True
        self.detect_visual_line_events()

    def _batch_after_analysis(self):
        """Appelé quand l'analyse asynchrone de la vidéo courante est terminée."""
        if not self.batch_running:
            return
        try:
            self.batch_video_progress_var.set(55)
            self.batch_detail_var.set(
                f"Étape 2/4 : épisodes + profil {self.batch_profile_id:03d}/100"
            )
            self.generate_cockhero_visual()

            self.batch_video_progress_var.set(82)
            self.batch_detail_var.set(
                f"Étape 3/4 : application/sauvegarde du profil {self.batch_profile_id:03d}/100"
            )

            # Le profil est appliqué après la segmentation stable, puis on
            # régénère uniquement les actions; aucune détection n'est modifiée.
            duration_ms = self._video_duration_ms_for_generation()
            self._apply_adaptive_profile(self.batch_profile_id, duration_ms=duration_ms)
            actions = self.generate_visual_actions_by_episode(
                duration_ms=duration_ms,
                logic=self.visual_generation_logic_var.get(),
            )
            self.preview_data["actions"] = actions
            meta = self.preview_data.setdefault("metadata", {}).setdefault("cockheroGenerator", {})
            meta["adaptiveProfile"] = int(self.batch_profile_id)
            meta["adaptiveProfileCount"] = 100
            meta["episodes"] = [
                {
                    "number": ep["index"], "name": ep.get("name", f"Épisode {ep['index']}"),
                    "start": ep["start"], "end": ep["end"], "count": ep["count"],
                    "high": ep["high"], "low": ep["low"],
                    "patternIndex": ep.get("pattern_index", 0),
                    "profileId": self.batch_profile_id,
                }
                for ep in self.visual_episodes
            ]

            out = self._write_preview_next_to_video()
            self.batch_video_progress_var.set(100)
            self.batch_detail_var.set(f"Étape 4/4 : terminé — {out.name}")
            self.batch_results.append((self.current_video, True, str(out), self.batch_profile_id))
        except Exception as exc:
            self.batch_results.append((self.current_video, False, str(exc), self.batch_profile_id))

        self.batch_index += 1
        self.batch_total_progress_var.set(
            100.0 * self.batch_index / max(1, len(self.batch_videos))
        )
        self.after(250, self._start_next_batch_video)

    def _finish_batch(self):
        total = len(self.batch_videos)
        ok = sum(1 for r in self.batch_results if r[1] is True)
        skipped = sum(1 for r in self.batch_results if r[1] is None)
        errors = sum(1 for r in self.batch_results if r[1] is False)
        self.batch_running = False
        self.batch_video_progress_var.set(100)
        self.batch_total_progress_var.set(100)
        self.batch_status_var.set(f"Terminé — {ok} générée(s) — {skipped} ignorée(s) — {errors} erreur(s)")
        self.batch_detail_var.set(
            "Les .funscript réussis ont été enregistrés automatiquement à côté de leurs vidéos."
        )
        if total:
            messagebox.showinfo(
                "Traitement par lot",
                f"Traitement terminé.\n\nGénérées : {ok}\nIgnorées : {skipped}\nErreurs : {errors}\nTotal : {total}"
            )



    def _set_generation_progress(self, value, stage):
        """Met à jour la progression de génération du funscript."""
        try:
            value = max(0.0, min(100.0, float(value)))
            self.generation_progress_var.set(value)
            self.generation_status_var.set(
                f"Génération : {value:5.1f} % — {stage}"
            )
            self.status_var.set(f"CockHero Visuel : {stage}")
            self.update_idletasks()
        except Exception:
            pass

    def generate_cockhero_visual(self):
        self._set_generation_progress(0.0, "Préparation…")
        try:
            self.btn_generate_visual_panel.configure(state="disabled")
        except Exception:
            pass

        if not self.current_video:
            self.choose_video()
            if not self.current_video:
                return

        if not self.detected_line_times_ms:
            # v4.14.6 : un seul clic. L'analyse démarre et la génération
            # reprend automatiquement quand le thread d'analyse est terminé.
            self.visual_auto_generate_pending = True
            self.detect_visual_line_events()
            self._set_generation_progress(2.0, "Analyse automatique des indicateurs…")
            return

        duration_ms = self._video_duration_ms_for_generation()
        if not duration_ms:
            messagebox.showerror("CockHero Visuel", "Impossible de déterminer la durée de la vidéo.")
            return

        self._set_generation_progress(8.0, "Nettoyage des événements…")
        final_events, raw_count, merged_count, started_count = self._final_clean_visual_events(
            self.detected_line_times_ms
        )

        # Nettoyage structurel du début AVANT génération :
        # un petit faux groupe initial ne doit jamais devenir E1.
        final_events, false_start_removed, first_valid_start = (
            self._trim_false_leading_event_groups(
                final_events,
                min_duration_ms=6500,
                min_events=18,
            )
        )

        if false_start_removed:
            started_count = len(final_events)
            self._set_generation_progress(
                12.0,
                f"Faux départ supprimé — {false_start_removed} marqueur(s) retiré(s) "
                f"avant {self._format_time(first_valid_start)}"
            )

        if not final_events:
            messagebox.showwarning(
                "CockHero Visuel",
                "Aucun franchissement ne reste après le nettoyage final."
            )
            return

        # Remplace la liste active par la liste réellement utilisée.
        self.detected_line_times_ms = final_events
        self.visual_raw_count = raw_count
        self.visual_merged_count = merged_count
        self.visual_started_count = started_count

        self._set_generation_progress(
            15.0,
            f"Construction de la trame — {len(final_events)} marqueurs…"
        )

        def _progress_generation(frac):
            self._set_generation_progress(
                15.0 + float(frac) * 70.0,
                f"Construction des points — {int(float(frac) * 100):d} %"
            )

        try:
            # Toute pause > seuil devient un épisode.
            # Les plages Haut/Bas du tableau sont appliquées séparément.
            self.rebuild_visual_episodes(final_events)
            # Progression automatique entre épisodes : 100-50 vers 100-0,
            # avec 100-0 atteint sur l'épisode le plus proche de 65 % de la vidéo.
            # Chaque épisode reçoit aussi un motif 4T/8T fixe répété intégralement.
            self._apply_adaptive_profile(1, duration_ms=duration_ms)
            actions = self.generate_visual_actions_by_episode(
                duration_ms=duration_ms,
                logic=self.visual_generation_logic_var.get(),
                progress_cb=_progress_generation,
            )
        except Exception as exc:
            self._set_generation_progress(0.0, "Échec")
            try:
                self.btn_generate_visual_panel.configure(state="normal")
            except Exception:
                pass
            messagebox.showerror("CockHero Visuel", f"Génération impossible : {exc}")
            return

        self.current_file = None
        self.original_data = None
        if self.current_video:
            self.current_funscript_name_var.set(
                f"FUNSCRIPT : {Path(self.current_video).with_suffix('.funscript').name} — aperçu"
            )

        self._set_generation_progress(88.0, "Préparation de l'aperçu…")

        self.preview_data = {
            "version": "1.0",
            "inverted": False,
            "range": 100,
            "actions": actions,
            "metadata": {
                "cockheroGenerator": {
                    "version": APP_VERSION,
                    "logic": self.visual_generation_logic_var.get(),
                    "markers": list(final_events),
                    "episodeGapMs": int(float(self.visual_episode_gap_var.get())),
                    "falseStartMarkersRemoved": int(false_start_removed),
                    "episodes": [
                        {
                            "number": ep["index"],
                            "name": ep.get("name", f"Épisode {ep['index']}"),
                            "start": ep["start"],
                            "end": ep["end"],
                            "count": ep["count"],
                            "high": ep["high"],
                            "low": ep["low"],
                            "highDeg": ep.get("high_deg", int(round(ep["high"] * 1.8))),
                            "lowDeg": ep.get("low_deg", int(round(ep["low"] * 1.8))),
                            "enabled": bool(ep.get("enabled", True)),
                            "invert": bool(ep.get("invert", False)),
                            "startSide": ep.get("start_side", "high"),
                            "offsetMs": int(ep.get("offset_ms", 0)),
                            "pauseMode": ep.get("pause_mode", "hold"),
                            "color": ep["color"],
                        }
                        for ep in self.visual_episodes
                    ],
                }
            },
        }
        self.file_var.set(f"APERÇU NON SAUVEGARDÉ — CockHero Visuel — {Path(self.current_video).stem}")
        self.zoom_start = 0.0
        self.zoom_end = 1.0
        self.stats_var.set(
            f"Bruts {raw_count} → fusion {merged_count} → "
            f"faux début -{false_start_removed} → final {started_count} → "
            f"{len(self.visual_episodes)} épisode(s) → {len(actions)} points | "
            f"durée {self._format_time(duration_ms)}"
        )
        logic_name = (
            "sommet + creux au milieu"
            if self.visual_generation_logic_var.get() == "marker_peaks"
            else "extrémités directes haut/bas"
        )
        self.status_var.set(
            f"v4.6.3 : {started_count} marqueurs — logique : {logic_name}. "
            "Aucun .funscript écrit tant que tu ne cliques pas Enregistrer."
        )
        self._save_app_config()
        self.draw_all()

    # ------------------------------------------------------------
    # DETECTION VISUELLE COCKHERO
    # ------------------------------------------------------------
    def detect_visual_indicators(self):
        """
        Détecte des événements dans une bande de la vidéo en mesurant les
        changements visuels. Cela fonctionne avec plusieurs formes d'indicateurs :
        barres, cercles, rectangles, flashes, icônes, etc.
        """
        if not self.current_video:
            self.choose_video()
            if not self.current_video:
                return

        try:
            top_pct = max(0.0, min(99.0, float(self.visual_roi_top_var.get())))
            bottom_pct = max(top_pct + 1.0, min(100.0, float(self.visual_roi_bottom_var.get())))
            threshold = max(0.1, float(self.visual_threshold_var.get()))
            min_interval = max(50, int(self.visual_min_interval_var.get()))
            fps = max(2.0, min(30.0, float(self.visual_sample_fps_var.get())))
        except Exception as exc:
            messagebox.showerror("Détection visuelle", f"Paramètre invalide : {exc}")
            return

        self.visual_detection_status_var.set("Analyse visuelle en cours...")
        self.status_var.set("CockHero : analyse des indicateurs visuels...")
        self.update_idletasks()

        raw_path = None
        try:
            # On demande à ffmpeg une petite image grise de la bande choisie.
            # scale=320:-1 réduit fortement le coût CPU.
            vf = (
                f"crop=iw:ih*{(bottom_pct-top_pct)/100.0}:0:ih*{top_pct/100.0},"
                f"scale=320:-1,format=gray,fps={fps}"
            )

            # Détermine la hauteur résultante via ffprobe + ratio source.
            probe = subprocess.check_output(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height",
                    "-of", "csv=p=0:s=x",
                    self.current_video,
                ],
                stderr=subprocess.DEVNULL,
                text=True
            ).strip()
            sw, sh = [int(x) for x in probe.split("x")[:2]]
            crop_h = max(2, int(sh * (bottom_pct-top_pct)/100.0))
            out_h = max(2, int(round(crop_h * 320.0 / sw)))

            fd, raw_path = tempfile.mkstemp(prefix="funscript_visual_", suffix=".raw")
            os.close(fd)

            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", self.current_video,
                    "-an", "-vf", vf,
                    "-f", "rawvideo", "-pix_fmt", "gray", raw_path
                ],
                check=True
            )

            frame_size = 320 * out_h
            if frame_size <= 0:
                raise RuntimeError("Dimension d'analyse visuelle invalide.")

            data = Path(raw_path).read_bytes()
            frame_count = len(data) // frame_size
            if frame_count < 3:
                raise RuntimeError("Pas assez d'images pour analyser la vidéo.")

            # Score = moyenne des différences absolues entre images successives.
            # Python pur pour éviter une nouvelle dépendance obligatoire.
            scores = []
            prev = data[0:frame_size]
            for i in range(1, frame_count):
                cur = data[i*frame_size:(i+1)*frame_size]
                # Échantillonnage 1 pixel sur 4 : suffisant pour flashes/formes.
                total = 0
                n = 0
                for j in range(0, frame_size, 4):
                    total += abs(cur[j] - prev[j])
                    n += 1
                scores.append(total / max(1, n))
                prev = cur

            if not scores:
                raise RuntimeError("Aucun changement visuel mesurable.")

            # Seuil utilisateur + seuil adaptatif basé sur le bruit de fond.
            sorted_scores = sorted(scores)
            median = sorted_scores[len(sorted_scores)//2]
            deviations = sorted(abs(s - median) for s in scores)
            mad = deviations[len(deviations)//2] if deviations else 0.0
            adaptive = median + max(2.0, mad * 4.0)
            effective_threshold = max(threshold, adaptive)

            events = []
            last_event = -10**9
            for idx, score in enumerate(scores, start=1):
                t_ms = int(round(idx * 1000.0 / fps))
                if score >= effective_threshold and t_ms - last_event >= min_interval:
                    # Garde le pic local approximatif.
                    events.append(t_ms)
                    last_event = t_ms

            self.detected_visual_times_ms = events
            self.use_visual_beats_var.set(bool(events))

            self.visual_detection_status_var.set(
                f"{len(events)} événements détectés | "
                f"zone {top_pct:.0f}-{bottom_pct:.0f}% | seuil réel {effective_threshold:.1f}"
            )
            self.status_var.set(
                "Analyse visuelle terminée. Génère CockHero pour voir les événements sur la trame."
            )
            self._save_app_config()
            self.draw_all()

        except subprocess.CalledProcessError:
            self.visual_detection_status_var.set("Erreur ffmpeg")
            messagebox.showerror(
                "Détection visuelle",
                "FFmpeg n'a pas pu analyser les images de cette vidéo."
            )
        except Exception as exc:
            self.visual_detection_status_var.set(f"Échec : {exc}")
            messagebox.showerror("Détection visuelle", str(exc))
        finally:
            if raw_path:
                try:
                    os.unlink(raw_path)
                except Exception:
                    pass

    def _combined_cockhero_events(self):
        """Retourne uniquement les événements issus de l'analyse visuelle."""
        events = []
        if self.use_visual_beats_var.get() and self.detected_visual_times_ms:
            events.extend(self.detected_visual_times_ms)
        if self.use_line_events_var.get() and self.detected_line_times_ms:
            events.extend(self.detected_line_times_ms)
        return sorted(set(int(t) for t in events))


    # ------------------------------------------------------------
    # MODE COCKHERO
    # ------------------------------------------------------------



    def _mark_preview_unsaved(self, label="CockHero"):
        """Marque le résultat comme temporaire; aucun chemin de sortie n'est associé."""
        self.current_file = None
        if self.current_video:
            self.file_var.set(
                f"APERÇU NON SAUVEGARDÉ — {label} — {Path(self.current_video).stem}"
            )
        else:
            self.file_var.set(f"APERÇU NON SAUVEGARDÉ — {label}")

    # ------------------------------------------------------------
    # PROCESS
    # ------------------------------------------------------------
    def generate_preview(self):
        if not self.current_file:
            messagebox.showinfo("Aperçu", "Choisis d'abord un fichier .funscript.")
            return
        if not self.apply_parameters():
            return
        try:
            result = process_file(self.current_file, Path(self.current_file).name)
            self.preview_data = result[0]
            before, after, added = result[1], result[2], result[3]
            cycle_gaps_used, total_cycles_repeated = result[4], result[8]
            duration = max((int(a["at"]) for a in self.preview_data.get("actions", [])), default=0)
            self.stats_var.set(f"Original {before}   |   Optimisé {after}   |   +{added}   |   durée {self._format_time(duration)}")
            gap_mode = "patterns" if self.use_patterns_var.get() else "cycles"
            end_mode = "patterns fin" if self.use_end_patterns_var.get() else "cycles fin"
            self.status_var.set(f"Aperçu généré — trous: {gap_mode} | fin: {end_mode}.")
            self.draw_all()
        except Exception as exc:
            messagebox.showerror("Erreur d'aperçu", str(exc))

    def apply_to_file(self):
        if not self.current_file:
            messagebox.showinfo("Appliquer", "Choisis d'abord un fichier.")
            return
        if self.preview_data is None:
            self.generate_preview()
            if self.preview_data is None:
                return
        if not messagebox.askyesno("Confirmation", f"Écraser :\n{self.current_file}\n\nUne sauvegarde .bak sera créée."):
            return
        try:
            backup = self.current_file + ".bak"
            if not os.path.exists(backup):
                Path(backup).write_bytes(Path(self.current_file).read_bytes())
            with open(self.current_file, "w", encoding="utf-8") as f:
                json.dump(self.preview_data, f, ensure_ascii=False, separators=(",", ":"))
            self.original_data = copy.deepcopy(self.preview_data)
            self.preview_data = None
            self.status_var.set(f"Fichier enregistré. Sauvegarde : {Path(backup).name}")
            self.draw_all()
        except Exception as exc:
            messagebox.showerror("Erreur", str(exc))

    def process_folder_gui(self):
        if not self.apply_parameters():
            return
        folder = CURRENT_DIR
        files = sorted(Path(folder).glob("*.funscript"))
        if not files:
            messagebox.showinfo("Dossier", "Aucun .funscript.")
            return
        if not messagebox.askyesno("Traiter le dossier", f"Traiter {len(files)} fichier(s) ?"):
            return
        self.progress["maximum"] = len(files)
        self.progress["value"] = 0
        done = 0
        for idx, path in enumerate(files, 1):
            self.status_var.set(f"{idx}/{len(files)} — {path.name}")
            self.update_idletasks()
            try:
                bak = str(path) + ".bak"
                if not os.path.exists(bak):
                    Path(bak).write_bytes(path.read_bytes())
                data, *_ = process_file(str(path), path.name)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
                done += 1
            except Exception:
                pass
            self.progress["value"] = idx
        self.status_var.set(f"Terminé : {done}/{len(files)}")


    # ------------------------------------------------------------
    # SUIVI DE LECTURE + INDICATEUR DE MOUVEMENT
    # ------------------------------------------------------------
    def _active_motion_data(self):
        """Retourne la trame utilisée pour l'animation verticale."""
        if self.preview_data and self.preview_data.get("actions"):
            return self.preview_data
        if self.original_data and self.original_data.get("actions"):
            return self.original_data
        return None

    def _position_at_time(self, time_ms):
        """Interpolation linéaire de la position funscript à un instant donné."""
        data = self._active_motion_data()
        if not data:
            return 50.0

        actions = data.get("actions", [])
        if not actions:
            return 50.0

        t = int(time_ms)

        if t <= int(actions[0]["at"]):
            return float(actions[0]["pos"])
        if t >= int(actions[-1]["at"]):
            return float(actions[-1]["pos"])

        # Recherche binaire manuelle pour éviter de parcourir des milliers de points.
        lo, hi = 0, len(actions) - 1
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if int(actions[mid]["at"]) <= t:
                lo = mid
            else:
                hi = mid

        a = actions[lo]
        b = actions[hi]
        ta = int(a["at"])
        tb = int(b["at"])
        pa = float(a["pos"])
        pb = float(b["pos"])

        if tb <= ta:
            return max(0.0, min(100.0, pb))

        ratio = (t - ta) / float(tb - ta)
        pos = pa + (pb - pa) * ratio
        return max(0.0, min(100.0, pos))

    def _follow_detail_playhead(self, duration_ms):
        """
        Fait suivre la vue détaillée par la ligne de lecture.
        Si la vue est encore 'Tout voir', crée automatiquement une fenêtre
        d'environ 30 secondes autour de la lecture.
        """
        duration_ms = max(1, int(duration_ms))
        current_ratio = max(0.0, min(1.0, self.playhead_ms / duration_ms))

        width = max(0.0005, self.zoom_end - self.zoom_start)

        # Une vue complète ne permet pas de voir le mouvement.
        # On passe automatiquement à ~30 s lors du suivi.
        if width > 0.50:
            width = min(0.25, max(0.002, 30000.0 / duration_ms))

        # Garde le playhead dans le tiers central.
        left_guard = self.zoom_start + width * 0.35
        right_guard = self.zoom_start + width * 0.65

        if current_ratio < left_guard or current_ratio > right_guard:
            new_start = current_ratio - width * 0.50
            new_end = new_start + width

            if new_start < 0:
                new_start = 0.0
                new_end = width
            if new_end > 1:
                new_end = 1.0
                new_start = max(0.0, 1.0 - width)

            self.zoom_start = new_start
            self.zoom_end = new_end

    def _draw_motion_indicator(self):
        """Animation verticale 0–100 représentant le mouvement du funscript."""
        if not hasattr(self, "motion_canvas"):
            return

        c = self.motion_canvas
        c.delete("all")

        w = max(50, c.winfo_width())
        h = max(160, c.winfo_height())

        top = 24
        bottom = h - 28
        center_x = w // 2
        track_h = max(1, bottom - top)

        # Axe vertical + repères.
        c.create_line(center_x, top, center_x, bottom, fill="#4d5a62", width=4)

        for value in (100, 75, 50, 25, 0):
            y = bottom - (value / 100.0) * track_h
            c.create_line(center_x - 9, y, center_x + 9, y, fill="#6d7b83")
            c.create_text(
                4, y,
                text=str(value),
                fill="#9caab2",
                anchor="w",
                font=("Sans", 7)
            )

        pos = max(0.0, min(100.0, float(self.motion_pos)))
        y = bottom - (pos / 100.0) * track_h

        # Direction.
        delta = pos - float(self.motion_last_pos)
        if delta > 0.25:
            direction = "↑"
        elif delta < -0.25:
            direction = "↓"
        else:
            direction = "•"

        # Position actuelle : boule mobile.
        c.create_oval(
            center_x - 11, y - 11,
            center_x + 11, y + 11,
            fill="#35dc8b",
            outline="#ffffff",
            width=2
        )

        c.create_text(
            center_x, 10,
            text=f"{pos:05.1f}",
            fill="#ffffff",
            anchor="n",
            font=("Sans", 9, "bold")
        )
        c.create_text(
            center_x, h - 8,
            text=direction,
            fill="#35dc8b",
            anchor="s",
            font=("Sans", 18, "bold")
        )

    # ------------------------------------------------------------
    # DRAW
    # ------------------------------------------------------------

    def _ensure_track_context_menu(self):
        if hasattr(self, "track_context_menu") and self.track_context_menu:
            return

        menu = tk.Menu(self, tearoff=0)

        menu.add_radiobutton(
            label="Mode : éditer les points",
            variable=self.track_edit_mode_var,
            value="points"
        )
        menu.add_radiobutton(
            label="Mode : déplacer toute la trame",
            variable=self.track_edit_mode_var,
            value="shift"
        )
        menu.add_radiobutton(
            label="Mode : navigation vidéo",
            variable=self.track_edit_mode_var,
            value="navigate"
        )

        menu.add_separator()

        menu.add_command(
            label="Ajouter un point ici",
            command=self._context_add_point
        )
        menu.add_command(
            label="Supprimer le point le plus proche",
            command=self._context_delete_point
        )

        menu.add_separator()

        menu.add_command(
            label="Décaler la trame -1 s",
            command=lambda: self._context_shift_track(-1000)
        )
        menu.add_command(
            label="Décaler la trame -100 ms",
            command=lambda: self._context_shift_track(-100)
        )
        menu.add_command(
            label="Décaler la trame +100 ms",
            command=lambda: self._context_shift_track(100)
        )
        menu.add_command(
            label="Décaler la trame +1 s",
            command=lambda: self._context_shift_track(1000)
        )
        menu.add_command(
            label="Réinitialiser l'offset de la trame",
            command=self._context_reset_shift
        )

        menu.add_separator()

        menu.add_checkbutton(
            label="Verrouiller la trame",
            variable=self.track_locked_var
        )

        menu.add_separator()

        menu.add_command(
            label="Éditer l'épisode correspondant",
            command=self._context_edit_episode
        )
        menu.add_command(
            label="Supprimer l'épisode correspondant",
            command=self._delete_episode_at_context_position
        )

        menu.add_separator()

        menu.add_command(
            label="Supprimer le faux départ avant le 1er épisode stable",
            command=self._trim_before_stable_from_menu
        )

        menu.add_command(
            label="Nettoyer les anomalies de début",
            command=self._clean_leading_anomalies_from_menu
        )

        self.track_context_menu = menu

    def _show_track_context_menu(self, event, canvas, kind):
        self._ensure_track_context_menu()
        self.track_context_canvas = canvas
        self.track_context_kind = kind
        self.track_context_event = event

        try:
            self.track_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self.track_context_menu.grab_release()
            except Exception:
                pass
        return "break"

    def _context_xy_to_action(self):
        event = self.track_context_event
        if event is None:
            return None, None

        if self.track_context_kind == "overview":
            return self._overview_xy_to_action(event.x, event.y)
        else:
            return self._track_edit_xy_to_action(event.x, event.y)

    def _context_add_point(self):
        if self.track_locked_var.get():
            return

        at, pos = self._context_xy_to_action()
        if at is None:
            return

        actions = self._editable_actions()
        actions.append({"at": int(at), "pos": int(pos)})
        actions.sort(key=lambda a: int(a.get("at", 0)))

        self.status_var.set(
            "Point ajouté via menu : %d ms / position %d" % (at, pos)
        )
        self.draw_all()

    def _find_first_sustained_action_group(
        self,
        gap_ms=1500,
        min_duration_ms=5000,
        min_actions=20,
        min_turns=8,
    ):
        """
        Trouve le premier groupe d'actions réellement soutenu.

        Les groupes sont séparés par un trou > gap_ms.
        Un groupe n'est considéré comme un vrai épisode que s'il dure
        suffisamment longtemps, contient assez d'actions et assez de
        changements de direction.

        Cela élimine les petits faux paquets de va-et-vient du début.
        """
        actions = self._editable_actions()
        if not actions:
            return None

        ordered = sorted(
            actions,
            key=lambda a: int(a.get("at", 0))
        )

        groups = []
        current = [ordered[0]]

        for action in ordered[1:]:
            gap = int(action.get("at", 0)) - int(current[-1].get("at", 0))
            if gap > int(gap_ms):
                groups.append(current)
                current = [action]
            else:
                current.append(action)

        if current:
            groups.append(current)

        for group in groups:
            if len(group) < 2:
                continue

            start = int(group[0].get("at", 0))
            end = int(group[-1].get("at", start))
            duration = max(0, end - start)
            count = len(group)

            turns = 0
            last_dir = 0
            for a, b in zip(group, group[1:]):
                diff = int(b.get("pos", 0)) - int(a.get("pos", 0))
                direction = 1 if diff > 0 else (-1 if diff < 0 else 0)
                if direction and last_dir and direction != last_dir:
                    turns += 1
                if direction:
                    last_dir = direction

            if (
                duration >= int(min_duration_ms)
                and count >= int(min_actions)
                and turns >= int(min_turns)
            ):
                return {
                    "start": start,
                    "end": end,
                    "count": count,
                    "turns": turns,
                    "actions": group,
                }

        return None

    def _trim_before_first_sustained_group(self):
        """
        Supprime tout ce qui précède le premier groupe réellement soutenu.
        """
        group = self._find_first_sustained_action_group(
            gap_ms=1500,
            min_duration_ms=5000,
            min_actions=20,
            min_turns=8,
        )

        if not group:
            return 0, None

        first_start = int(group["start"])
        actions = self._editable_actions()

        before = len(actions)
        actions[:] = [
            a for a in actions
            if int(a.get("at", 0)) >= first_start
        ]
        removed = before - len(actions)

        if removed <= 0:
            return 0, first_start

        # Supprimer aussi les marqueurs antérieurs.
        for attr in (
            "detected_line_times_ms",
            "loaded_marker_times_ms",
            "detected_visual_times_ms",
        ):
            try:
                values = getattr(self, attr)
                setattr(
                    self,
                    attr,
                    [int(t) for t in values if int(t) >= first_start]
                )
            except Exception:
                pass

        return removed, first_start

    def _trim_track_before_first_episode(self):
        """
        Supprime tous les points avant le premier épisode valide (E1).

        Cette méthode est volontairement déterministe : lorsque les épisodes
        ont été restaurés depuis les métadonnées du funscript, le début de E1
        est la référence. Aucun ancien faux départ, V isolé ou va-et-vient
        parasite n'est conservé avant cette limite.
        """
        if not self.visual_episodes:
            return 0

        first_ep = min(
            self.visual_episodes,
            key=lambda ep: int(ep.get("start", 0))
        )

        first_start = (
            int(first_ep.get("start", 0))
            + int(first_ep.get("offset_ms", 0))
        )
        first_start = max(0, first_start)

        actions = self._editable_actions()
        if not actions:
            return 0

        before = len(actions)

        actions[:] = [
            a for a in actions
            if int(a.get("at", 0)) >= first_start
        ]

        removed = before - len(actions)

        if removed <= 0:
            return 0

        # Nettoyer également les marqueurs antérieurs afin qu'ils ne puissent
        # pas recréer un faux épisode lors d'une reconstruction ultérieure.
        try:
            self.detected_line_times_ms = [
                int(t) for t in self.detected_line_times_ms
                if int(t) >= first_start
            ]
        except Exception:
            pass

        try:
            self.loaded_marker_times_ms = [
                int(t) for t in self.loaded_marker_times_ms
                if int(t) >= first_start
            ]
        except Exception:
            pass

        try:
            self.detected_visual_times_ms = [
                int(t) for t in self.detected_visual_times_ms
                if int(t) >= first_start
            ]
        except Exception:
            pass

        return removed

    def _remove_leading_sparse_block(
        self,
        scan_window_ms=45000,
        abnormal_gap_ms=1200,
        stable_min_actions=30,
        stable_window_ms=5000,
    ):
        """
        Supprime un faux bloc de début contenant une cassure anormale.

        Cas visé :
            oscillations -- grand V / zone clairsemée -- oscillations

        Si ce motif apparaît dans la fenêtre de début, tout le bloc avant le
        prochain groupe réellement stable est supprimé.
        """
        actions = self._editable_actions()
        if not actions or len(actions) < 4:
            return 0

        ordered = sorted(
            actions,
            key=lambda a: int(a.get("at", 0))
        )

        first_at = int(ordered[0].get("at", 0))
        scan_end = first_at + int(scan_window_ms)

        # Ne regarder que le début du script.
        early = [
            a for a in ordered
            if int(a.get("at", 0)) <= scan_end
        ]
        if len(early) < 4:
            return 0

        # Chercher une cassure anormale dans la zone de début.
        break_index = None
        for i in range(1, len(early)):
            t0 = int(early[i-1].get("at", 0))
            t1 = int(early[i].get("at", 0))
            p0 = int(early[i-1].get("pos", 0))
            p1 = int(early[i].get("pos", 0))
            dt = t1 - t0

            # Une grande durée entre deux positions différentes est typique
            # de la longue diagonale anormale montrée par l'utilisateur.
            if dt >= int(abnormal_gap_ms) and p0 != p1:
                break_index = i
                break

        if break_index is None:
            return 0

        # Après la cassure, chercher le prochain bloc dense/stable.
        stable_start_at = None

        for i in range(break_index, len(ordered)):
            start_t = int(ordered[i].get("at", 0))
            end_t = start_t + int(stable_window_ms)

            window = [
                a for a in ordered[i:]
                if int(a.get("at", 0)) <= end_t
            ]

            if len(window) < int(stable_min_actions):
                continue

            # Vérifier qu'il n'y a pas de grand trou dans cette fenêtre.
            stable = True
            for a, b in zip(window, window[1:]):
                gap = int(b.get("at", 0)) - int(a.get("at", 0))
                if gap >= int(abnormal_gap_ms):
                    stable = False
                    break

            if stable:
                stable_start_at = start_t
                break

        if stable_start_at is None:
            return 0

        # Supprimer TOUT le bloc depuis le début jusqu'au groupe stable.
        before = len(actions)
        actions[:] = [
            a for a in actions
            if int(a.get("at", 0)) >= stable_start_at
        ]
        removed = before - len(actions)

        if removed <= 0:
            return 0

        # Nettoyer aussi les marqueurs antérieurs.
        try:
            self.detected_line_times_ms = [
                int(t) for t in self.detected_line_times_ms
                if int(t) >= stable_start_at
            ]
        except Exception:
            pass

        try:
            self.loaded_marker_times_ms = [
                int(t) for t in self.loaded_marker_times_ms
                if int(t) >= stable_start_at
            ]
        except Exception:
            pass

        self._rebuild_episodes_from_action_gaps(1500)
        return removed

    def _remove_leading_track_anomalies(
        self,
        max_gap_ms=1500,
        min_duration_ms=7000,
        min_actions=24,
        min_density_hz=2.2,
    ):
        """
        Supprime les petits groupes anormaux situés au début de la trame.

        Un groupe de début est considéré comme suspect s'il est court,
        contient peu de points ou est très peu dense. Le nettoyage s'arrête
        dès qu'un groupe suffisamment soutenu est trouvé.

        Cette règle vise précisément les faux départs visuels : quelques
        va-et-vient isolés avant le vrai début du CockHero.
        """
        actions = self._editable_actions()
        if not actions:
            return 0, 0

        ordered = sorted(
            actions,
            key=lambda a: int(a.get("at", 0))
        )

        # Segmenter par trous > max_gap_ms.
        groups = []
        current = [ordered[0]]
        for action in ordered[1:]:
            gap = int(action.get("at", 0)) - int(current[-1].get("at", 0))
            if gap > int(max_gap_ms):
                groups.append(current)
                current = [action]
            else:
                current.append(action)
        if current:
            groups.append(current)

        if len(groups) <= 1:
            return 0, 0

        remove_ids = set()
        removed_groups = 0

        # On ne nettoie que la tête de la trame.
        for group in groups:
            start = int(group[0].get("at", 0))
            end = int(group[-1].get("at", start))
            duration = max(1, end - start)
            count = len(group)
            density_hz = count / max(0.001, duration / 1000.0)

            substantial = (
                duration >= int(min_duration_ms)
                and count >= int(min_actions)
                and density_hz >= float(min_density_hz)
            )

            if substantial:
                break

            # Groupe de faux départ : court / pauvre / trop dispersé.
            for action in group:
                remove_ids.add(id(action))
            removed_groups += 1

        if not remove_ids:
            return 0, 0

        before = len(actions)
        actions[:] = [
            a for a in actions
            if id(a) not in remove_ids
        ]
        removed_actions = before - len(actions)

        # Nettoyer aussi les marqueurs correspondants sur la même plage
        # afin que ces faux épisodes ne réapparaissent pas.
        if actions:
            first_valid_at = min(int(a.get("at", 0)) for a in actions)

            try:
                self.detected_line_times_ms = [
                    int(t) for t in self.detected_line_times_ms
                    if int(t) >= first_valid_at
                ]
            except Exception:
                pass

            try:
                self.loaded_marker_times_ms = [
                    int(t) for t in self.loaded_marker_times_ms
                    if int(t) >= first_valid_at
                ]
            except Exception:
                pass

        return removed_groups, removed_actions

    def _rebuild_episodes_from_action_gaps(self, max_gap_ms=1500):
        """
        Reconstruit les épisodes depuis la trame éditée.
        Tout trou strictement supérieur à max_gap_ms démarre un nouvel épisode.
        """
        actions = list(self._editable_actions())
        if not actions:
            self.visual_episodes = []
            self._refresh_visual_episode_tree()
            return []

        actions = sorted(actions, key=lambda a: int(a.get("at", 0)))
        groups = []
        current = [actions[0]]

        for action in actions[1:]:
            prev = current[-1]
            gap = int(action.get("at", 0)) - int(prev.get("at", 0))
            if gap > int(max_gap_ms):
                groups.append(current)
                current = [action]
            else:
                current.append(action)

        if current:
            groups.append(current)

        old_eps = list(getattr(self, "visual_episodes", []))
        rebuilt = []

        for i, group in enumerate(groups):
            start = int(group[0].get("at", 0))
            end = int(group[-1].get("at", start))

            # Conserver les réglages de l'ancien épisode ayant le plus grand
            # chevauchement temporel avec ce nouveau groupe.
            best_old = None
            best_overlap = -1
            for old in old_eps:
                os = int(old.get("start", 0)) + int(old.get("offset_ms", 0))
                oe = int(old.get("end", os)) + int(old.get("offset_ms", 0))
                overlap = max(0, min(end, oe) - max(start, os))
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_old = old

            if best_old is not None and best_overlap > 0:
                high = int(best_old.get("high", 100))
                low = int(best_old.get("low", 25))
                name = best_old.get("name", f"Épisode {i+1}")
                enabled = bool(best_old.get("enabled", True))
                invert = bool(best_old.get("invert", False))
                start_side = best_old.get("start_side", "high")
                pause_mode = best_old.get("pause_mode", "hold")
                color = best_old.get(
                    "color",
                    self.visual_episode_palette[i % len(self.visual_episode_palette)]
                )
            else:
                high, low = self._default_episode_range(i)
                name = f"Épisode {i+1}"
                enabled = True
                invert = False
                start_side = "high"
                pause_mode = "hold"
                color = self.visual_episode_palette[i % len(self.visual_episode_palette)]

            rebuilt.append({
                "index": i + 1,
                "name": name,
                "start": start,
                "end": end,
                "events": [int(a.get("at", 0)) for a in group],
                "count": len(group),
                "high": int(high),
                "low": int(low),
                "high_deg": int(round(int(high) * 1.8)),
                "low_deg": int(round(int(low) * 1.8)),
                "enabled": enabled,
                "invert": invert,
                "start_side": start_side,
                "offset_ms": 0,
                "pause_mode": pause_mode,
                "color": color,
            })

        self.visual_episodes = rebuilt
        self._refresh_visual_episode_tree()
        return rebuilt

    def _enforce_max_slope_duration(self, actions=None, max_ms=1500):
        """
        Garantit qu'aucune montée/descente directe entre deux positions
        différentes ne dure plus de max_ms.

        Si un grand trou existe entre deux positions différentes, on garde
        la position précédente à plat puis on effectue la transition seulement
        pendant les max_ms précédant le point suivant.
        """
        if actions is None:
            actions = self._editable_actions()

        if not actions or len(actions) < 2:
            return 0

        max_ms = max(1, int(max_ms))

        # Nettoyer et trier les actions.
        clean = []
        for a in actions:
            try:
                clean.append({
                    **a,
                    "at": max(0, int(a.get("at", 0))),
                    "pos": max(0, min(100, int(a.get("pos", 0))))
                })
            except Exception:
                continue

        clean.sort(key=lambda a: int(a["at"]))

        # Éliminer les doublons temporels : garder le dernier.
        unique = []
        for a in clean:
            if unique and int(unique[-1]["at"]) == int(a["at"]):
                unique[-1] = a
            else:
                unique.append(a)

        if len(unique) < 2:
            actions[:] = unique
            return 0

        out = [unique[0]]
        inserted = 0

        for nxt in unique[1:]:
            prev = out[-1]
            t0 = int(prev["at"])
            t1 = int(nxt["at"])
            p0 = int(prev["pos"])
            p1 = int(nxt["pos"])
            dt = t1 - t0

            # Une ligne horizontale longue est acceptable.
            # Seules les montées/descentes sont limitées à 1.5 seconde.
            if dt > max_ms and p0 != p1:
                hold_at = t1 - max_ms

                if hold_at > t0:
                    out.append({
                        "at": int(hold_at),
                        "pos": int(p0)
                    })
                    inserted += 1

            out.append(nxt)

        actions[:] = out
        return inserted

    def _context_delete_point(self):
        if self.track_locked_var.get():
            return

        event = self.track_context_event
        if event is None:
            return

        actions = self._editable_actions()
        if not actions:
            return

        if self.track_context_kind == "overview":
            idx = self._overview_find_nearest_action(event.x, event.y)
        else:
            idx = self._track_edit_find_nearest(event.x, event.y)

        if idx is None or not (0 <= idx < len(actions)):
            return

        removed = actions.pop(idx)
        self._rebuild_episodes_from_action_gaps(1500)
        self.status_var.set(
            "Point supprimé : %d ms / position %d — "
            "tout trou > 1,5 s devient un nouvel épisode."
            % (
                int(removed.get("at", 0)),
                int(removed.get("pos", 0))
            )
        )
        self.draw_all()

    def _trim_before_stable_from_menu(self):
        removed, first_start = self._trim_before_first_sustained_group()

        if removed:
            self._rebuild_episodes_from_action_gaps(1500)
            self.status_var.set(
                f"Faux départ supprimé : {removed} point(s) retiré(s) "
                f"avant {self._format_time(first_start)}."
            )
            self.draw_all()
        elif first_start is not None:
            self.status_var.set(
                "Aucun faux départ avant le premier épisode stable."
            )
        else:
            self.status_var.set(
                "Impossible d'identifier un épisode stable."
            )

    def _clean_leading_anomalies_from_menu(self):
        sparse_points = self._remove_leading_sparse_block(
            scan_window_ms=45000,
            abnormal_gap_ms=1200,
            stable_min_actions=30,
            stable_window_ms=5000,
        )

        groups, points = self._remove_leading_track_anomalies(
            max_gap_ms=1500,
            min_duration_ms=7000,
            min_actions=24,
            min_density_hz=2.2,
        )

        total = sparse_points + points

        if total:
            self._rebuild_episodes_from_action_gaps(1500)
            self.status_var.set(
                f"Nettoyage début : {total} point(s) supprimé(s)."
            )
            self.draw_all()
        else:
            self.status_var.set(
                "Nettoyage début : aucune anomalie détectée."
            )

    def _context_shift_track(self, delta_ms):
        if self.track_locked_var.get():
            return

        actions = self._editable_actions()
        if not actions:
            return

        delta_ms = int(delta_ms)
        first_at = min(int(a.get("at", 0)) for a in actions)
        if first_at + delta_ms < 0:
            delta_ms = -first_at

        for action in actions:
            action["at"] = max(0, int(action.get("at", 0)) + delta_ms)

        self.track_sync_total_ms += delta_ms
        sign = "+" if self.track_sync_total_ms >= 0 else ""
        self.track_sync_offset_ms_var.set(
            f"Offset : {sign}{self.track_sync_total_ms} ms"
        )

        self.status_var.set(
            "Décalage global : %+d ms" % self.track_sync_total_ms
        )
        self.draw_all()

    def _context_reset_shift(self):
        if self.track_locked_var.get():
            return

        current = int(self.track_sync_total_ms)
        if not current:
            return

        actions = self._editable_actions()
        for action in actions:
            action["at"] = max(0, int(action.get("at", 0)) - current)

        self.track_sync_total_ms = 0
        self.track_sync_offset_ms_var.set("Offset : +0 ms")
        self.status_var.set("Offset global réinitialisé.")
        self.draw_all()

    def _context_edit_episode(self):
        if not self.visual_episodes:
            return

        at, _ = self._context_xy_to_action()
        if at is None:
            return

        chosen = None
        for ep in self.visual_episodes:
            if int(ep["start"]) <= int(at) <= int(ep["end"]):
                chosen = ep
                break

        if chosen is None:
            return

        try:
            self.visual_episode_tree.selection_set(str(chosen["index"]))
            self.visual_episode_tree.focus(str(chosen["index"]))
            self.visual_episode_tree.see(str(chosen["index"]))
            self._on_visual_episode_selected()
            self.status_var.set(
                f"Édition de {chosen.get('name', 'Épisode ' + str(chosen['index']))}"
            )
        except Exception:
            pass

    def _editable_actions(self):
        data = self.preview_data if self.preview_data else self.original_data
        if not data or not isinstance(data, dict):
            return []
        actions = data.get("actions", [])
        return actions if isinstance(actions, list) else []

    def _track_edit_time_window(self):
        duration_ms = 0
        try:
            if self.current_video:
                d = self.mpv.duration()
                if d:
                    duration_ms = int(float(d) * 1000)
        except Exception:
            pass

        if duration_ms <= 0:
            actions = self._editable_actions()
            if actions:
                duration_ms = max(int(a.get("at", 0)) for a in actions)

        duration_ms = max(1, duration_ms)

        try:
            z0 = float(self.zoom_start)
            z1 = float(self.zoom_end)
        except Exception:
            z0, z1 = 0.0, 1.0

        start_ms = int(duration_ms * max(0.0, min(1.0, z0)))
        end_ms = int(duration_ms * max(0.0, min(1.0, z1)))
        if end_ms <= start_ms:
            end_ms = start_ms + 1
        return start_ms, end_ms

    def _track_edit_xy_to_action(self, x, y):
        canvas = self.graph
        w = max(2, int(canvas.winfo_width()))
        h = max(2, int(canvas.winfo_height()))
        left, right, top, bottom = 42, 10, 12, 24
        usable_w = max(1, w - left - right)
        usable_h = max(1, h - top - bottom)
        start_ms, end_ms = self._track_edit_time_window()

        ratio_x = max(0.0, min(1.0, (x - left) / usable_w))
        at = int(round(start_ms + ratio_x * (end_ms - start_ms)))

        ratio_y = max(0.0, min(1.0, (y - top) / usable_h))
        pos = int(round(100.0 * (1.0 - ratio_y)))
        return at, max(0, min(100, pos))

    def _track_edit_action_to_xy(self, action):
        canvas = self.graph
        w = max(2, int(canvas.winfo_width()))
        h = max(2, int(canvas.winfo_height()))
        left, right, top, bottom = 42, 10, 12, 24
        usable_w = max(1, w - left - right)
        usable_h = max(1, h - top - bottom)
        start_ms, end_ms = self._track_edit_time_window()

        at = int(action.get("at", 0))
        pos = max(0, min(100, int(action.get("pos", 0))))

        x = left + ((at - start_ms) / max(1, end_ms - start_ms)) * usable_w
        y = top + (1.0 - pos / 100.0) * usable_h
        return x, y

    def _track_edit_find_nearest(self, x, y):
        actions = self._editable_actions()
        if not actions:
            return None

        start_ms, end_ms = self._track_edit_time_window()
        best = None
        best_d2 = None

        for i, action in enumerate(actions):
            at = int(action.get("at", 0))
            if at < start_ms or at > end_ms:
                continue
            px, py = self._track_edit_action_to_xy(action)
            d2 = (px - x) ** 2 + (py - y) ** 2
            if best_d2 is None or d2 < best_d2:
                best_d2 = d2
                best = i

        if best is not None and best_d2 <= self.track_edit_pick_radius ** 2:
            return best
        return None

    def _clear_track_edit_selection(self):
        self.track_edit_selected_index = None
        self.track_edit_dragging = False
        try:
            self.draw_all()
        except Exception:
            pass

    def _navigate_graph_from_event(self, event):
        """
        Mode lecteur vidéo : la trame devient une barre de navigation.
        Clic gauche ou clic-glisser horizontal = seek immédiat dans mpv.
        La lecture continue si elle était en lecture; une vidéo en pause
        reste en pause. Aucun point de la trame n'est modifié.
        """
        w = max(2, self.graph.winfo_width())
        left, right = 42, 10
        usable = max(1, w - left - right)
        ratio = max(0.0, min(1.0, (event.x - left) / usable))
        start_ms, end_ms = self._track_edit_time_window()
        at = int(round(start_ms + ratio * max(1, end_ms - start_ms)))

        # Limite à la durée réelle lorsque mpv la connaît.
        try:
            duration_s = self.mpv.duration()
            if duration_s and duration_s > 0:
                at = max(0, min(at, int(round(duration_s * 1000.0))))
            else:
                at = max(0, at)
        except Exception:
            at = max(0, at)

        try:
            self.mpv.seek_absolute(at / 1000.0)
            self.playhead_ms = at

            # Met à jour aussi le curseur général immédiatement.
            try:
                duration_s = self.mpv.duration()
                if duration_s and duration_s > 0:
                    self._updating_seek_scale = True
                    self.seek_scale.set(
                        max(0, min(1000, (at / 1000.0) / duration_s * 1000.0))
                    )
                    self._updating_seek_scale = False
            except Exception:
                self._updating_seek_scale = False

            self.draw_playheads_only()
        except Exception:
            pass
        return "break"

    def _navigate_overview_from_event(self, event):
        w = max(2, self.overview.winfo_width())
        left, right = 42, 10
        usable = max(1, w - left - right)
        ratio = max(0.0, min(1.0, (event.x - left) / usable))
        at = int(round(ratio * max(1, self._full_duration())))
        try:
            self.mpv.seek_absolute(at / 1000.0)
            self.playhead_ms = at
            self.draw_all()
        except Exception:
            pass
        return "break"

    def _update_track_mode_cursor(self, _event=None):
        """Adapte le curseur de la trame au mode sélectionné."""
        try:
            mode = self.track_edit_mode_var.get()
            cursor = {
                "points": "crosshair",
                "shift": "fleur",
                "navigate": "hand2",
                "lecteur vidéo": "hand2",
            }.get(mode, "")
            self.graph.configure(cursor=cursor)
        except Exception:
            pass

    def _mode_graph_press(self, event):
        mode = self.track_edit_mode_var.get()

        # Le mode lecteur reste utilisable même si l'édition de la trame est verrouillée.
        if mode in ("navigate", "lecteur vidéo"):
            return self._navigate_graph_from_event(event)

        if self.track_locked_var.get():
            return "break"

        if mode == "shift":
            actions = self._editable_actions()
            if not actions:
                return "break"
            self.track_sync_drag_start_x = int(event.x)
            self.track_sync_drag_base_times = [
                int(a.get("at", 0)) for a in actions
            ]
            self.track_sync_drag_base_offset = int(self.track_sync_total_ms)
            return "break"

        return self._track_edit_press(event)

    def _mode_graph_drag(self, event):
        mode = self.track_edit_mode_var.get()

        if mode in ("navigate", "lecteur vidéo"):
            return self._navigate_graph_from_event(event)

        if self.track_locked_var.get():
            return "break"

        if mode == "shift":
            if self.track_sync_drag_start_x is None or self.track_sync_drag_base_times is None:
                return "break"

            actions = self._editable_actions()
            if len(actions) != len(self.track_sync_drag_base_times):
                return "break"

            w = max(2, self.graph.winfo_width())
            usable = max(1, w - 52)
            start_ms, end_ms = self._track_edit_time_window()
            span = max(1, end_ms - start_ms)

            dx = int(event.x) - int(self.track_sync_drag_start_x)
            delta = int(round((dx / usable) * span))

            min_base = min(self.track_sync_drag_base_times)
            if min_base + delta < 0:
                delta = -min_base

            for action, base_at in zip(actions, self.track_sync_drag_base_times):
                action["at"] = max(0, base_at + delta)

            self.track_sync_total_ms = self.track_sync_drag_base_offset + delta
            sign = "+" if self.track_sync_total_ms >= 0 else ""
            self.track_sync_offset_ms_var.set(
                f"Offset : {sign}{self.track_sync_total_ms} ms"
            )
            self.draw_all()
            return "break"

        return self._track_edit_drag(event)

    def _mode_graph_release(self, event):
        mode = self.track_edit_mode_var.get()
        if mode == "shift":
            self.track_sync_drag_start_x = None
            self.track_sync_drag_base_times = None
            return "break"
        if mode in ("navigate", "lecteur vidéo"):
            return self._navigate_graph_from_event(event)
        return self._track_edit_release(event)

    def _mode_overview_press(self, event):
        if self.track_locked_var.get():
            return "break"

        mode = self.track_edit_mode_var.get()

        if mode == "navigate":
            return self._navigate_overview_from_event(event)

        if mode == "shift":
            actions = self._editable_actions()
            if not actions:
                return "break"
            self.track_sync_drag_start_x = int(event.x)
            self.track_sync_drag_base_times = [
                int(a.get("at", 0)) for a in actions
            ]
            self.track_sync_drag_base_offset = int(self.track_sync_total_ms)
            return "break"

        return self._overview_edit_press(event)

    def _mode_overview_drag(self, event):
        if self.track_locked_var.get():
            return "break"

        mode = self.track_edit_mode_var.get()

        if mode == "navigate":
            return self._navigate_overview_from_event(event)

        if mode == "shift":
            if self.track_sync_drag_start_x is None or self.track_sync_drag_base_times is None:
                return "break"

            actions = self._editable_actions()
            if len(actions) != len(self.track_sync_drag_base_times):
                return "break"

            w = max(2, self.overview.winfo_width())
            usable = max(1, w - 52)
            duration = max(1, self._full_duration())

            dx = int(event.x) - int(self.track_sync_drag_start_x)
            delta = int(round((dx / usable) * duration))

            min_base = min(self.track_sync_drag_base_times)
            if min_base + delta < 0:
                delta = -min_base

            for action, base_at in zip(actions, self.track_sync_drag_base_times):
                action["at"] = max(0, base_at + delta)

            self.track_sync_total_ms = self.track_sync_drag_base_offset + delta
            sign = "+" if self.track_sync_total_ms >= 0 else ""
            self.track_sync_offset_ms_var.set(
                f"Offset : {sign}{self.track_sync_total_ms} ms"
            )
            self.draw_all()
            return "break"

        return self._overview_edit_drag(event)

    def _mode_overview_release(self, event):
        mode = self.track_edit_mode_var.get()
        if mode == "shift":
            self.track_sync_drag_start_x = None
            self.track_sync_drag_base_times = None
            return "break"
        if mode == "navigate":
            return self._navigate_overview_from_event(event)
        return self._overview_edit_release(event)

    def _track_edit_press(self, event):
        if not self.track_edit_enabled_var.get():
            return
        idx = self._track_edit_find_nearest(event.x, event.y)
        self.track_edit_selected_index = idx
        self.track_edit_dragging = idx is not None

        if idx is not None:
            actions = self._editable_actions()
            a = actions[idx]
            self.status_var.set(
                "Point sélectionné : %d ms / position %d"
                % (int(a["at"]), int(a["pos"]))
            )

        try:
            self.draw_all()
        except Exception:
            pass

    def _track_edit_drag(self, event):
        if not self.track_edit_enabled_var.get():
            return
        if not self.track_edit_dragging or self.track_edit_selected_index is None:
            return

        actions = self._editable_actions()
        idx = self.track_edit_selected_index
        if not (0 <= idx < len(actions)):
            return

        at, pos = self._track_edit_xy_to_action(event.x, event.y)

        if idx > 0:
            at = max(at, int(actions[idx - 1].get("at", 0)) + 1)
        if idx < len(actions) - 1:
            at = min(at, int(actions[idx + 1].get("at", at + 1)) - 1)

        actions[idx]["at"] = int(at)
        actions[idx]["pos"] = int(pos)

        self.status_var.set(
            "Édition : %d ms / position %d" % (at, pos)
        )

        try:
            self.draw_all()
        except Exception:
            pass

    def _track_edit_release(self, event):
        if not self.track_edit_enabled_var.get():
            return
        self.track_edit_dragging = False

        # Si le déplacement crée un trou > 1,5 s, le groupe suivant
        # devient automatiquement un nouvel épisode.
        self._rebuild_episodes_from_action_gaps(1500)
        try:
            self.draw_all()
        except Exception:
            pass

    def _track_edit_add(self, event):
        if not self.track_edit_enabled_var.get():
            return

        actions = self._editable_actions()
        if actions is None:
            return

        at, pos = self._track_edit_xy_to_action(event.x, event.y)
        actions.append({"at": int(at), "pos": int(pos)})
        actions.sort(key=lambda a: int(a.get("at", 0)))

        self.track_edit_selected_index = min(
            range(len(actions)),
            key=lambda i: abs(int(actions[i].get("at", 0)) - at)
        )

        self.status_var.set(
            "Point ajouté : %d ms / position %d" % (at, pos)
        )
        try:
            self.draw_all()
        except Exception:
            pass

    def _track_edit_delete(self, event):
        if not self.track_edit_enabled_var.get():
            return

        actions = self._editable_actions()
        idx = self._track_edit_find_nearest(event.x, event.y)
        if idx is None or not (0 <= idx < len(actions)):
            return

        removed = actions.pop(idx)
        self._rebuild_episodes_from_action_gaps(1500)
        self.track_edit_selected_index = None
        self.status_var.set(
            "Point supprimé : %d ms / position %d — "
            "tout trou > 1,5 s devient un nouvel épisode."
            % (
                int(removed.get("at", 0)),
                int(removed.get("pos", 0))
            )
        )

        try:
            self.draw_all()
        except Exception:
            pass

    def draw_all(self):
        self.draw_overview()
        self.draw_detail()
        try:
            if self.track_edit_selected_index is not None:
                actions = self._editable_actions()
                idx = self.track_edit_selected_index
                if 0 <= idx < len(actions):
                    x, y = self._track_edit_action_to_xy(actions[idx])
                    self.graph.create_oval(
                        x - 6, y - 6, x + 6, y + 6,
                        outline="#ffffff",
                        width=2
                    )
        except Exception:
            pass


    def draw_playheads_only(self):
        """Met à jour uniquement les curseurs de lecture, sans redessiner les trames.

        Le redessin complet à 10 Hz était coûteux sur les longs funscripts :
        grilles, épisodes et milliers de segments étaient recréés à chaque tick.
        """
        duration = self._full_duration()

        # Vue globale.
        c = self.overview
        c.delete("playhead")
        w = max(30, c.winfo_width()); h = max(80, c.winfo_height())
        ml, mr, mt, mb = 42, 10, 10, 24
        gw = max(1, w - ml - mr); gh = max(1, h - mt - mb)
        self._draw_playhead(c, ml, mt, gw, gh, 0, duration)

        # Vue détaillée.
        c = self.graph
        c.delete("playhead")
        w = max(40, c.winfo_width()); h = max(120, c.winfo_height())
        ml, mr, mt, mb = 48, 12, 12, 34
        gw = max(1, w - ml - mr); gh = max(1, h - mt - mb)
        start_t = duration * self.zoom_start
        end_t = duration * self.zoom_end
        if end_t <= start_t:
            end_t = start_t + 1
        self._draw_playhead(c, ml, mt, gw, gh, start_t, end_t)

    def _all_actions(self):
        out = []
        if self.original_data:
            out.extend(self.original_data.get("actions", []))
        if self.preview_data:
            out.extend(self.preview_data.get("actions", []))
        return out

    def _full_duration(self):
        actions = self._all_actions()
        action_duration = max((int(a["at"]) for a in actions), default=0)
        event_duration = max(
            [0] +
            [int(x) for x in self.detected_visual_times_ms] +
            [int(x) for x in self.detected_line_times_ms]
        )
        video_duration = 0
        try:
            d = self.mpv.duration() if hasattr(self, "mpv") else None
            if d:
                video_duration = int(float(d) * 1000)
        except Exception:
            pass
        return max(1, action_duration, event_duration, video_duration)

    def _draw_grid(self, c, ml, mt, gw, gh, start_t, end_t, compact=False):
        for p in (0,25,50,75,100):
            y = mt + gh - (p/100.0)*gh
            c.create_line(ml,y,ml+gw,y,fill=self.COL_GRID)
            c.create_text(ml-7,y,text=str(p),fill=self.COL_MUTED,anchor="e",font=("Sans",8))
        steps = 6 if compact else 10
        span = max(1, end_t-start_t)
        for i in range(steps+1):
            r=i/steps
            x=ml+r*gw
            t=start_t+r*span
            c.create_line(x,mt,x,mt+gh,fill=self.COL_GRID)
            if not compact or i in (0,steps//2,steps):
                c.create_text(x,mt+gh+14,text=self._format_time(t),fill=self.COL_MUTED,font=("Sans",8))

    def _draw_deadtime_bands(self,c,actions,ml,mt,gw,gh,start_t,end_t):
        if not actions: return
        threshold=int(globals().get("DEADTIME_MS",900))
        span=max(1,end_t-start_t)
        for a,b in zip(actions,actions[1:]):
            a_t,b_t=int(a["at"]),int(b["at"])
            if b_t-a_t < threshold or b_t < start_t or a_t > end_t:
                continue
            x1=ml+((max(a_t,start_t)-start_t)/span)*gw
            x2=ml+((min(b_t,end_t)-start_t)/span)*gw
            c.create_rectangle(x1,mt,x2,mt+gh,fill="#30171b",outline="")

    def _draw_actions(self,c,data,color,width,ml,mt,gw,gh,start_t,end_t,max_points=12000):
        if not data:
            return

        acts = sorted(
            data.get("actions", []),
            key=lambda a: int(a.get("at", 0))
        )
        visible = [
            a for a in acts
            if start_t <= int(a.get("at", 0)) <= end_t
        ]

        if len(visible) > max_points:
            step = max(1, len(visible) // max_points)
            visible = visible[::step]

        if not visible:
            return

        span = max(1, end_t - start_t)
        max_gap_ms = 1500
        segment = []

        def draw_segment(seg):
            if len(seg) < 2:
                return
            pts = []
            for a in seg:
                x = ml + ((int(a.get("at", 0)) - start_t) / span) * gw
                y = mt + gh - (int(a.get("pos", 0)) / 100.0) * gh
                pts.extend((x, y))
            if len(pts) >= 4:
                c.create_line(
                    *pts,
                    fill=color,
                    width=width,
                    smooth=False
                )

        for a in visible:
            if segment:
                gap = int(a.get("at", 0)) - int(segment[-1].get("at", 0))
                if gap > max_gap_ms:
                    draw_segment(segment)
                    segment = []
            segment.append(a)

        draw_segment(segment)

    def _draw_detection_events(self, c, ml, mt, gw, gh, start_t, end_t):
        if not self.show_detection_events.get():
            return
        span = max(1, end_t - start_t)

        series = [
            (self.detected_visual_times_ms, "#ff4fd8", 0),
            (self.detected_line_times_ms, "#37e6ff", 1),
        ]
        for events, color, lane in series:
            if not events:
                continue
            y1 = mt + 3 + lane * 7
            y2 = min(mt + gh, y1 + 12)
            for t in events:
                t = int(t)
                if t < start_t or t > end_t:
                    continue
                x = ml + ((t - start_t) / span) * gw
                c.create_line(x, y1, x, y2, fill=color, width=2)

        combined = self._combined_cockhero_events()
        if combined:
            y1 = mt + gh - 14
            for t in combined:
                t = int(t)
                if t < start_t or t > end_t:
                    continue
                x = ml + ((t - start_t) / span) * gw
                c.create_line(x, y1, x, mt + gh, fill="#ffffff", width=1)

    def _draw_playhead(self,c,ml,mt,gw,gh,start_t,end_t):
        if self.playhead_ms < start_t or self.playhead_ms > end_t:
            return
        span=max(1,end_t-start_t)
        x=ml+((self.playhead_ms-start_t)/span)*gw
        c.create_line(x,mt,x,mt+gh,fill="#ffffff",width=2,tags=("playhead",))
        c.create_text(x+4,mt+3,text=self._format_time(self.playhead_ms),fill="#ffffff",anchor="nw",font=("Sans",8),tags=("playhead",))

    def draw_overview(self):
        c=self.overview
        c.delete("all")
        w=max(30,c.winfo_width()); h=max(80,c.winfo_height())
        ml,mr,mt,mb=42,10,10,24
        gw=max(1,w-ml-mr); gh=max(1,h-mt-mb)
        duration=self._full_duration()
        self._draw_grid(c,ml,mt,gw,gh,0,duration,compact=True)

        # Bandes d'épisodes : lecture immédiate de la structure complète.
        try:
            if self.visual_episodes and duration > 0:
                for ep in self.visual_episodes:
                    x1 = ml + (max(0, ep["start"]) / duration) * gw
                    x2 = ml + (min(duration, ep["end"]) / duration) * gw
                    color = ep.get("color", "#4dabf7")
                    band_h = min(20, max(12, int(gh * 0.12)))
                    c.create_rectangle(
                        x1, mt, x2, mt + band_h,
                        fill=color,
                        outline=color,
                        width=1
                    )
                    label = "E%d   %d/%d" % (
                        ep["index"],
                        int(ep.get("high",100)),
                        int(ep.get("low",0))
                    )
                    c.create_text(
                        (x1+x2)/2.0,
                        mt + band_h/2.0,
                        text=label,
                        fill="#ffffff",
                        anchor="center",
                        font=("Sans",9,"bold")
                    )
                    c.create_line(
                        x1, mt, x1, mt+gh,
                        fill=color,
                        width=2
                    )
        except Exception:
            pass

        if self.original_data:
            self._draw_deadtime_bands(c,self.original_data.get("actions",[]),ml,mt,gw,gh,0,duration)
        if self.show_original.get():
            self._draw_actions(c,self.original_data,self.COL_BLUE,1,ml,mt,gw,gh,0,duration,8000)
        if self.show_preview.get():
            self._draw_actions(c,self.preview_data,self.COL_GREEN,2,ml,mt,gw,gh,0,duration,8000)
        x1=ml+self.zoom_start*gw; x2=ml+self.zoom_end*gw
        c.create_rectangle(x1,mt,x2,mt+gh,outline="#f2f5f7",width=1)
        self._draw_detection_events(c,ml,mt,gw,gh,0,duration)
        self._draw_playhead(c,ml,mt,gw,gh,0,duration)

        try:
            if self.track_edit_selected_index is not None:
                actions = self._editable_actions()
                idx = self.track_edit_selected_index
                if 0 <= idx < len(actions):
                    x, y = self._overview_action_to_xy(actions[idx])
                    c.create_oval(
                        x - 7, y - 7, x + 7, y + 7,
                        outline="#ffffff",
                        width=3
                    )
        except Exception:
            pass

        c.create_rectangle(ml,mt,ml+gw,mt+gh,outline=self.COL_BORDER)

    def draw_detail(self):
        c=self.graph
        c.delete("all")
        w=max(40,c.winfo_width()); h=max(120,c.winfo_height())
        ml,mr,mt,mb=48,12,12,34
        gw=max(1,w-ml-mr); gh=max(1,h-mt-mb)
        duration=self._full_duration()
        start_t=duration*self.zoom_start; end_t=duration*self.zoom_end
        if end_t<=start_t: end_t=start_t+1
        self._draw_grid(c,ml,mt,gw,gh,start_t,end_t,compact=False)
        try:
            for ep in self.visual_episodes:
                es=int(ep["start"]); ee=int(ep["end"])
                if ee < start_t or es > end_t:
                    continue
                a=max(start_t,es); b=min(end_t,ee)
                x1=ml+((a-start_t)/max(1,end_t-start_t))*gw
                x2=ml+((b-start_t)/max(1,end_t-start_t))*gw
                col=ep.get("color","#4dabf7")
                # Ne pas masquer les mouvements : la couleur de l'épisode
                # reste uniquement dans un bandeau supérieur.
                band_h = min(18, max(12, int(gh * 0.10)))
                c.create_rectangle(
                    x1, mt,
                    x2, mt + band_h,
                    fill=col,
                    outline=col,
                    width=1
                )

                c.create_text(
                    (x1 + x2) / 2.0,
                    mt + band_h / 2.0,
                    text="E%d" % ep["index"],
                    fill="#ffffff",
                    font=("Sans", 9, "bold")
                )

                # Séparateurs d'épisodes sur toute la hauteur, sans fond coloré.
                c.create_line(
                    x1, mt,
                    x1, mt + gh,
                    fill=col,
                    width=2
                )
        except Exception:
            pass
        if self.original_data:
            self._draw_deadtime_bands(c,self.original_data.get("actions",[]),ml,mt,gw,gh,start_t,end_t)
        if self.show_original.get():
            self._draw_actions(c,self.original_data,self.COL_BLUE,1,ml,mt,gw,gh,start_t,end_t,16000)
        if self.show_preview.get():
            self._draw_actions(c,self.preview_data,self.COL_GREEN,2,ml,mt,gw,gh,start_t,end_t,16000)
        self._draw_detection_events(c,ml,mt,gw,gh,start_t,end_t)
        self._draw_playhead(c,ml,mt,gw,gh,start_t,end_t)
        c.create_rectangle(ml,mt,ml+gw,mt+gh,outline=self.COL_BORDER)

    # ------------------------------------------------------------
    # NAVIGATION
    # ------------------------------------------------------------
    def zoom_reset(self):
        self.zoom_start=0.0; self.zoom_end=1.0; self.draw_all()

    def zoom_in(self):
        center=(self.zoom_start+self.zoom_end)/2
        width=max(0.002,(self.zoom_end-self.zoom_start)*0.60)
        self.zoom_start=max(0,center-width/2); self.zoom_end=min(1,center+width/2)
        self.draw_all()

    def zoom_out(self):
        old_width=max(0.002,self.zoom_end-self.zoom_start)
        if old_width>=0.999:
            self.zoom_start,self.zoom_end=0.0,1.0
            self.draw_all()
            return
        new_width=min(1.0,old_width*1.65)
        if new_width>=0.999:
            self.zoom_start,self.zoom_end=0.0,1.0
            self.draw_all()
            return
        center=(self.zoom_start+self.zoom_end)/2.0
        ns=center-new_width/2.0
        ne=ns+new_width
        if ns<0:
            ne-=ns
            ns=0.0
        if ne>1:
            ns-=ne-1.0
            ne=1.0
        self.zoom_start=max(0.0,ns)
        self.zoom_end=min(1.0,ne)
        self.draw_all()

    def _zoom_around(self,ratio,direction):
        old=self.zoom_end-self.zoom_start
        new=max(0.002,old*0.72) if direction>0 else min(1.0,old*1.38)
        anchor=self.zoom_start+ratio*old
        ns=anchor-ratio*new; ne=ns+new
        if ns<0: ne-=ns; ns=0
        if ne>1: ns-=ne-1; ne=1
        self.zoom_start=max(0,ns); self.zoom_end=min(1,ne); self.draw_all()

    def _mouse_zoom(self,event):
        self._zoom_around(max(0,min(1,event.x/max(1,self.graph.winfo_width()))),1 if event.delta>0 else -1)

    def _mouse_zoom_linux(self,direction,event):
        self._zoom_around(max(0,min(1,event.x/max(1,self.graph.winfo_width()))),direction)

    def _detail_press(self,event):
        self.dragging_detail=True; self.detail_drag_x=event.x; self.detail_drag_window=(self.zoom_start,self.zoom_end)

    def _detail_drag(self,event):
        if not self.dragging_detail: return
        w=max(1,self.graph.winfo_width()); dx=event.x-self.detail_drag_x
        s0,e0=self.detail_drag_window; width=e0-s0; shift=-(dx/w)*width
        ns,ne=s0+shift,e0+shift
        if ns<0: ne-=ns; ns=0
        if ne>1: ns-=ne-1; ne=1
        self.zoom_start=max(0,ns); self.zoom_end=min(1,ne); self.draw_all()

    def _detail_release(self,event):
        self.dragging_detail=False

    def _overview_xy_to_action(self, x, y):
        c = self.overview
        w = max(30, c.winfo_width())
        h = max(80, c.winfo_height())
        ml, mr, mt, mb = 42, 10, 10, 24
        gw = max(1, w - ml - mr)
        gh = max(1, h - mt - mb)
        duration = max(1, self._full_duration())

        ratio_x = max(0.0, min(1.0, (x - ml) / gw))
        at = int(round(ratio_x * duration))

        ratio_y = max(0.0, min(1.0, (y - mt) / gh))
        pos = int(round(100.0 * (1.0 - ratio_y)))
        return at, max(0, min(100, pos))

    def _overview_action_to_xy(self, action):
        c = self.overview
        w = max(30, c.winfo_width())
        h = max(80, c.winfo_height())
        ml, mr, mt, mb = 42, 10, 10, 24
        gw = max(1, w - ml - mr)
        gh = max(1, h - mt - mb)
        duration = max(1, self._full_duration())

        at = max(0, min(duration, int(action.get("at", 0))))
        pos = max(0, min(100, int(action.get("pos", 0))))

        x = ml + (at / duration) * gw
        y = mt + (1.0 - pos / 100.0) * gh
        return x, y

    def _overview_find_nearest_action(self, x, y):
        actions = self._editable_actions()
        if not actions:
            return None

        best = None
        best_d2 = None
        radius = max(10, int(self.track_edit_pick_radius))

        for i, action in enumerate(actions):
            px, py = self._overview_action_to_xy(action)
            d2 = (px - x) ** 2 + (py - y) ** 2
            if best_d2 is None or d2 < best_d2:
                best = i
                best_d2 = d2

        if best is not None and best_d2 <= radius ** 2:
            return best
        return None

    def _overview_edit_press(self, event):
        # Maj+clic reste réservé à la navigation/lecture.
        if event.state & 0x0001:
            return self._overview_click(event)

        if not self.track_edit_enabled_var.get():
            return self._overview_click(event)

        idx = self._overview_find_nearest_action(event.x, event.y)
        self.track_edit_selected_index = idx
        self.track_edit_dragging = idx is not None

        if idx is None:
            return

        actions = self._editable_actions()
        a = actions[idx]
        self.status_var.set(
            "TRAME COMPLÈTE — point %d : %d ms / position %d"
            % (idx + 1, int(a.get("at", 0)), int(a.get("pos", 0)))
        )
        self.draw_all()

    def _overview_edit_drag(self, event):
        if not self.track_edit_enabled_var.get():
            return
        if not self.track_edit_dragging or self.track_edit_selected_index is None:
            return

        actions = self._editable_actions()
        idx = self.track_edit_selected_index
        if not (0 <= idx < len(actions)):
            return

        at, pos = self._overview_xy_to_action(event.x, event.y)

        if idx > 0:
            at = max(at, int(actions[idx - 1].get("at", 0)) + 1)
        if idx < len(actions) - 1:
            at = min(at, int(actions[idx + 1].get("at", at + 1)) - 1)

        actions[idx]["at"] = int(at)
        actions[idx]["pos"] = int(pos)

        self.status_var.set(
            "TRAME COMPLÈTE — édition : %d ms / position %d" % (at, pos)
        )
        self.draw_all()

    def _overview_edit_release(self, event):
        self.track_edit_dragging = False

    def _overview_edit_add(self, event):
        if not self.track_edit_enabled_var.get():
            return

        actions = self._editable_actions()
        at, pos = self._overview_xy_to_action(event.x, event.y)
        actions.append({"at": int(at), "pos": int(pos)})
        actions.sort(key=lambda a: int(a.get("at", 0)))

        self.track_edit_selected_index = min(
            range(len(actions)),
            key=lambda i: abs(int(actions[i].get("at", 0)) - at)
        )

        self.status_var.set(
            "TRAME COMPLÈTE — point ajouté : %d ms / position %d" % (at, pos)
        )
        self.draw_all()

    def _overview_edit_delete(self, event):
        if not self.track_edit_enabled_var.get():
            return

        actions = self._editable_actions()
        idx = self._overview_find_nearest_action(event.x, event.y)
        if idx is None or not (0 <= idx < len(actions)):
            return

        removed = actions.pop(idx)
        self.track_edit_selected_index = None
        self.status_var.set(
            "TRAME COMPLÈTE — point supprimé : %d ms / position %d"
            % (int(removed.get("at", 0)), int(removed.get("pos", 0)))
        )
        self.draw_all()

    def _overview_click(self,event):
        self._overview_recenter(event.x)
        # also seek video to clicked point
        duration=self._full_duration()
        ratio=max(0,min(1,event.x/max(1,self.overview.winfo_width())))
        self.mpv.seek_absolute((ratio*duration)/1000.0)

    def _overview_drag(self,event):
        self._overview_recenter(event.x)

    def _overview_recenter(self,x):
        w=max(1,self.overview.winfo_width()); ratio=max(0,min(1,x/w))
        width=self.zoom_end-self.zoom_start
        if width>=0.999: width=0.25
        ns=ratio-width/2; ne=ratio+width/2
        if ns<0: ne-=ns; ns=0
        if ne>1: ns-=ne-1; ne=1
        self.zoom_start=max(0,ns); self.zoom_end=min(1,ne); self.draw_all()

    def _format_time(self,ms):
        total=max(0,int(ms//1000))
        h=total//3600; m=(total%3600)//60; s=total%60
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def gui_main():
    app = FunscriptGUI()
    app.mainloop()


if __name__ == "__main__":
    gui_main()
