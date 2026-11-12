//! Slash commands: one registry drives the menu, /help, and dispatch.

pub struct Command {
    pub name: &'static str,
    pub usage: &'static str,
    pub desc: &'static str,
}

pub const COMMANDS: &[Command] = &[
    Command {
        name: "/model",
        usage: "[id]",
        desc: "Switch model",
    },
    Command {
        name: "/theme",
        usage: "[dark|light|auto]",
        desc: "Switch palette",
    },
    Command {
        name: "/mascot",
        usage: "[dog|ant]",
        desc: "Switch mascot",
    },
    Command {
        name: "/mode",
        usage: "[strict|auto]",
        desc: "Set what asks for approval",
    },
    Command {
        name: "/cost",
        usage: "",
        desc: "Estimated tokens this session",
    },
    Command {
        name: "/connect",
        usage: "[gmail|calendar]",
        desc: "Connect an account",
    },
    Command {
        name: "/connections",
        usage: "",
        desc: "Show connected accounts",
    },
    Command {
        name: "/refresh",
        usage: "",
        desc: "Recheck connections",
    },
    Command {
        name: "/clear",
        usage: "",
        desc: "Clear the transcript view",
    },
    Command {
        name: "/status",
        usage: "",
        desc: "Queue, runs, and pending actions",
    },
    Command {
        name: "/pause",
        usage: "",
        desc: "Pause the message queue",
    },
    Command {
        name: "/resume",
        usage: "",
        desc: "Resume the message queue",
    },
    Command {
        name: "/stop",
        usage: "",
        desc: "Interrupt the current run",
    },
    Command {
        name: "/cancel",
        usage: "[message-id]",
        desc: "Cancel a queued message",
    },
    Command {
        name: "/edit",
        usage: "[message-id] [text]",
        desc: "Edit a queued message",
    },
    Command {
        name: "/approve",
        usage: "[id]",
        desc: "Approve the pending action",
    },
    Command {
        name: "/deny",
        usage: "[id]",
        desc: "Deny the pending action",
    },
    Command {
        name: "/answer",
        usage: "[id] [text]",
        desc: "Answer a pending question",
    },
    Command {
        name: "/help",
        usage: "",
        desc: "Show this guide",
    },
    Command {
        name: "/exit",
        usage: "",
        desc: "Leave bud",
    },
    Command {
        name: "/quit",
        usage: "",
        desc: "Leave bud",
    },
];

/// Commands whose name starts with `typed` (a `/` prefix, no spaces), in registry order.
pub fn matching(typed: &str) -> Vec<&'static Command> {
    let typed = typed.to_lowercase();
    let mut found: Vec<&'static Command> = COMMANDS
        .iter()
        .filter(|c| c.name.starts_with(&typed))
        .collect();
    // An exact name leads, so Enter on `/mode` runs /mode and not /model.
    found.sort_by_key(|c| c.name != typed);
    found
}

pub fn help_text() -> String {
    let width = COMMANDS
        .iter()
        .map(|c| format!("{} {}", c.name, c.usage).trim().len())
        .max()
        .unwrap_or(0);
    let mut out = String::from("Commands\n");
    for c in COMMANDS {
        let label = format!("{} {}", c.name, c.usage);
        out.push_str(&format!("  {:<w$}  {}\n", label.trim(), c.desc, w = width));
    }
    out.push_str(
        "\nKeys\n  Enter send · Shift+Enter or Alt+Enter newline · Ctrl+O tools · PageUp/PageDown scroll\n  Esc interrupt or back · Ctrl+C clear or interrupt · Ctrl+D exit · y/n/r answer an approval",
    );
    out
}
