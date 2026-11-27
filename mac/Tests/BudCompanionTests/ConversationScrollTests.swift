import Testing
@testable import BudCompanion

struct ConversationScrollTests {
    @Test
    func userScrollCancelsTheInitialJumpAndPausesLayoutFollowing() {
        var follow = ConversationScrollFollow()
        follow.userBeganScrolling()
        let initialScroll = follow.takeInitialScroll()
        #expect(!initialScroll)
        #expect(!follow.followsLayout)

        follow.userMoved(nearBottom: false)
        follow.userEndedScrolling()
        #expect(!follow.isFollowingBottom)
        #expect(!follow.followsLayout)
    }

    @Test
    func scrollingBackToTheBottomResumesFollowingAfterTheGestureEnds() {
        var follow = ConversationScrollFollow()
        let initialScroll = follow.takeInitialScroll()
        let duplicateScroll = follow.takeInitialScroll()
        #expect(initialScroll)
        #expect(!duplicateScroll)
        #expect(follow.followsLayout)

        follow.userBeganScrolling()
        follow.userMoved(nearBottom: false)
        follow.userMoved(nearBottom: true)
        #expect(!follow.followsLayout)
        follow.userEndedScrolling()
        #expect(follow.followsLayout)
    }

    @Test
    func revealingAnOlderMessagePausesFollowingUntilJumpToLatest() {
        var follow = ConversationScrollFollow()
        follow.revealEarlierMessage()
        follow.userMoved(nearBottom: true)
        let initialScroll = follow.takeInitialScroll()
        #expect(!initialScroll)
        #expect(!follow.followsLayout)

        follow.jumpToLatest()
        #expect(follow.followsLayout)
    }

    @Test
    func escapeDismissesTheForegroundControlBeforeAnInspectorOrComposer() {
        let cases: [(Bool, Bool, Bool, Bool, ConversationEscapeTarget)] = [
            (true, true, true, true, .shortcuts),
            (false, true, true, true, .find),
            (false, false, true, true, .queuedEdit),
            (false, false, false, true, .inspector),
            (false, false, false, false, .composer),
        ]
        for (shortcuts, find, queuedEdit, inspector, target) in cases {
            #expect(ConversationEscapeTarget.resolve(
                shortcuts: shortcuts, find: find, queuedEdit: queuedEdit, inspector: inspector
            ) == target)
        }
    }
}
