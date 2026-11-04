import Foundation

func metricTests() {
    expectEqual(Metric.allCases, [.inFlight, .rpsStarted, .rpsFinished, .tpsInput,
                                  .tpsOutput, .tpsCacheWrite, .tpsCacheRead, .tpsCredits])
    expectEqual(Metric.allCases.map(\.rawValue),
                ["in_flight", "rps.started", "rps.finished", "tps.input", "tps.output",
                 "tps.cache_write", "tps.cache_read", "tps.credits"])
    expectEqual(Metric.rpsStarted.path, ["rps", "started"])
    expectEqual(Metric.inFlight.path, ["in_flight"])
    expectEqual(Metric.tpsCacheWrite.path, ["tps", "cache_write"])
    expectEqual(Metric.defaultSparkline, .tpsCredits)
    expectEqual(Metric.tpsInput.id, "tps.input")

    expectEqual(Metric(stored: "tps.output"), .tpsOutput)
    expectEqual(Metric(stored: nil), .tpsCredits)
    expectEqual(Metric(stored: ""), .tpsCredits)
    expectEqual(Metric(stored: "bogus"), .tpsCredits)

    for m in Metric.allCases {
        expect(m.group.metrics.contains(m), "\(m) in its group")
    }
    let all = MetricGroup.allCases.flatMap(\.metrics)
    expectEqual(all.count, 8, "groups have 8 members in total")
    expectEqual(Set(all), Set(Metric.allCases), "groups partition all metrics")
    expectEqual(MetricGroup.allCases, [.requests, .inFlight, .tokens, .cacheRead, .credits])
    expectEqual(MetricGroup.requests.metrics, [.rpsStarted, .rpsFinished])
    expectEqual(MetricGroup.inFlight.metrics, [.inFlight])
    expectEqual(MetricGroup.inFlight.title, "In flight")
    expectEqual(MetricGroup.inFlight.id, "inFlight")
    expectEqual(MetricGroup.tokens.metrics, [.tpsInput, .tpsOutput, .tpsCacheWrite])
    expectEqual(MetricGroup.cacheRead.metrics, [.tpsCacheRead])
    expectEqual(MetricGroup.credits.metrics, [.tpsCredits])
    expectEqual(MetricGroup.requests.title, "Requests/s")
    expectEqual(MetricGroup.tokens.title, "Tokens/s")
    expectEqual(MetricGroup.cacheRead.title, "Cache read tokens/s")
    expectEqual(MetricGroup.credits.title, "Credits/s")
    expectEqual(MetricGroup.tokens.id, "tokens")

    expectEqual(Metric.allCases.map(\.name),
                ["In flight", "Started", "Finished", "Input", "Output",
                 "Cache write", "Cache read", "Credits"])
    expectEqual(Metric.allCases.map(\.menuTitle),
                ["Requests in flight", "Requests started/s", "Requests finished/s",
                 "Input tokens/s", "Output tokens/s", "Cache write tokens/s",
                 "Cache read tokens/s", "Credits/s"])
    expectEqual(Metric.inFlight.unit, "in flight")
    expectEqual(Metric.rpsFinished.unit, "req/s")
    expectEqual(Metric.tpsCacheRead.unit, "tok/s")
    expectEqual(Metric.tpsCredits.unit, "credits/s")
    expectEqual(Metric.inFlight.menuBarSuffix, "")
    expectEqual(Metric.tpsCacheRead.menuBarSuffix, "/s")
    expectEqual(Metric.tpsCredits.menuBarSuffix, "/s")
    expectEqual(Metric.inFlight.group, .inFlight)
    expectEqual(Metric.rpsStarted.group, .requests)
    expectEqual(Metric.tpsCacheRead.group, .cacheRead)
    expectEqual(Metric.tpsCredits.group, .credits)
}
