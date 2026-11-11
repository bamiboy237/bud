//! The one place colors are spelled: palettes, blends, brand ramp, depth downgrade.

use ratatui::style::{Color, Modifier, Style};

/// A palette color before depth handling.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Rgb(pub u8, pub u8, pub u8);

impl Rgb {
    const fn hex(value: u32) -> Self {
        Rgb((value >> 16) as u8, (value >> 8) as u8, value as u8)
    }
}

/// Mixes `other` into `base` by `alpha`, truncating each channel like the reference theme does.
pub fn blend(base: Rgb, other: Rgb, alpha: f32) -> Rgb {
    let mix = |a: u8, b: u8| (a as f32 + (b as f32 - a as f32) * alpha) as u8;
    Rgb(
        mix(base.0, other.0),
        mix(base.1, other.1),
        mix(base.2, other.2),
    )
}

/// Named blend stops shared by every derived token.
pub mod stop {
    pub const HOVER: f32 = 0.10;
    pub const TRACK: f32 = 0.22;
    pub const KEYLINE: f32 = 0.35;
    pub const FENCE: f32 = 0.50;
    pub const EFFECT: f32 = 0.55;
    pub const MUTED: f32 = 0.60;
    pub const TINT: f32 = 0.66;
    pub const CARET: f32 = 0.70;
}

/// Brand ramp: the ember of the cigar, light to dark. Fixed on every theme.
const BRAND_LIGHT: Rgb = Rgb::hex(0xfbbf24);
const BRAND: Rgb = Rgb::hex(0xf59e0b);
const BRAND_DARK: Rgb = Rgb::hex(0xea580c);

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum ColorDepth {
    TrueColor,
    Indexed256,
    /// `NO_COLOR` or a dumb terminal: modifiers only.
    None,
}

impl ColorDepth {
    pub fn detect() -> Self {
        if std::env::var_os("NO_COLOR").is_some_and(|v| !v.is_empty()) {
            return ColorDepth::None;
        }
        match std::env::var("COLORTERM").as_deref() {
            Ok("truecolor") | Ok("24bit") => ColorDepth::TrueColor,
            _ => match std::env::var("TERM").as_deref() {
                Ok("dumb") => ColorDepth::None,
                Ok(t) if t.contains("256color") || t.contains("kitty") => ColorDepth::Indexed256,
                _ => ColorDepth::TrueColor,
            },
        }
    }

    fn paint(self, c: Rgb) -> Color {
        match self {
            ColorDepth::TrueColor => Color::Rgb(c.0, c.1, c.2),
            ColorDepth::Indexed256 => Color::Indexed(nearest_256(c)),
            ColorDepth::None => Color::Reset,
        }
    }
}

fn nearest_256(c: Rgb) -> u8 {
    const LEVELS: [u8; 6] = [0, 95, 135, 175, 215, 255];
    let near = |v: u8| {
        LEVELS
            .iter()
            .enumerate()
            .min_by_key(|(_, l)| (**l as i32 - v as i32).abs())
            .map(|(i, _)| i as u8)
            .unwrap_or(0)
    };
    let (ri, gi, bi) = (near(c.0), near(c.1), near(c.2));
    let cube = Rgb(
        LEVELS[ri as usize],
        LEVELS[gi as usize],
        LEVELS[bi as usize],
    );
    let avg = ((c.0 as u32 + c.1 as u32 + c.2 as u32) / 3) as i32;
    let gi_step = ((avg - 8 + 5) / 10).clamp(0, 23);
    let gv = (8 + gi_step * 10) as u8;
    let dist = |a: Rgb, b: Rgb| {
        let d = |x: u8, y: u8| (x as i32 - y as i32).pow(2);
        d(a.0, b.0) + d(a.1, b.1) + d(a.2, b.2)
    };
    if dist(Rgb(gv, gv, gv), c) < dist(cube, c) {
        232 + gi_step as u8
    } else {
        16 + 36 * ri + 6 * gi + bi
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Polarity {
    Dark,
    Light,
}

/// Raw slots of one named palette.
struct Palette {
    name: &'static str,
    polarity: Polarity,
    bg: Rgb,
    fg: Rgb,
    primary: Rgb,
    secondary: Rgb,
    accent: Rgb,
    success: Rgb,
    warning: Rgb,
    error: Rgb,
    muted: Rgb,
    dim: Rgb,
    border: Rgb,
    surface: Rgb,
    selection_bg: Rgb,
    input_selection_bg: Rgb,
    block_cursor_bg: Rgb,
    block_cursor_fg: Rgb,
    input_cursor_bg: Rgb,
    scrollbar: Rgb,
    scrollbar_bg: Rgb,
    panel_lighten_1: Rgb,
    text_success: Rgb,
    text_error: Rgb,
}

const BUD_DARK: Palette = Palette {
    name: "bud-dark",
    polarity: Polarity::Dark,
    bg: Rgb::hex(0x09090b),
    fg: Rgb::hex(0xf4f4f5),
    primary: Rgb::hex(0xf4f4f5),
    secondary: Rgb::hex(0xd4d4d8),
    accent: Rgb::hex(0xffffff),
    success: Rgb::hex(0xa1a1aa),
    warning: Rgb::hex(0xe4e4e7),
    error: Rgb::hex(0xf87171),
    muted: Rgb::hex(0x9c9c9d),
    dim: Rgb::hex(0x52525b),
    border: Rgb::hex(0x3f3f46),
    surface: Rgb::hex(0x18181b),
    selection_bg: Rgb::hex(0x27272a),
    input_selection_bg: Rgb::hex(0x1d1d21),
    block_cursor_bg: Rgb::hex(0xf4f4f5),
    block_cursor_fg: Rgb::hex(0x09090b),
    input_cursor_bg: Rgb::hex(0xf4f4f5),
    scrollbar: Rgb::hex(0x52525b),
    scrollbar_bg: Rgb::hex(0x060608),
    panel_lighten_1: Rgb::hex(0x464648),
    text_success: Rgb::hex(0xd4d4d8),
    text_error: Rgb::hex(0xfca5a5),
};

const BUD_LIGHT: Palette = Palette {
    name: "bud-light",
    polarity: Polarity::Light,
    bg: Rgb::hex(0xfafafa),
    fg: Rgb::hex(0x18181b),
    primary: Rgb::hex(0x18181b),
    secondary: Rgb::hex(0x71717a),
    accent: Rgb::hex(0x18181b),
    success: Rgb::hex(0x3f3f46),
    warning: Rgb::hex(0x52525b),
    error: Rgb::hex(0xdc2626),
    muted: Rgb::hex(0x646464),
    dim: Rgb::hex(0xa1a1aa),
    border: Rgb::hex(0xd4d4d8),
    surface: Rgb::hex(0xf4f4f5),
    selection_bg: Rgb::hex(0xe4e4e7),
    input_selection_bg: Rgb::hex(0xf0f0f2),
    block_cursor_bg: Rgb::hex(0x18181b),
    block_cursor_fg: Rgb::hex(0xfafafa),
    input_cursor_bg: Rgb::hex(0x5b5b5d),
    scrollbar: Rgb::hex(0xa1a1aa),
    scrollbar_bg: Rgb::hex(0xf4f4f5),
    panel_lighten_1: Rgb::hex(0xffffff),
    text_success: Rgb::hex(0x323237),
    text_error: Rgb::hex(0xb01e1e),
};

/// Every resolved color token. Views read these and never spell a color.
#[derive(Clone, Debug)]
pub struct Theme {
    pub name: &'static str,
    pub polarity: Polarity,
    pub depth: ColorDepth,
    pub bg: Color,
    pub fg: Color,
    pub primary: Color,
    pub secondary: Color,
    pub accent: Color,
    pub success: Color,
    pub warning: Color,
    pub error: Color,
    pub muted: Color,
    pub dim: Color,
    pub border: Color,
    pub surface: Color,
    pub popup_border: Color,
    pub selection_bg: Color,
    pub input_selection_bg: Color,
    pub input_cursor_bg: Color,
    pub scrollbar: Color,
    pub scrollbar_bg: Color,
    pub panel_lighten_1: Color,
    pub text_success: Color,
    pub text_error: Color,
    pub keyline: Color,
    pub fence_border: Color,
    pub hover_bg: Color,
    pub track: Color,
    pub effect_verb: Color,
    pub effect_message: Color,
    pub md_accent: Color,
    pub md_link: Color,
    pub md_code_inline: Color,
    pub brand: Color,
    pub brand_ramp: [Color; 3],
    block_cursor_bg: Color,
    block_cursor_fg: Color,
}

impl Theme {
    pub fn named(name: &str, depth: ColorDepth) -> Option<Self> {
        match name {
            "bud-dark" | "dark" => Some(Self::build(&BUD_DARK, depth)),
            "bud-light" | "light" => Some(Self::build(&BUD_LIGHT, depth)),
            _ => None,
        }
    }

    pub fn names() -> [&'static str; 2] {
        [BUD_DARK.name, BUD_LIGHT.name]
    }

    pub fn dark(depth: ColorDepth) -> Self {
        Self::build(&BUD_DARK, depth)
    }

    pub fn light(depth: ColorDepth) -> Self {
        Self::build(&BUD_LIGHT, depth)
    }

    /// Dark unless the terminal reports a light background through `COLORFGBG`.
    pub fn auto(depth: ColorDepth) -> Self {
        let light = std::env::var("COLORFGBG")
            .ok()
            .and_then(|v| v.rsplit(';').next().and_then(|b| b.parse::<u8>().ok()))
            .is_some_and(|bg| bg == 7 || bg == 15);
        if light {
            Self::light(depth)
        } else {
            Self::dark(depth)
        }
    }

    fn build(p: &Palette, depth: ColorDepth) -> Self {
        let paint = |c: Rgb| depth.paint(c);
        let contrast = match p.polarity {
            Polarity::Dark => Rgb(255, 255, 255),
            Polarity::Light => Rgb(0, 0, 0),
        };
        let tint = |c: Rgb| blend(contrast, c, stop::TINT);
        let code = match p.polarity {
            Polarity::Dark => p.text_success,
            Polarity::Light => blend(p.text_success, Rgb(0, 0, 0), 0.2),
        };
        Theme {
            name: p.name,
            polarity: p.polarity,
            depth,
            bg: paint(p.bg),
            fg: paint(p.fg),
            primary: paint(p.primary),
            secondary: paint(p.secondary),
            accent: paint(p.accent),
            success: paint(p.success),
            warning: paint(p.warning),
            error: paint(p.error),
            muted: paint(p.muted),
            dim: paint(p.dim),
            border: paint(p.border),
            surface: paint(p.surface),
            popup_border: paint(blend(p.bg, p.fg, stop::FENCE)),
            selection_bg: paint(p.selection_bg),
            input_selection_bg: paint(p.input_selection_bg),
            input_cursor_bg: paint(p.input_cursor_bg),
            scrollbar: paint(p.scrollbar),
            scrollbar_bg: paint(p.scrollbar_bg),
            panel_lighten_1: paint(p.panel_lighten_1),
            text_success: paint(p.text_success),
            text_error: paint(p.text_error),
            keyline: paint(blend(p.bg, p.fg, stop::KEYLINE)),
            fence_border: paint(blend(p.bg, p.fg, stop::FENCE)),
            hover_bg: paint(blend(p.bg, contrast, stop::HOVER)),
            track: paint(blend(p.bg, contrast, stop::TRACK)),
            effect_verb: paint(blend(p.bg, p.primary, stop::EFFECT)),
            effect_message: paint(blend(p.bg, p.fg, stop::EFFECT)),
            md_accent: paint(tint(p.primary)),
            md_link: paint(tint(p.secondary)),
            md_code_inline: paint(code),
            brand: paint(BRAND),
            brand_ramp: [paint(BRAND_LIGHT), paint(BRAND), paint(BRAND_DARK)],
            block_cursor_bg: paint(p.block_cursor_bg),
            block_cursor_fg: paint(p.block_cursor_fg),
        }
    }

    pub fn is_dark(&self) -> bool {
        self.polarity == Polarity::Dark
    }

    /// Whole-screen base style.
    pub fn base(&self) -> Style {
        Style::new().fg(self.fg).bg(self.bg)
    }

    pub fn text(&self, color: Color) -> Style {
        Style::new().fg(color)
    }

    pub fn fg_style(&self) -> Style {
        self.text(self.fg)
    }

    pub fn muted_style(&self) -> Style {
        self.text(self.muted)
    }

    pub fn dim_style(&self) -> Style {
        self.text(self.dim)
    }

    pub fn bold(&self, color: Color) -> Style {
        self.text(color).add_modifier(Modifier::BOLD)
    }

    /// The focused row bar of any list: block-cursor colors, bold, full row width.
    pub fn list_cursor(&self) -> Style {
        Style::new()
            .fg(self.block_cursor_fg)
            .bg(self.block_cursor_bg)
            .add_modifier(Modifier::BOLD)
    }

    /// The caret cell in the composer.
    pub fn caret(&self) -> Style {
        Style::new().fg(self.bg).bg(self.input_cursor_bg)
    }

    /// Ping-pong brand ramp for the loading label, indexed by a running counter.
    pub fn ramp_at(&self, i: usize) -> Color {
        let n = self.brand_ramp.len();
        let period = 2 * (n - 1);
        let k = i % period;
        let idx = if k < n { k } else { period - k };
        self.brand_ramp[idx]
    }

    /// Hairline separators and quote bars.
    pub fn keyline_style(&self) -> Style {
        self.text(self.keyline)
    }
}

impl Theme {
    /// Interactive accent: keycap labels.
    pub fn primary_style(&self) -> Style {
        self.text(self.primary)
    }
}
