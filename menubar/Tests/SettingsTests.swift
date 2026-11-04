import Foundation

func settingsTests() {
    expectEqual(SettingsKey.sparklineMetric, "sparklineMetric")
    expectEqual(SettingsKey.pollInterval, "pollIntervalSeconds")
    expectEqual(SettingsKey.endpointURL, "endpointURL")
    expectEqual(SettingsKey.chartRange, "chartRangeSeconds")

    expectEqual(PollInterval.choices, [1, 2, 5, 10, 30])
    expectEqual(PollInterval.defaultSeconds, 5)
    for s in [1, 2, 5, 10, 30] { expectEqual(PollInterval.validated(s), s) }
    for s in [7, 0, -5] { expectEqual(PollInterval.validated(s), 5, "validated(\(s))") }
    expectEqual(PollInterval.validated(nil), 5)
    expectEqual(PollInterval.breakThreshold(1), 10)
    expectEqual(PollInterval.breakThreshold(5), 15)
    expectEqual(PollInterval.breakThreshold(30), 90)
    expectClose(PollInterval.nextDelay(interval: 5, elapsed: 0.3), 4.7)
    expectEqual(PollInterval.nextDelay(interval: 5, elapsed: 6), 0)

    expectEqual(ChartRange.allCases, [.fiveMinutes, .fifteenMinutes, .hour])
    expectEqual(ChartRange.default, .fiveMinutes)
    expectEqual(ChartRange(stored: 900), .fifteenMinutes)
    expectEqual(ChartRange(stored: 3600), .hour)
    expectEqual(ChartRange(stored: 1800), .fiveMinutes)
    expectEqual(ChartRange(stored: nil), .fiveMinutes)
    expectEqual(ChartRange.allCases.map(\.label), ["5m", "15m", "1h"])
    expectEqual(ChartRange.hour.seconds, 3600)
    expectEqual(ChartRange.fifteenMinutes.id, 900)

    expectEqual(EndpointURL.defaultString, "http://127.0.0.1:8787/__proxy/rates")
    expectEqual(EndpointURL.default.absoluteString, EndpointURL.defaultString)
    expectEqual(EndpointURL.validate(EndpointURL.defaultString)?.absoluteString, EndpointURL.defaultString)
    expectEqual(EndpointURL.validate("  http://127.0.0.1:8787/__proxy/rates\n")?.absoluteString,
                EndpointURL.defaultString)
    expectEqual(EndpointURL.validate("HTTPS://example.com/r")?.host, "example.com")
    for bad in ["", "localhost:8787", "ftp://x/y", "http://", "not a url"] {
        expectEqual(EndpointURL.validate(bad), nil, "validate(\(bad))")
    }

    let def = AppSettings.default
    expectEqual(def.sparklineMetric, .tpsCredits)
    expectEqual(def.pollInterval, 5)
    expectEqual(def.endpoint, EndpointURL.default)
    expectEqual(def.chartRange, .fiveMinutes)

    let suite = "menubar-tests-\(UUID())"
    let defaults = UserDefaults(suiteName: suite)!
    defer { defaults.removePersistentDomain(forName: suite) }

    expectEqual(AppSettings.load(from: defaults), .default)

    let custom = AppSettings(sparklineMetric: .tpsOutput, pollInterval: 10,
                             endpoint: URL(string: "http://10.0.0.2:9000/__proxy/rates")!,
                             chartRange: .hour)
    custom.save(to: defaults)
    expectEqual(defaults.string(forKey: SettingsKey.sparklineMetric), "tps.output")
    expectEqual(defaults.integer(forKey: SettingsKey.pollInterval), 10)
    expectEqual(defaults.string(forKey: SettingsKey.endpointURL), "http://10.0.0.2:9000/__proxy/rates")
    expectEqual(defaults.integer(forKey: SettingsKey.chartRange), 3600)
    expectEqual(AppSettings.load(from: defaults), custom)

    defaults.set(7, forKey: SettingsKey.pollInterval)
    var want = custom
    want.pollInterval = 5
    expectEqual(AppSettings.load(from: defaults), want)

    defaults.set("foo", forKey: SettingsKey.sparklineMetric)
    want.sparklineMetric = .tpsCredits
    expectEqual(AppSettings.load(from: defaults), want)

    defaults.set("nonsense", forKey: SettingsKey.endpointURL)
    want.endpoint = EndpointURL.default
    expectEqual(AppSettings.load(from: defaults), want)

    defaults.set(1800, forKey: SettingsKey.chartRange)
    want.chartRange = .fiveMinutes
    expectEqual(AppSettings.load(from: defaults), want)

    // Command-line arguments (NSArgumentDomain) arrive as strings
    defaults.set("10", forKey: SettingsKey.pollInterval)
    defaults.set("900", forKey: SettingsKey.chartRange)
    let fromStrings = AppSettings.load(from: defaults)
    expectEqual(fromStrings.pollInterval, 10, "string poll interval")
    expectEqual(fromStrings.chartRange, .fifteenMinutes, "string chart range")
    defaults.set("abc", forKey: SettingsKey.pollInterval)
    defaults.set("abc", forKey: SettingsKey.chartRange)
    expectEqual(AppSettings.load(from: defaults).pollInterval, 5)
    expectEqual(AppSettings.load(from: defaults).chartRange, .fiveMinutes)
    defaults.set(NSNumber(value: 30), forKey: SettingsKey.pollInterval)
    expectEqual(AppSettings.load(from: defaults).pollInterval, 30)
}
