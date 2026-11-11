//! bud-tui: the terminal chat client for a running bud core.

use anyhow::{anyhow, Context, Result};
use bud_tui::app::{App, AppMsg};
use bud_tui::client::Core;
use bud_tui::mascot::{self, Kind};
use bud_tui::protocol::{ModelInfo, DEFAULT_URL};
use bud_tui::theme::{ColorDepth, Theme};
use bud_tui::{auth, run, sse, terminal};
use clap::Parser;
use std::path::PathBuf;
use tokio::sync::mpsc;

#[derive(Parser)]
#[command(name = "bud-tui", version, about = "Terminal chat client for bud")]
struct Args {
    /// Session id to attach to
    session: Option<String>,
    /// Start a separate conversation
    #[arg(long)]
    new: bool,
    /// Server origin
    #[arg(long, default_value = DEFAULT_URL)]
    url: String,
    /// Mascot: dog or ant
    #[arg(long, default_value = "dog")]
    mascot: String,
    /// Palette: dark, light or auto
    #[arg(long, default_value = "auto")]
    theme: String,
    /// Pin this model id for the session
    #[arg(long)]
    model: Option<String>,
    /// Open the model picker at startup
    #[arg(long)]
    pick_model: bool,
    /// Data directory holding auth/owner.token (default: DATA_DIR or ./data)
    #[arg(long)]
    data_dir: Option<PathBuf>,
    /// Do not capture the mouse (wheel scroll and click-to-expand turn off)
    #[arg(long)]
    no_mouse: bool,
    /// Print every mascot in every state and exit
    #[arg(long)]
    gallery: bool,
}

fn default_model(models: &[ModelInfo]) -> Option<String> {
    models
        .iter()
        .find(|m| m.tier == "fast" && m.configured)
        .or_else(|| models.iter().find(|m| m.configured))
        .or_else(|| models.first())
        .map(|m| m.id.clone())
}

async fn start(args: Args) -> Result<()> {
    let kind = Kind::parse(&args.mascot).ok_or_else(|| anyhow!("--mascot takes dog or ant"))?;
    let depth = ColorDepth::detect();
    let theme = Theme::named(&args.theme, depth).unwrap_or_else(|| Theme::auto(depth));

    let base = auth::check_origin(&args.url)?;
    let token = auth::owner_token(args.data_dir.as_deref())?;
    let core = Core::new(base.clone(), &token)?;
    let runtime = core.runtime().await.map_err(|e| {
        anyhow!(
            "Cannot reach bud at {}: {e}. Start it with `bud serve`.",
            args.url
        )
    })?;
    let core = core.with_scope(&runtime.scope, &token)?;

    let wants_model = args.model.is_some() || (args.new && args.session.is_none());
    let model = if wants_model {
        match args.model.clone() {
            Some(m) => Some(m),
            None => default_model(&core.models().await.unwrap_or_default()),
        }
    } else {
        None
    };
    let session = core
        .open_session(args.session.as_deref(), args.new, model.as_deref())
        .await
        .map_err(|e| anyhow!("Could not open a session: {e}"))?;
    let snapshot = core
        .snapshot(&session)
        .await
        .map_err(|e| anyhow!("Could not read the session: {e}"))?;

    let mut app = App::new(theme, kind, model.as_deref().unwrap_or("default"));
    app.mouse = !args.no_mouse;
    app.theme_pref = args.theme.clone();
    if !runtime.approval_mode.is_empty() {
        app.approval_mode = runtime.approval_mode.clone();
    }
    app.load_snapshot(&snapshot);

    let (tx, mut rx) = mpsc::unbounded_channel::<AppMsg>();
    app.attach(core.clone(), tx.clone(), session.clone());
    let (feed_tx, mut feed_rx) = mpsc::unbounded_channel();
    tokio::spawn(sse::follow(
        core.clone(),
        session.clone(),
        snapshot.last_seq,
        feed_tx,
    ));
    let forward = tx.clone();
    tokio::spawn(async move {
        while let Some(feed) = feed_rx.recv().await {
            if forward.send(AppMsg::Feed(feed)).is_err() {
                return;
            }
        }
    });
    if args.pick_model {
        app.run_command("/model");
    }

    let mut term = terminal::enter(app.mouse).context("could not start the terminal")?;
    let result = run::run(&mut app, &mut term, &mut rx).await;
    terminal::leave();
    drop(term);
    result
}

#[tokio::main]
async fn main() {
    let args = Args::parse();
    if args.gallery {
        println!("{}", mascot::gallery(&[Kind::Dog, Kind::Ant]));
        return;
    }
    if let Err(error) = start(args).await {
        terminal::leave();
        eprintln!("bud: {error:#}");
        std::process::exit(1);
    }
}
