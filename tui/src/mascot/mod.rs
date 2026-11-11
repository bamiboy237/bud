//! Mascot engine: a dot grid, a looping cycle of poses, and four session states.
//!
//! The engine owns no clock. The app hands `tick` the current instant, at most one
//! 160 ms beat is applied per call, and the return value says whether any dot changed.

mod ant;
mod braille;
mod dog;

use std::collections::BTreeSet;
use std::time::{Duration, Instant};

pub type Dot = (i16, i16);
pub type Dots = BTreeSet<Dot>;

pub const FRAME_INTERVAL: Duration = Duration::from_millis(160);
const REST_MIN_S: f64 = 5.0;
const REST_MAX_S: f64 = 20.0;
const REST_CHANCE: f64 = 0.25;
const WAITING_TOGGLE: Duration = Duration::from_millis(500);
const ERROR_PULSE: Duration = Duration::from_millis(1000);
/// Columns reserved for the art, so the text beside it never moves between frames.
pub const COLS: usize = 14;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    Dog,
    Ant,
}

impl Kind {
    pub fn name(self) -> &'static str {
        match self {
            Kind::Dog => "dog",
            Kind::Ant => "ant",
        }
    }

    pub fn parse(text: &str) -> Option<Self> {
        match text.trim().to_ascii_lowercase().as_str() {
            "dog" => Some(Kind::Dog),
            "ant" => Some(Kind::Ant),
            _ => None,
        }
    }

    pub fn other(self) -> Self {
        match self {
            Kind::Dog => Kind::Ant,
            Kind::Ant => Kind::Dog,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum State {
    Idle,
    Working,
    Waiting,
    Error,
}

impl State {
    pub const ALL: [State; 4] = [State::Idle, State::Working, State::Waiting, State::Error];

    pub fn name(self) -> &'static str {
        match self {
            State::Idle => "idle",
            State::Working => "working",
            State::Waiting => "waiting",
            State::Error => "error",
        }
    }
}

/// Everything one mascot is: its start pose, its two loops, and its two frozen poses.
pub struct Art {
    pub width: i16,
    pub height: i16,
    pub start: Dots,
    /// Poses after each beat of the idle loop; the loop wraps back to `start`.
    pub idle: Vec<Dots>,
    /// Beats after which the idle loop may rest (post-step indices).
    pub rest_frames: Vec<usize>,
    /// Poses of the working loop; it never rests.
    pub work: Vec<Dots>,
    /// Dots that blink on the held pose while waiting for approval.
    pub waiting_mark: Dots,
    /// Frozen pose after a failed run.
    pub error: Dots,
    /// Dots that pulse faintly on the error pose.
    pub error_pulse: Dots,
}

impl Art {
    pub fn for_kind(kind: Kind) -> Art {
        match kind {
            Kind::Dog => dog::art(),
            Kind::Ant => ant::art(),
        }
    }
}

/// xorshift64: enough randomness for rest lengths, no dependency.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> f64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        (self.0 >> 11) as f64 / (1u64 << 53) as f64
    }

    fn uniform(&mut self, lo: f64, hi: f64) -> f64 {
        lo + (hi - lo) * self.next()
    }
}

pub struct Mascot {
    kind: Kind,
    art: Art,
    state: State,
    dots: Dots,
    index: usize,
    cycles: u32,
    resume_frame: Option<usize>,
    pause_until: Option<Instant>,
    next_step: Option<Instant>,
    next_toggle: Option<Instant>,
    phase_on: bool,
    animate: bool,
    rng: Rng,
}

impl Mascot {
    pub fn new(kind: Kind, seed: u64, animate: bool) -> Self {
        let art = Art::for_kind(kind);
        let dots = art.start.clone();
        Mascot {
            kind,
            art,
            state: State::Idle,
            dots,
            index: 0,
            cycles: 0,
            resume_frame: None,
            pause_until: None,
            next_step: None,
            next_toggle: None,
            phase_on: false,
            animate,
            rng: Rng(seed | 1),
        }
    }

    pub fn kind(&self) -> Kind {
        self.kind
    }

    pub fn state(&self) -> State {
        self.state
    }

    pub fn is_cycle_complete(&self) -> bool {
        self.cycles > 0
    }

    pub fn dots(&self) -> &Dots {
        &self.dots
    }

    pub fn set_kind(&mut self, kind: Kind) {
        if kind == self.kind {
            return;
        }
        let state = self.state;
        *self = Mascot::new(kind, self.rng.0, self.animate);
        self.set_state(state);
    }

    /// Enters a state at its defined pose. A no-op when the state is unchanged.
    pub fn set_state(&mut self, state: State) -> bool {
        if state == self.state {
            return false;
        }
        self.state = state;
        self.pause_until = None;
        self.resume_frame = None;
        self.next_step = None;
        self.next_toggle = None;
        self.index = 0;
        match state {
            State::Waiting => {
                self.dots = self.art.start.clone();
                self.dots.extend(self.art.waiting_mark.iter().copied());
                self.phase_on = true;
            }
            State::Error => {
                self.dots = self.art.error.clone();
                self.phase_on = false;
            }
            State::Idle | State::Working => self.dots = self.art.start.clone(),
        }
        true
    }

    /// Rows of braille, one string per cell row.
    pub fn rows(&self) -> Vec<String> {
        let dots: Vec<Dot> = self.dots.iter().copied().collect();
        braille::render(&dots, self.art.width, self.art.height)
    }

    pub fn tick(&mut self, now: Instant) -> bool {
        if !self.animate {
            return false;
        }
        match self.state {
            State::Idle | State::Working => self.step_cycle(now),
            State::Waiting | State::Error => self.step_toggle(now),
        }
    }

    fn step_cycle(&mut self, now: Instant) -> bool {
        if self.pause_until.is_some_and(|until| now < until) {
            return false;
        }
        let Some(due) = self.next_step else {
            self.next_step = Some(now + FRAME_INTERVAL);
            return false;
        };
        if now < due {
            return false;
        }
        let frames = if self.state == State::Working {
            &self.art.work
        } else {
            &self.art.idle
        };
        let next = frames[self.index % frames.len()].clone();
        let changed = next != self.dots;
        self.dots = next;
        self.index += 1;
        if self.index >= frames.len() {
            self.index = 0;
            self.cycles += 1;
        }
        self.next_step = Some(now + FRAME_INTERVAL);
        self.maybe_rest(now);
        changed
    }

    /// Always rest after a wrap; 25 % at the composed poses. After a rest, one full
    /// cycle back to the rest frame must pass before the next one.
    fn maybe_rest(&mut self, now: Instant) {
        if self.state == State::Working {
            return;
        }
        if let Some(frame) = self.resume_frame {
            if self.index == frame {
                self.resume_frame = None;
            } else {
                return;
            }
        }
        let resting = self.index == 0
            || (self.art.rest_frames.contains(&self.index) && self.rng.next() < REST_CHANCE);
        if resting {
            let pause = self.rng.uniform(REST_MIN_S, REST_MAX_S);
            self.pause_until = Some(now + Duration::from_secs_f64(pause));
            self.resume_frame = Some(self.index);
        }
    }

    fn step_toggle(&mut self, now: Instant) -> bool {
        let (interval, delta) = if self.state == State::Waiting {
            (WAITING_TOGGLE, &self.art.waiting_mark)
        } else {
            (ERROR_PULSE, &self.art.error_pulse)
        };
        let Some(due) = self.next_toggle else {
            self.next_toggle = Some(now + interval);
            return false;
        };
        if now < due {
            return false;
        }
        self.phase_on = !self.phase_on;
        if self.phase_on {
            self.dots.extend(delta.iter().copied());
        } else {
            for dot in delta {
                self.dots.remove(dot);
            }
        }
        self.next_toggle = Some(now + interval);
        true
    }
}

/// Parses `#`/`.` rows into dots (x = column, y = row).
pub fn parse_grid(rows: &[&str]) -> Dots {
    let mut dots = Dots::new();
    for (y, row) in rows.iter().enumerate() {
        for (x, c) in row.chars().enumerate() {
            if c == '#' {
                dots.insert((x as i16, y as i16));
            }
        }
    }
    dots
}

/// A static render of one pose, for the gallery and goldens.
pub fn pose_rows(kind: Kind, state: State) -> Vec<String> {
    let mut mascot = Mascot::new(kind, 7, false);
    mascot.set_state(state);
    mascot.rows()
}

/// Rows of the rest pose with the given dots, for art checks.
pub fn rows_of(kind: Kind, dots: &Dots) -> Vec<String> {
    let art = Art::for_kind(kind);
    let flat: Vec<Dot> = dots.iter().copied().collect();
    braille::render(&flat, art.width, art.height)
}

/// Side-by-side poses of every state for each mascot, as plain text.
pub fn gallery(kinds: &[Kind]) -> String {
    let mut out = String::new();
    for kind in kinds {
        out.push_str(&format!("{}\n", kind.name()));
        let poses: Vec<Vec<String>> = State::ALL.iter().map(|s| pose_rows(*kind, *s)).collect();
        let labels: Vec<String> = State::ALL
            .iter()
            .map(|s| format!("{:<width$}", s.name(), width = COLS + 8))
            .collect();
        out.push_str(&format!("  {}\n", labels.concat()));
        let rows = poses.first().map_or(0, Vec::len);
        for row in 0..rows {
            let line: String = poses
                .iter()
                .map(|p| format!("{:<width$}", p[row], width = COLS + 8))
                .collect();
            out.push_str(&format!("  {}\n", line.trim_end()));
        }
        out.push('\n');
    }
    out
}

pub fn dog_cycle_poses() -> Vec<Dots> {
    dog::cycle_poses()
}

pub fn dog_start() -> Dots {
    dog::start()
}

pub fn dog_smoke_pose() -> Dots {
    dog::smoke_pose()
}

pub fn dog_think_pose() -> Dots {
    dog::think_pose()
}
