enum WelcomeSuggestion: CaseIterable, Identifiable {
    case newsletters, morning, friday

    var id: Self { self }

    var symbol: String {
        switch self {
        case .newsletters: "tray"
        case .morning: "sun.horizon"
        case .friday: "clock.arrow.circlepath"
        }
    }

    var title: String {
        switch self {
        case .newsletters: "Clean up newsletters I never open"
        case .morning: "Pull what actually matters this morning"
        case .friday: "Check Friday at noon if Priya sent the deck"
        }
    }

    var prompt: String {
        switch self {
        case .newsletters:
            "Scan my Gmail for promotional clutter and newsletters I never open. Group them by sender and ask before archiving or unsubscribing."
        case .morning:
            "Check my calendar and recent emails for deadlines, schedule changes, or replies waiting on me. Stay brief and link the original messages."
        case .friday:
            "Check Friday at noon whether Priya replied with the deck. Stay quiet if she already sent it, or draft a quick follow-up for me if not."
        }
    }
}
