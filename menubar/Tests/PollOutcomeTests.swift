import Foundation

func pollOutcomeTests() {
    // Settings change effects
    let base = AppSettings.default
    expectEqual(base.changeEffects(to: base), SettingsChangeEffects(clearHistory: false, restartPolling: false))
    var spark = base
    spark.sparklineMetric = .tpsOutput
    spark.chartRange = .hour
    expectEqual(base.changeEffects(to: spark), SettingsChangeEffects(clearHistory: false, restartPolling: false))
    var poll = base
    poll.pollInterval = 1
    expectEqual(base.changeEffects(to: poll), SettingsChangeEffects(clearHistory: false, restartPolling: true))
    var endpoint = base
    endpoint.endpoint = URL(string: "http://127.0.0.1:9999/__proxy/rates")!
    expectEqual(base.changeEffects(to: endpoint), SettingsChangeEffects(clearHistory: true, restartPolling: true))

    // HTTP response -> outcome
    let body = Data(realPayload.utf8)
    let snap = try! RatesSnapshot.decode(from: body)
    expectEqual(PollOutcome.from(statusCode: 200, data: body), .snapshot(snap))
    expectEqual(PollOutcome.from(statusCode: nil, data: body), .snapshot(snap))
    expectEqual(PollOutcome.from(statusCode: 503, data: body), .failure("HTTP 503"))
    expectEqual(PollOutcome.from(statusCode: 404, data: Data()), .failure("HTTP 404"))
    expectEqual(PollOutcome.from(statusCode: 200, data: Data("<html>".utf8)), .failure("Bad response"))
    expectEqual(PollOutcome.from(statusCode: 200, data: Data("{}".utf8)), .failure("Bad response"))
    expectEqual(PollOutcome.snapshot(snap).errorMessage, nil)
    expectEqual(PollOutcome.failure("Timed out").errorMessage, "Timed out")

    // Error descriptions
    expectEqual(PollOutcome.describe(URLError(.cannotConnectToHost)), "Could not connect")
    expectEqual(PollOutcome.describe(URLError(.cannotFindHost)), "Could not connect")
    expectEqual(PollOutcome.describe(URLError(.networkConnectionLost)), "Could not connect")
    expectEqual(PollOutcome.describe(URLError(.notConnectedToInternet)), "Could not connect")
    expectEqual(PollOutcome.describe(URLError(.timedOut)), "Timed out")
    expectEqual(PollOutcome.describe(URLError(.appTransportSecurityRequiresSecureConnection)),
                "Plain http blocked (use https)")
    expectEqual(PollOutcome.describe(URLError(.badURL)), "Request failed (-1000)")
    expectEqual(PollOutcome.describe(RatesDecodeError.invalidJSON), "Request failed")

    // Recording outcomes
    var store = SampleStore()
    store.record(.snapshot(snap), at: t(0), pollInterval: 2)
    expectEqual(store.samples, [Sample(time: t(0), snapshot: snap, pollInterval: 2)])
    store.record(.failure("Timed out"), at: t(2), pollInterval: 2)
    expect(store.samples.last!.isGap)
    expectEqual(store.samples.last?.pollInterval, 2)
    expectEqual(store.status, .unreachable(since: t(2)))

    // Status helpers
    expect(ConnectionStatus.waiting.isWaiting)
    expect(!ConnectionStatus.waiting.isUnreachable)
    expect(!ConnectionStatus.live(updated: t(0)).isWaiting)
    expect(!ConnectionStatus.live(updated: t(0)).isUnreachable)
    expect(ConnectionStatus.unreachable(since: t(0)).isUnreachable)
    expect(!ConnectionStatus.unreachable(since: t(0)).isWaiting)
}
