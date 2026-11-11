//! The mascot art is data: the dog port matches the drafted frames and every loop closes.

use bud_tui::mascot::{
    dog_cycle_poses, dog_smoke_pose, dog_start, dog_think_pose, pose_rows, rows_of, Art, Kind,
    State,
};
use std::path::PathBuf;

fn frame_file(name: &str) -> Vec<String> {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/dog-banner")
        .join(name);
    std::fs::read_to_string(path)
        .expect("drafted dog frame")
        .lines()
        .map(str::to_owned)
        .collect()
}

fn padded(rows: Vec<String>) -> Vec<String> {
    rows.into_iter().map(|r| r.trim_end().to_owned()).collect()
}

#[test]
fn dog_cycle_returns_to_frame_zero_and_matches_drafts() {
    let poses = dog_cycle_poses();
    assert_eq!(poses.len(), 26);
    assert_eq!(poses.last(), Some(&dog_start()));
    assert_eq!(poses[6], dog_smoke_pose());
    assert_eq!(dog_start().len(), 79);
    assert_eq!(dog_smoke_pose().len(), 87);
    assert_eq!(dog_think_pose().len(), 78);
    let check = |name: &str, dots| {
        let drawn = padded(rows_of(Kind::Dog, dots));
        let saved: Vec<String> = frame_file(name)
            .into_iter()
            .map(|r| r.trim_end().to_owned())
            .collect();
        assert_eq!(drawn, saved, "{name}");
    };
    let start = dog_start();
    check("idle.txt", &start);
    let smoke = dog_smoke_pose();
    let think = dog_think_pose();
    check("smoke.txt", &smoke);
    check("think.txt", &think);
}

#[test]
fn every_pose_fits_the_reserved_block() {
    for kind in [Kind::Dog, Kind::Ant] {
        let art = Art::for_kind(kind);
        assert_eq!(
            art.idle.last(),
            Some(&art.start),
            "{kind:?} idle loop must close"
        );
        assert!(art.work.len() >= 4);
        for dots in art.idle.iter().chain(art.work.iter()) {
            assert!(dots
                .iter()
                .all(|&(x, y)| (0..24).contains(&x) && (0..12).contains(&y)));
        }
        for state in State::ALL {
            let rows = pose_rows(kind, state);
            assert_eq!(rows.len(), 3);
            assert!(rows.iter().all(|r| r.chars().count() == 12));
            assert!(rows.iter().any(|r| !r.trim().is_empty()));
        }
    }
}
