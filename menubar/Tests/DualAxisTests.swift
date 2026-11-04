import Foundation

func dualAxisTests() {
    // Nice ceilings: {1, 2, 4, 6, 8, 10} x 10^k, so quarter steps read cleanly.
    expectEqual(DualAxis.niceCeiling(0), 1)
    expectEqual(DualAxis.niceCeiling(-5), 1)
    expectEqual(DualAxis.niceCeiling(.nan), 1)
    expectEqual(DualAxis.niceCeiling(.infinity), 1)
    expectEqual(DualAxis.niceCeiling(1), 1)
    expectClose(DualAxis.niceCeiling(1.1), 2)
    expectClose(DualAxis.niceCeiling(3), 4)
    expectClose(DualAxis.niceCeiling(4.5), 6)
    expectClose(DualAxis.niceCeiling(7), 8)
    expectClose(DualAxis.niceCeiling(9), 10)
    expectClose(DualAxis.niceCeiling(10), 10)
    expectClose(DualAxis.niceCeiling(101.5), 200)
    expectClose(DualAxis.niceCeiling(728.1), 800)
    expectClose(DualAxis.niceCeiling(8146.3), 10000)
    expectClose(DualAxis.niceCeiling(0.03), 0.04, tol: 1e-12)

    // Which metrics ride the secondary (trailing) axis.
    expectEqual(MetricGroup.tokens.secondaryMetrics, [.tpsCacheWrite])
    expectEqual(MetricGroup.tokens.primaryMetrics, [.tpsInput, .tpsOutput])
    for g in MetricGroup.allCases where g != .tokens {
        expectEqual(g.secondaryMetrics, [], "\(g) has no secondary axis")
        expectEqual(g.primaryMetrics, g.metrics, "\(g) primary is all its metrics")
    }

    // Scaling: input/output peak 101.5 -> 200; cache write peak 728.1 -> 800.
    let t0 = Date(timeIntervalSinceReferenceDate: 0)
    func pt(_ m: Metric, _ v: Double, _ s: Double = 0) -> ChartPoint {
        ChartPoint(metric: m, time: t0.addingTimeInterval(s), value: v, segment: 0)
    }
    let points = [pt(.tpsInput, 46.3), pt(.tpsOutput, 101.5), pt(.tpsCacheWrite, 728.1),
                  pt(.tpsInput, 10, 5), pt(.tpsCacheWrite, 300, 5)]
    let axis = DualAxis(points: points, group: .tokens)
    expect(axis.isActive)
    expectClose(axis.primaryMax, 200)
    expectClose(axis.secondaryMax, 800)
    expectClose(axis.plotted(pt(.tpsCacheWrite, 728.1)), 182.025)
    expectClose(axis.plotted(pt(.tpsCacheWrite, 800)), 200)
    expectClose(axis.plotted(pt(.tpsOutput, 101.5)), 101.5)
    expectEqual(axis.primaryTicks, [0, 50, 100, 150, 200])
    expectEqual(axis.secondaryTicks.map(\.position), [0, 50, 100, 150, 200])
    expectEqual(axis.secondaryTicks.map(\.value), [0, 200, 400, 600, 800])
    expectEqual(axis.secondaryValue(atPosition: 150), 600)

    // No data: both axes fall back to 0...1 rather than collapsing.
    let empty = DualAxis(points: [], group: .tokens)
    expectEqual(empty.primaryMax, 1)
    expectEqual(empty.secondaryMax, 1)
    expectEqual(empty.secondaryTicks.map(\.value), [0, 0.25, 0.5, 0.75, 1])

    // Groups without a secondary metric pass values through untouched.
    let credits = DualAxis(points: [pt(.tpsCredits, 389.9)], group: .credits)
    expect(!credits.isActive)
    expectClose(credits.plotted(pt(.tpsCredits, 389.9)), 389.9)
}
