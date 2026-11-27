import CoreGraphics
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

        follow.geometryChanged(from: geometry(0), to: geometry(100))
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
        follow.geometryChanged(from: geometry(0), to: geometry(100))
        follow.geometryChanged(from: geometry(100), to: geometry(0))
        #expect(!follow.followsLayout)
        follow.userEndedScrolling()
        #expect(follow.followsLayout)
    }

    @Test
    func revealingAnOlderMessagePausesFollowingUntilJumpToLatest() {
        var follow = ConversationScrollFollow()
        follow.revealEarlierMessage()
        follow.geometryChanged(from: geometry(0), to: geometry(12))
        #expect(!follow.showsJumpButton)
        follow.geometryChanged(from: geometry(12), to: geometry(100))
        #expect(follow.showsJumpButton)
        let initialScroll = follow.takeInitialScroll()
        #expect(!initialScroll)
        #expect(!follow.followsLayout)

        follow.jumpToLatest()
        #expect(follow.followsLayout)
    }

    @Test
    func bounceSettlingAfterWheelEndsResumesFollowingAndHidesJump() {
        var follow = ConversationScrollFollow()
        follow.userBeganScrolling()
        follow.geometryChanged(from: geometry(0), to: geometry(25))
        follow.userEndedScrolling()
        #expect(follow.showsJumpButton)
        #expect(!follow.isFollowingBottom)

        follow.geometryChanged(from: geometry(25), to: geometry(24))
        #expect(!follow.showsJumpButton)
        #expect(follow.followsLayout)
    }

    @Test(arguments: [300, 408])
    func shortChatsHideJumpDespiteAnOffsetFromBounce(contentHeight: Int) {
        var follow = ConversationScrollFollow()
        follow.revealEarlierMessage()
        follow.geometryChanged(from: geometry(0), to: geometry(100))
        #expect(follow.showsJumpButton)

        follow.geometryChanged(from: geometry(100), to: geometry(40, contentHeight: CGFloat(contentHeight)))
        #expect(!follow.showsJumpButton)
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

    private func geometry(_ distance: CGFloat, contentHeight: CGFloat = 800) -> ChatScrollGeometry {
        ChatScrollGeometry(content: CGSize(width: 320, height: contentHeight),
                           viewport: CGSize(width: 320, height: 400),
                           offset: contentHeight - 400 - distance, distanceFromBottom: distance)
    }
}
