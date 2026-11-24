import Foundation

struct RoutineSchedule {
    enum Repeat: String, CaseIterable {
        case daily = "Daily"
        case weekdays = "Weekdays"
        case weekly = "Weekly"
        case monthly = "Monthly"
        case custom = "Custom cron"
    }

    // A date picker needs a Date, but cron stores a wall-clock time, not an instant.
    // Use a fixed UTC clock so neither the Mac's time zone nor DST changes the digits.
    static let clockCalendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = .gmt
        return calendar
    }()

    private static let cronWeekdays = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
    private(set) var frequency: Repeat = .custom
    var time = Date(timeIntervalSince1970: 9 * 3600)
    var weekday = 1
    var monthDay = 1
    var customCron = ""

    init(cron: String) {
        let fields = cron.split(whereSeparator: \.isWhitespace).map(String.init)
        guard fields.count == 5, let minute = Int(fields[0]), let hour = Int(fields[1]),
              (0...59).contains(minute), (0...23).contains(hour) else {
            customCron = cron
            return
        }
        time = Date(timeIntervalSince1970: Double(hour * 3600 + minute * 60))
        let day = fields[2]
        let week = fields[4].lowercased()
        if fields[3] == "*" {
            switch (day, week) {
            case ("*", "*"):
                frequency = .daily
            case ("*", "1-5"), ("*", "mon-fri"), ("*", "1,2,3,4,5"):
                frequency = .weekdays
            case ("*", _):
                if let number = Int(week) ?? Self.cronWeekdays.firstIndex(of: week),
                   (0...7).contains(number) {
                    frequency = .weekly
                    weekday = number % 7
                }
            case (_, "*"):
                if let number = Int(day), (1...31).contains(number) {
                    frequency = .monthly
                    monthDay = number
                }
            default:
                break
            }
        }
        if frequency == .custom { customCron = cron }
    }

    mutating func select(_ repeatValue: Repeat) {
        if repeatValue == .custom && customCron.isEmpty { customCron = cron }
        frequency = repeatValue
    }

    var cron: String {
        let minute = Self.clockCalendar.component(.minute, from: time)
        let hour = Self.clockCalendar.component(.hour, from: time)
        let slot = "\(minute) \(hour)"
        switch frequency {
        case .daily: return "\(slot) * * *"
        case .weekdays: return "\(slot) * * 1-5"
        case .weekly: return "\(slot) * * \(weekday)"
        case .monthly: return "\(slot) \(monthDay) * *"
        case .custom: return customCron
        }
    }
}
