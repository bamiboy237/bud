import Foundation
import Testing
@testable import BudCompanion

struct RoutineScheduleTests {
    @Test(arguments: [
        ("0 0 * * *", RoutineSchedule.Repeat.daily, "0 0 * * *"),
        ("30 8 * * 1-5", .weekdays, "30 8 * * 1-5"),
        ("30 8 * * MON-FRI", .weekdays, "30 8 * * 1-5"),
        ("30 8 * * 1,2,3,4,5", .weekdays, "30 8 * * 1-5"),
        ("05 17 * * 7", .weekly, "5 17 * * 0"),
        ("59 23 * * Thu", .weekly, "59 23 * * 4"),
        ("15 11 31 * *", .monthly, "15 11 31 * *"),
    ])
    func commonSchedulesRoundTrip(input: String, repeatValue: RoutineSchedule.Repeat, output: String) {
        let schedule = RoutineSchedule(cron: input)
        #expect(schedule.frequency == repeatValue)
        #expect(schedule.cron == output)
    }

    @Test(arguments: ["*/15 8-17 * * 1-5", "0 9 1 * mon", "0 9 * 6 *", "15 5 * * 1,3", "0 9 l * *"])
    func advancedSchedulesStayUnchanged(_ cron: String) {
        let schedule = RoutineSchedule(cron: cron)
        #expect(schedule.frequency == .custom)
        #expect(schedule.cron == cron)
    }

    @Test
    func repeatAndClockChangesGenerateCron() {
        var schedule = RoutineSchedule(cron: "30 8 * * 1-5")
        schedule.select(.weekly)
        schedule.weekday = 0
        schedule.time = Date(timeIntervalSince1970: 17 * 3600 + 5 * 60)
        #expect(schedule.cron == "5 17 * * 0")
        schedule.select(.monthly)
        schedule.monthDay = 28
        #expect(schedule.cron == "5 17 28 * *")

        schedule.select(.custom)
        #expect(schedule.customCron == "5 17 28 * *")
        schedule.customCron = "0 12 * * mon,wed"
        schedule.select(.daily)
        #expect(schedule.cron == "5 17 * * *")
        schedule.select(.custom)
        #expect(schedule.cron == "0 12 * * mon,wed")
    }
}
