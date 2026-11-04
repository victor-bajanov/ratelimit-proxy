import Foundation

func valueFormatTests() {
    let cases: [(Double, String)] = [
        (0, "0"), (0.883, "0.9"), (0.06, "0.1"), (0.049, "<0.1"), (1.0, "1"), (9.96, "10"),
        (78.7, "79"), (999.4, "999"), (999.6, "1k"), (1000, "1k"), (1234, "1.2k"),
        (1250.2, "1.3k"), (9949, "9.9k"), (9960, "10k"), (26377.35, "26k"),
        (999_499, "999k"), (999_600, "1M"), (1_500_000, "1.5M"), (2.5e9, "2.5B"),
        (-3, "-3"), (-0.01, "0"), (.nan, "\u{2013}"), (.infinity, "\u{2013}"),
        (-.infinity, "\u{2013}"), (-1234, "-1.2k"), (2.5e12, "2500B"),
        (1e30, "1000000000000000000000B"), (.greatestFiniteMagnitude, String(format: "%.0f", Double.greatestFiniteMagnitude / 1e9) + "B"),
    ]
    for (v, want) in cases {
        expectEqual(ValueFormat.compact(v), want, "compact(\(v))")
    }

    expectEqual(ValueFormat.full(668.8, metric: .tpsCredits), "669 credits/s")
    expectEqual(ValueFormat.full(9, metric: .inFlight), "9 in flight")
    expectEqual(ValueFormat.full(26377.35, metric: .tpsCacheRead), "26k tok/s")

    expectEqual(ValueFormat.menuBar(668.8, metric: .tpsCredits, unreachable: false), "669/s")
    expectEqual(ValueFormat.menuBar(9, metric: .inFlight, unreachable: false), "9")
    expectEqual(ValueFormat.menuBar(0.883, metric: .rpsStarted, unreachable: false), "0.9/s")
    expectEqual(ValueFormat.menuBar(nil, metric: .tpsCredits, unreachable: false), "\u{2013}")
    expectEqual(ValueFormat.menuBar(668.8, metric: .tpsCredits, unreachable: true), "off")
    expectEqual(ValueFormat.menuBar(nil, metric: .tpsCredits, unreachable: true), "off")
}
