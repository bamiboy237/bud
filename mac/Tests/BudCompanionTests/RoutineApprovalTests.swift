import Foundation
import Testing
@testable import BudCompanion

@Suite
struct RoutineApprovalTests {
    @Test
    func nextRunUsesRoutineLocalTimeAndIdentifiesTheTimeZone() throws {
        let stamp = "2026-12-01T18:30:00.123456+00:00"
        let zone = try #require(TimeZone(identifier: "America/Chicago"))
        let locale = Locale(identifier: "en_US")
        let approval = try #require(Approval([
            "approval_id": "routine", "tool": "create_routine",
            "args": ["cron": "30 12 * * *"],
            "next_run_at": stamp, "timezone": zone.identifier,
        ], jobs: [:]))

        #expect(approval.facts.map(\.label) == ["Schedule", "Next run"])
        #expect(approval.facts.last?.value == Approval.nextRunText(stamp, timezone: zone))
        let remote = Approval.nextRunText(stamp, timezone: zone, localTimeZone: .gmt, locale: locale)
        let local = Approval.nextRunText(stamp, timezone: zone, localTimeZone: zone, locale: locale)
        #expect(local?.contains("Dec 1, 2026") == true)
        #expect(local?.contains("12:30") == true)
        #expect(remote == local.map { $0 + " CST" })
    }

    @Test
    func showsRoutinePlanBeforeApproval() throws {
        let prompt = "Summarize unread mail and flag anything that needs a reply."
        let approval = try #require(Approval([
            "approval_id": "routine",
            "tool": "create_routine",
            "args": [
                "title": "Morning inbox",
                "cron": "15 9 * * 1-5",
                "timezone": "America/Chicago",
                "skill": "inbox",
                "prompt": prompt,
            ],
        ], jobs: [:]))

        let time = Date(timeIntervalSince1970: 9 * 3600 + 15 * 60).formatted(
            Date.FormatStyle(date: .omitted, time: .shortened, timeZone: .gmt))
        #expect(approval.title == "Can I schedule \"Morning inbox\"?")
        #expect(approval.verb == "Schedule")
        #expect(approval.symbol == "clock.arrow.circlepath")
        #expect(approval.facts.map(\.label) == ["Schedule", "Skill"])
        #expect(approval.facts.map(\.value) == ["Weekdays at \(time) (America/Chicago)", "inbox"])
        #expect(approval.body == prompt)
    }

    @Test
    func customScheduleWorksWithoutOptionalFields() throws {
        let approval = try #require(Approval([
            "approval_id": "routine",
            "tool": "create_routine",
            "args": ["cron": "*/15 9-17 * * 1-5"],
        ], jobs: [:]))

        #expect(approval.title == "Can I schedule a routine?")
        #expect(approval.verb == "Schedule")
        #expect(approval.facts.map(\.label) == ["Schedule"])
        #expect(approval.facts.map(\.value) == ["Custom cron: */15 9-17 * * 1-5"])
        #expect(approval.body == nil)
    }

    @Test
    func blankScheduleAndSkillDoNotProduceFacts() throws {
        let approval = try #require(Approval([
            "approval_id": "routine",
            "tool": "create_routine",
            "args": ["cron": " ", "skill": " ", "timezone": "America/Chicago"],
        ], jobs: [:]))

        #expect(approval.facts.isEmpty)
    }

    @Test
    func emailApprovalKeepsItsOwnFactsAndBody() throws {
        let approval = try #require(Approval([
            "approval_id": "email",
            "tool": "send_email",
            "args": [
                "to": "dana@example.com",
                "subject": "Update",
                "body": "Here is the update.",
                "prompt": "Do not show this as the body.",
                "cron": "0 9 * * *",
                "skill": "inbox",
            ],
        ], jobs: [:]))

        #expect(approval.title == "Can I send email to dana@example.com?")
        #expect(approval.verb == "Send")
        #expect(approval.symbol == "paperplane")
        #expect(approval.facts.map(\.label) == ["To", "Subject"])
        #expect(approval.facts.map(\.value) == ["dana@example.com", "Update"])
        #expect(approval.body == "Here is the update.")
    }
}
