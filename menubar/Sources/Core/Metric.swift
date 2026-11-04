import Foundation

/// A chart panel: metrics that share an axis.
enum MetricGroup: String, CaseIterable, Sendable, Identifiable {
    case requests, tokens, cacheRead, credits

    var id: String { rawValue }

    var title: String {
        switch self {
        case .requests: "Requests"
        case .tokens: "Tokens/s"
        case .cacheRead: "Cache read tokens/s"
        case .credits: "Credits/s"
        }
    }

    var metrics: [Metric] {
        switch self {
        case .requests: [.rpsStarted, .rpsFinished, .inFlight]
        case .tokens: [.tpsInput, .tpsOutput, .tpsCacheWrite]
        case .cacheRead: [.tpsCacheRead]
        case .credits: [.tpsCredits]
        }
    }
}

/// One number read from /__proxy/rates. The raw value is its dotted JSON path.
enum Metric: String, CaseIterable, Sendable, Identifiable {
    case inFlight = "in_flight"
    case rpsStarted = "rps.started", rpsFinished = "rps.finished"
    case tpsInput = "tps.input", tpsOutput = "tps.output"
    case tpsCacheWrite = "tps.cache_write", tpsCacheRead = "tps.cache_read"
    case tpsCredits = "tps.credits"

    static let defaultSparkline: Metric = .tpsCredits

    init(stored: String?) {
        self = stored.flatMap(Metric.init(rawValue:)) ?? .defaultSparkline
    }

    var id: String { rawValue }

    var path: [String] { rawValue.split(separator: ".").map(String.init) }

    var name: String {
        switch self {
        case .inFlight: "In flight"
        case .rpsStarted: "Started"
        case .rpsFinished: "Finished"
        case .tpsInput: "Input"
        case .tpsOutput: "Output"
        case .tpsCacheWrite: "Cache write"
        case .tpsCacheRead: "Cache read"
        case .tpsCredits: "Credits"
        }
    }

    var menuTitle: String {
        switch self {
        case .inFlight: "Requests in flight"
        case .rpsStarted: "Requests started/s"
        case .rpsFinished: "Requests finished/s"
        case .tpsInput: "Input tokens/s"
        case .tpsOutput: "Output tokens/s"
        case .tpsCacheWrite: "Cache write tokens/s"
        case .tpsCacheRead: "Cache read tokens/s"
        case .tpsCredits: "Credits/s"
        }
    }

    var unit: String {
        switch self {
        case .inFlight: "in flight"
        case .rpsStarted, .rpsFinished: "req/s"
        case .tpsInput, .tpsOutput, .tpsCacheWrite, .tpsCacheRead: "tok/s"
        case .tpsCredits: "credits/s"
        }
    }

    var menuBarSuffix: String { self == .inFlight ? "" : "/s" }

    var group: MetricGroup {
        switch self {
        case .inFlight, .rpsStarted, .rpsFinished: .requests
        case .tpsInput, .tpsOutput, .tpsCacheWrite: .tokens
        case .tpsCacheRead: .cacheRead
        case .tpsCredits: .credits
        }
    }
}
