import Foundation

enum SettingsKey {
    static let sparklineMetric = "sparklineMetric"     // String (Metric.rawValue)
    static let pollInterval = "pollIntervalSeconds"    // Int
    static let endpointURL = "endpointURL"             // String
    static let chartRange = "chartRangeSeconds"        // Int
}

enum PollInterval {
    static let choices = [1, 2, 5, 10, 30]
    static let defaultSeconds = 5

    static func validated(_ s: Int?) -> Int {
        guard let s, choices.contains(s) else { return defaultSeconds }
        return s
    }

    /// Line-break threshold for SampleStore.points: max(3 * interval, 10).
    static func breakThreshold(_ s: Int) -> TimeInterval {
        max(3 * TimeInterval(s), 10)
    }

    /// Delay until the next poll: max(0, interval - elapsed).
    static func nextDelay(interval: Int, elapsed: TimeInterval) -> TimeInterval {
        max(0, TimeInterval(interval) - elapsed)
    }
}

enum ChartRange: Int, CaseIterable, Sendable, Identifiable {
    case fiveMinutes = 300, fifteenMinutes = 900, hour = 3600

    static let `default`: ChartRange = .fiveMinutes

    var id: Int { rawValue }

    var label: String {
        switch self {
        case .fiveMinutes: "5m"
        case .fifteenMinutes: "15m"
        case .hour: "1h"
        }
    }

    var seconds: TimeInterval { TimeInterval(rawValue) }

    init(stored: Int?) {
        self = stored.flatMap(ChartRange.init(rawValue:)) ?? .default
    }
}

enum EndpointURL {
    static let defaultString = "http://127.0.0.1:8787/__proxy/rates"

    static var `default`: URL { URL(string: defaultString)! }

    /// Trimmed; accepted only with an http/https scheme and a non-empty host.
    static func validate(_ s: String) -> URL? {
        let trimmed = s.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed),
              let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = url.host, !host.isEmpty else { return nil }
        return url
    }
}

struct AppSettings: Equatable, Sendable {
    var sparklineMetric: Metric
    var pollInterval: Int
    var endpoint: URL
    var chartRange: ChartRange

    static let `default` = AppSettings(sparklineMetric: .defaultSparkline,
                                       pollInterval: PollInterval.defaultSeconds,
                                       endpoint: EndpointURL.default,
                                       chartRange: .default)

    /// Each field falls back to its default independently when missing/invalid.
    static func load(from defaults: UserDefaults) -> AppSettings {
        AppSettings(
            sparklineMetric: Metric(stored: defaults.string(forKey: SettingsKey.sparklineMetric)),
            pollInterval: PollInterval.validated(storedInt(defaults, SettingsKey.pollInterval)),
            endpoint: defaults.string(forKey: SettingsKey.endpointURL)
                .flatMap(EndpointURL.validate) ?? EndpointURL.default,
            chartRange: ChartRange(stored: storedInt(defaults, SettingsKey.chartRange)))
    }

    /// An Int, NSNumber, or a String that parses as Int (command-line
    /// arguments in NSArgumentDomain arrive as strings).
    private static func storedInt(_ defaults: UserDefaults, _ key: String) -> Int? {
        switch defaults.object(forKey: key) {
        case let n as Int: n
        case let n as NSNumber: n.intValue
        case let s as String: Int(s.trimmingCharacters(in: .whitespaces))
        default: nil
        }
    }

    func save(to defaults: UserDefaults) {
        defaults.set(sparklineMetric.rawValue, forKey: SettingsKey.sparklineMetric)
        defaults.set(pollInterval, forKey: SettingsKey.pollInterval)
        defaults.set(endpoint.absoluteString, forKey: SettingsKey.endpointURL)
        defaults.set(chartRange.rawValue, forKey: SettingsKey.chartRange)
    }
}
