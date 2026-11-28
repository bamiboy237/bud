import Testing
@testable import BudCompanion

@MainActor
struct WelcomeSuggestionTests {
    @Test
    func dismissingATipAndChoosingAnotherFillsAndFocusesTheDraft() {
        let companion = Companion()
        companion.link = .connected
        #expect(companion.welcomeSuggestions == [.newsletters, .morning, .friday])

        companion.dismissSuggestion(.morning)
        #expect(companion.welcomeSuggestions == [.newsletters, .friday])
        companion.useSuggestion(.morning)
        #expect(companion.draft.isEmpty)

        let initialFocus = companion.focusComposerNonce
        companion.useSuggestion(.friday)
        #expect(companion.draft == "Check Friday at noon whether Priya replied with the deck. Stay quiet if she already sent it, or draft a quick follow-up for me if not.")
        #expect(companion.focusComposerNonce == initialFocus + 1)
        #expect(companion.turns.isEmpty)
        #expect(companion.welcomeSuggestions.isEmpty)

        companion.draft = ""
        #expect(companion.welcomeSuggestions == [.newsletters, .friday])
    }

    @Test
    func tipsOnlyAppearInAnEmptyChatAfterOnboarding() {
        let companion = Companion()
        companion.link = .connected
        companion.needsOnboarding = true
        #expect(companion.welcomeSuggestions.isEmpty)
        companion.needsOnboarding = false

        companion.draft = "a"
        #expect(companion.welcomeSuggestions.isEmpty)
        companion.draft = ""
        #expect(companion.welcomeSuggestions.count == 3)

        companion.apply(snapshot: ["transcript": [["role": "user", "text": "Hi"]]])
        #expect(companion.welcomeSuggestions.isEmpty)
    }
}
