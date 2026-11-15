import Foundation
import Testing
@testable import BudCompanion

struct ApprovalTests {
    private func approval(
        _ tool: String, id: String = "a", run: String? = "r", task: Int? = nil,
        args: [String: Any] = [:], preview: [String: Any]? = nil
    ) throws -> Approval {
        var raw: [String: Any] = ["approval_id": id, "tool": tool, "args": args]
        raw["run_id"] = run
        raw["task_id"] = task
        raw["preview"] = preview
        return try #require(Approval(raw, jobs: [:]))
    }

    @Test
    func actionNamesMatchTheirEffects() throws {
        for (tool, verb, done) in [
            ("archive_email", "Archive", "Archived"),
            ("mark_read", "Mark read", "Marked read"),
            ("send_email", "Send", "Sent"),
            ("GMAIL_SEND_EMAIL", "Send", "Sent"),
            ("draft_reply", "Save draft", "Draft saved"),
            ("GMAIL_CREATE_EMAIL_DRAFT", "Save draft", "Draft saved"),
        ] {
            let item = try approval(tool)
            #expect(item.verb == verb)
            #expect(item.done == done)
        }
        #expect(try approval("unknown_email_tool").verb == "Approve")
    }

    @Test
    func batchExcludesOtherRequestsJobsAndQuestions() throws {
        let selected = try approval("archive_email")
        let sibling = try approval("send_email", id: "b")
        let otherRun = try approval("send_email", id: "c", run: "other")
        let otherTask = try approval("send_email", id: "d", task: 7)
        let question = try approval("ask_user", id: "e")
        let items = [selected, sibling, otherRun, otherTask, question]
        #expect(selected.batch(in: items).map(\.id) == ["a", "b"])
        #expect(question.batch(in: items).isEmpty)
        let legacy = try approval("send_email", run: nil)
        #expect(legacy.batch(in: [legacy, sibling]).isEmpty)
    }

    @Test
    func siblingJobsGroupOnlyWithAnExplicitRequestID() throws {
        let first = try #require(Approval([
            "approval_id": "a", "tool": "archive_email", "args": [:],
            "run_id": "job-run-1", "task_id": 1, "request_id": "owner-request",
        ], jobs: [:]))
        let second = try #require(Approval([
            "approval_id": "b", "tool": "send_email", "args": [:],
            "run_id": "job-run-2", "task_id": 2, "request_id": "owner-request",
        ], jobs: [:]))
        let unrelated = try approval("send_email", id: "c", run: "owner-request")
        #expect(first.batch(in: [first, second, unrelated]).map(\.id) == ["a", "b"])
    }

    @Test @MainActor
    func selectionSurvivesRefreshAndFallsBackWhenDecided() {
        let companion = Companion()
        func snapshot(_ ids: [String], seq: Int) -> [String: Any] {
            ["last_seq": seq, "pending_approvals": ids.map {
                ["approval_id": $0, "tool": "archive_email", "args": [:]]
            }]
        }
        companion.apply(snapshot: snapshot(["a", "b"], seq: 1))
        companion.selectedApprovalID = "b"
        companion.apply(snapshot: snapshot(["a", "b", "c"], seq: 2))
        #expect(companion.selectedApproval?.id == "b")
        companion.apply(snapshot: snapshot(["a", "c"], seq: 3))
        #expect(companion.selectedApproval?.id == "a")
        companion.apply(snapshot: snapshot([], seq: 4))
        #expect(companion.selectedApproval == nil)
    }

    @Test
    func emailPreviewDoesNotReplaceProposedContent() throws {
        let item = try approval("draft_reply", args: ["body": "Proposed reply"], preview: [
            "kind": "email", "subject": "Invoice",
            "messages": [
                ["sender": "billing@example.test", "body": "Original email", "body_format": "plain"],
                ["body": "<p>HTML</p>", "body_text": "HTML", "body_format": "html"],
            ],
        ])
        #expect(item.body == "Proposed reply")
        #expect(item.emailPreview?.messages.first?.body == "Original email")
        #expect(item.emailPreview?.messages.last?.body == "HTML")
        #expect(item.menuTitle == "Save draft: Invoice")
    }

    @Test
    func emailFormattingPreservesTextAndQuotesWithoutMarkdownInterpretation() {
        let paragraphs = EmailParagraph.parse("Hello **Dana**,\r\nFriday works.\r\n\r\n> Quoted line\r\n> Second line\r\n\r\nThanks")
        #expect(paragraphs.count == 3)
        #expect(paragraphs[0].text == "Hello **Dana**,\nFriday works.")
        #expect(!paragraphs[0].quoted)
        #expect(paragraphs[1].text == "Quoted line\nSecond line")
        #expect(paragraphs[1].quoted)
        #expect(paragraphs[2].text == "Thanks")
    }

    @Test @MainActor
    func batchContinuesAfterFailureWithoutAddingNewIDs() async {
        enum Failure: Error { case rejected }
        var sent: [String] = []
        let result = await ApprovalBatch.approve(["a", "b", "c"]) { id in
            sent.append(id)
            if id == "b" { throw Failure.rejected }
            return ["applied": true]
        }
        #expect(sent == ["a", "b", "c"])
        #expect(result.approved == ["a", "c"])
        #expect(Set(result.failures.keys) == ["b"])
    }

    @Test @MainActor
    func batchApprovesEverySelectedIDOnce() async {
        var sent: [String] = []
        let result = await ApprovalBatch.approve(["a", "b"]) {
            sent.append($0)
            return ["applied": true]
        }
        #expect(sent == ["a", "b"])
        #expect(result.approved == ["a", "b"])
        #expect(result.failures.isEmpty)
    }

    @Test @MainActor
    func alreadyDecidedActionsAreNotCountedAsSuccesses() async {
        var sent: [String] = []
        let result = await ApprovalBatch.approve(["a", "b"]) {
            sent.append($0)
            return ["applied": false]
        }
        #expect(sent == ["a", "b"])
        #expect(result.approved.isEmpty)
        #expect(Set(result.failures.keys) == ["a", "b"])
    }

    @Test @MainActor
    func decisionResponsesMustConfirmApplied() async {
        let responses: [[String: Any]] = [[:], ["applied": "true"], ["applied": false]]
        for response in responses {
            var sent: [String] = []
            let result = await ApprovalBatch.approve(["a", "b", "c"]) { id in
                sent.append(id)
                return id == "a" ? ["applied": true] : response
            }
            #expect(sent == ["a", "b", "c"])
            #expect(result.approved == ["a"])
            #expect(Set(result.failures.keys) == ["b", "c"])
        }
    }

    @Test @MainActor
    func cancellationPreventsAnyFurtherApproval() async {
        var sent: [String] = []
        let task = Task {
            await ApprovalBatch.approve(["a", "b"]) { id in
                sent.append(id)
                withUnsafeCurrentTask { $0?.cancel() }
                return ["applied": true]
            }
        }
        let result = await task.value
        #expect(sent == ["a"])
        #expect(result.approved == ["a"])
        #expect(result.cancelled)
        #expect(result.failures.isEmpty)
    }

    @Test @MainActor
    func retryIncludesOnlyFailedItemsStillPendingInTheSameRequest() async throws {
        let a = try approval("archive_email", id: "a")
        let b = try approval("send_email", id: "b")
        let denied = try approval("send_email", id: "denied")
        let arrivedLater = try approval("send_email", id: "new")
        let unrelated = try approval("send_email", id: "other", run: "other-request")
        let first = await ApprovalBatch.approve([a.id, b.id, denied.id]) { id in
            ["applied": id == a.id]
        }
        let pending = [b, arrivedLater, unrelated]
        let retry = b.retryBatch(in: pending, failures: first.failures)
        #expect(retry.map(\.id) == ["b"])
        var sent: [String] = []
        let second = await ApprovalBatch.approve(retry.map(\.id)) {
            sent.append($0)
            return ["applied": true]
        }
        #expect(sent == ["b"])
        #expect(second.approved == ["b"])
        #expect(second.failures.isEmpty)
    }

    @Test @MainActor
    func failedApprovalsRemainVisibleUntilResolvedOrWithdrawn() throws {
        let companion = Companion()
        func state(_ ids: [String], seq: Int) -> [String: Any] {
            ["last_seq": seq, "pending_approvals": ids.map {
                ["approval_id": $0, "tool": "archive_email", "args": [:], "run_id": "request"]
            }]
        }
        companion.apply(snapshot: state(["a", "b", "c"], seq: 1))
        companion.completeBatch(.init(approved: ["a", "c"], failures: ["b": "Connection failed"]),
                                ids: ["a", "b", "c"])
        #expect(companion.approvals.map(\.id) == ["b"])
        #expect(companion.selectedApproval?.id == "b")
        #expect(companion.approvalFailures["b"] == "Connection failed")
        companion.apply(snapshot: state(["b", "new"], seq: 2))
        let selected = try #require(companion.selectedApproval)
        #expect(companion.failedApprovals(for: selected).map(\.id) == ["b"])
        companion.apply(snapshot: state(["new"], seq: 3))
        #expect(companion.approvalFailures.isEmpty)
        let remaining = try #require(companion.selectedApproval)
        #expect(companion.failedApprovals(for: remaining).isEmpty)
    }
}
