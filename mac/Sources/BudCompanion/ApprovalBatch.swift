import Foundation

struct ApprovalBatch {
    static func reviewedIDs(_ reviewed: [Approval], pending: [Approval]) -> [String] {
        let eligible = Set(pending.lazy.filter(\.canBatch).map(\.id))
        return reviewed.filter { $0.canBatch && eligible.contains($0.id) }.map(\.id)
    }

    enum DecisionError: Error, CustomStringConvertible {
        case noLongerPending

        var description: String { "This action is no longer pending." }
    }

    struct Result {
        var approved: Set<String> = []
        var failures: [String: String] = [:]
        var cancelled = false
    }

    static func requireApplied(_ response: [String: Any], key: String = "applied") throws {
        guard response[key] as? Bool == true else { throw DecisionError.noLongerPending }
    }

    /// New arrivals are not eligible. Independent failures do not block other decisions.
    @MainActor
    static func approve(
        _ ids: [String], send: (String) async throws -> [String: Any]
    ) async -> Result {
        var result = Result()
        for id in ids {
            do {
                try Task.checkCancellation()
                try requireApplied(try await send(id))
                result.approved.insert(id)
            } catch is CancellationError {
                result.cancelled = true
                break
            } catch {
                result.failures[id] = "\(error)"
            }
        }
        return result
    }
}
