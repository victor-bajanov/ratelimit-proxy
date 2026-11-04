import Foundation

/// What a settings edit requires of the poller and the history.
struct SettingsChangeEffects: Equatable, Sendable {
    var clearHistory: Bool
    var restartPolling: Bool
}

extension AppSettings {
    /// A new endpoint clears history and restarts polling; a new interval only
    /// restarts polling; sparkline metric and chart range need neither.
    func changeEffects(to new: AppSettings) -> SettingsChangeEffects {
        let endpointChanged = endpoint != new.endpoint
        return SettingsChangeEffects(
            clearHistory: endpointChanged,
            restartPolling: endpointChanged || pollInterval != new.pollInterval)
    }
}

/// The result of one poll, independent of URLSession.
enum PollOutcome: Equatable, Sendable {
    case snapshot(RatesSnapshot)
    case failure(String)

    /// nil or 200 -> decode the body; any other status -> "HTTP <code>";
    /// an undecodable body -> "Bad response".
    static func from(statusCode: Int?, data: Data) -> PollOutcome {
        if let code = statusCode, code != 200 { return .failure("HTTP \(code)") }
        guard let snapshot = try? RatesSnapshot.decode(from: data) else {
            return .failure("Bad response")
        }
        return .snapshot(snapshot)
    }

    /// Short text for a transport error.
    static func describe(_ error: Error) -> String {
        guard let e = error as? URLError else { return "Request failed" }
        switch e.code {
        case .cannotConnectToHost, .cannotFindHost, .networkConnectionLost, .notConnectedToInternet:
            return "Could not connect"
        case .timedOut: return "Timed out"
        case .appTransportSecurityRequiresSecureConnection: return "Plain http blocked (use https)"
        default: return "Request failed (\(e.code.rawValue))"
        }
    }

    var errorMessage: String? {
        if case .failure(let message) = self { return message }
        return nil
    }
}

extension SampleStore {
    /// A snapshot is recorded as a sample; a failure as a gap.
    mutating func record(_ outcome: PollOutcome, at time: Date, pollInterval: Int) {
        switch outcome {
        case .snapshot(let snapshot): record(snapshot, at: time, pollInterval: pollInterval)
        case .failure: recordGap(at: time, pollInterval: pollInterval)
        }
    }
}

extension ConnectionStatus {
    var isWaiting: Bool { self == .waiting }

    var isUnreachable: Bool {
        if case .unreachable = self { return true }
        return false
    }
}
