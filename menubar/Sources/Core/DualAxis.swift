import Foundation

extension MetricGroup {
    /// Metrics drawn against a trailing axis of their own, because they run an
    /// order of magnitude above the rest of the group.
    var secondaryMetrics: [Metric] {
        self == .tokens ? [.tpsCacheWrite] : []
    }

    var primaryMetrics: [Metric] {
        metrics.filter { !secondaryMetrics.contains($0) }
    }
}

/// A leading and a trailing y-axis on one chart. Swift Charts has a single y
/// scale, so secondary values are scaled onto the primary domain, and the
/// trailing axis labels map the positions back. Both maxima are nice numbers
/// split into quarters, so the two axes share gridlines.
struct DualAxis: Equatable, Sendable {
    static let divisions = 4
    private static let mantissas: [Double] = [1, 2, 4, 6, 8, 10]

    let isActive: Bool
    let secondary: Set<Metric>
    let primaryMax: Double
    let secondaryMax: Double

    init(points: [ChartPoint], group: MetricGroup) {
        secondary = Set(group.secondaryMetrics)
        isActive = !secondary.isEmpty
        var p = 0.0, s = 0.0
        for point in points where point.value.isFinite {
            if secondary.contains(point.metric) { s = max(s, point.value) }
            else { p = max(p, point.value) }
        }
        primaryMax = Self.niceCeiling(p)
        secondaryMax = Self.niceCeiling(s)
    }

    /// Smallest {1, 2, 4, 6, 8, 10} x 10^k at or above v; 1 for v <= 0 or non-finite.
    static func niceCeiling(_ v: Double) -> Double {
        guard v.isFinite, v > 0 else { return 1 }
        let scale = pow(10, floor(log10(v)))
        let m = v / scale
        let pick = mantissas.first { $0 >= m * (1 - 1e-9) } ?? 10
        return pick * scale
    }

    /// The y position at which to draw a point.
    func plotted(_ point: ChartPoint) -> Double {
        guard isActive, secondary.contains(point.metric) else { return point.value }
        return point.value * primaryMax / secondaryMax
    }

    func secondaryValue(atPosition position: Double) -> Double {
        position * secondaryMax / primaryMax
    }

    var primaryTicks: [Double] {
        (0...Self.divisions).map { primaryMax * Double($0) / Double(Self.divisions) }
    }

    var secondaryTicks: [(position: Double, value: Double)] {
        primaryTicks.map { ($0, secondaryValue(atPosition: $0)) }
    }
}
