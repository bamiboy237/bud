//! Dot grid to braille cells: 2x4 dots per cell, empty cell is a space.

use super::Dot;

pub const BASE: u32 = 0x2800;

fn bit(sub_x: i16, sub_y: i16) -> u32 {
    let dot = if sub_y < 3 {
        sub_y + 1 + 3 * sub_x
    } else {
        7 + sub_x
    };
    1 << (dot - 1)
}

/// Renders dots into rows of braille. Out-of-range dots are dropped, never clamped.
pub fn render(dots: &[Dot], width: i16, height: i16) -> Vec<String> {
    let (cols, rows) = ((width / 2) as usize, (height / 4) as usize);
    let mut cells = vec![vec![0u32; cols]; rows];
    for &(x, y) in dots {
        if (0..width).contains(&x) && (0..height).contains(&y) {
            cells[(y / 4) as usize][(x / 2) as usize] |= bit(x % 2, y % 4);
        }
    }
    cells
        .iter()
        .map(|row| {
            row.iter()
                .map(|&mask| {
                    if mask == 0 {
                        ' '
                    } else {
                        char::from_u32(BASE + mask).unwrap_or(' ')
                    }
                })
                .collect()
        })
        .collect()
}
