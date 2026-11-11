//! The dog with a cigar: 24x12 dots, a 26-beat smoke cycle, four poses.
//!
//! Frame 0 is stored as row lists; every later pose is a `(remove, add)` delta on it.

use super::{Art, Dot, Dots};

const START: [&[i16]; 12] = [
    &[],
    &[],
    &[11, 12, 13, 14, 15, 16, 17, 18, 19, 20],
    &[9, 10, 13, 14, 17, 18, 21, 22],
    &[9, 10, 12, 13, 18, 19, 21, 22],
    &[9, 10, 12, 15, 16, 19],
    &[4, 5, 12, 15, 16, 19, 23],
    &[3, 6, 12, 19, 22, 23],
    &[2, 7, 12, 13, 14, 15, 17, 18, 19, 20, 21, 22, 23],
    &[2, 8, 9, 10, 12, 13, 14, 15, 16, 17, 18, 19],
    &[8, 9, 10, 11, 13, 14, 17, 18, 19],
    &[],
];

type Delta = (&'static [Dot], &'static [Dot]);

const NONE: &[Dot] = &[];
const PLUME_START: &[Dot] = &[(23, 5), (23, 4)];
const PLUME_JUMP: &[Dot] = &[(23, 2)];
const PLUME_FLARE: &[Dot] = &[(22, 6), (22, 1)];
const PLUME_CAP: &[Dot] = &[(21, 0), (22, 0), (23, 0)];
const PLUME_COOL: &[Dot] = &[(22, 6), (23, 5), (23, 4)];
const PLUME_DROP_HEAD: &[Dot] = &[(23, 2), (22, 1)];
const PLUME_THIN: &[Dot] = &[(21, 0), (23, 0)];
const EYES: &[Dot] = &[(13, 4), (18, 4)];
const TAIL_TIP: &[Dot] = &[(2, 8), (2, 9)];
const TAIL_TIP_OUT: &[Dot] = &[(1, 7), (1, 8)];
const FLAP_BOT: &[Dot] = &[(9, 5), (10, 5)];
const FLAP_UP: &[Dot] = &[(9, 2), (10, 2)];
const LAST_WISP: &[Dot] = &[(22, 0)];

/// Twenty-six beats: the puff rises, holds, dissolves; micro-fidgets close the cycle.
const CYCLE: [Delta; 26] = [
    (NONE, PLUME_START),
    (NONE, NONE),
    (NONE, PLUME_JUMP),
    (NONE, NONE),
    (NONE, PLUME_FLARE),
    (NONE, NONE),
    (NONE, PLUME_CAP),
    (NONE, NONE),
    (NONE, NONE),
    (PLUME_COOL, NONE),
    (NONE, NONE),
    (PLUME_DROP_HEAD, NONE),
    (NONE, NONE),
    (PLUME_THIN, NONE),
    (NONE, NONE),
    (LAST_WISP, NONE),
    (NONE, NONE),
    (EYES, NONE),
    (NONE, EYES),
    (NONE, NONE),
    (TAIL_TIP, TAIL_TIP_OUT),
    (NONE, NONE),
    (TAIL_TIP_OUT, TAIL_TIP),
    (NONE, NONE),
    (FLAP_BOT, FLAP_UP),
    (FLAP_UP, FLAP_BOT),
];

const THINK_MARK: &[Dot] = &[(14, 0), (16, 0), (15, 1)];
const ERROR_REMOVE: &[Dot] = &[(13, 4), (18, 4), (23, 6)];
const ERROR_ADD: &[Dot] = &[(13, 5), (18, 5)];
const EMBER: &[Dot] = &[(23, 6)];

fn apply(dots: &Dots, remove: &[Dot], add: &[Dot]) -> Dots {
    let mut out = dots.clone();
    for dot in remove {
        out.remove(dot);
    }
    out.extend(add.iter().copied());
    out
}

pub fn start() -> Dots {
    let mut dots = Dots::new();
    for (y, row) in START.iter().enumerate() {
        for &x in *row {
            dots.insert((x, y as i16));
        }
    }
    dots
}

/// The idle cycle as the pose after each beat; the last pose equals frame 0.
pub fn cycle_poses() -> Vec<Dots> {
    let mut dots = start();
    CYCLE
        .iter()
        .map(|(remove, add)| {
            dots = apply(&dots, remove, add);
            dots.clone()
        })
        .collect()
}

pub fn smoke_pose() -> Dots {
    apply(
        &start(),
        NONE,
        &[
            (21, 0),
            (22, 0),
            (23, 0),
            (22, 1),
            (23, 2),
            (23, 4),
            (23, 5),
            (22, 6),
        ],
    )
}

pub fn think_pose() -> Dots {
    apply(
        &start(),
        &[(14, 3), (17, 3), (13, 4), (18, 4)],
        &[(14, 0), (16, 0), (15, 1)],
    )
}

pub fn art() -> Art {
    let base = start();
    let idle = cycle_poses();
    Art {
        width: 24,
        height: 12,
        start: base.clone(),
        work: idle.clone(),
        idle,
        // Post-step indices where the dog reads composed: risen cloud, fresh idle, settled blink.
        rest_frames: vec![7, 16, 19],
        waiting_mark: THINK_MARK.iter().copied().collect(),
        error: apply(&base, ERROR_REMOVE, ERROR_ADD),
        error_pulse: EMBER.iter().copied().collect(),
    }
}
