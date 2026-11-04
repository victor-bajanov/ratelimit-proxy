import Foundation

/// Short human-readable numbers for the menu bar and chart legends.
enum ValueFormat {
    private static let tiers: [(divisor: Double, suffix: String)] =
        [(1, ""), (1e3, "k"), (1e6, "M"), (1e9, "B")]

    static func compact(_ v: Double) -> String {
        guard v.isFinite else { return "\u{2013}" }
        if v == 0 { return "0" }
        if abs(v) < 0.05 { return v < 0 ? "0" : "<0.1" }
        if v < 0 { return "-" + compact(-v) }

        for (i, tier) in tiers.enumerated() {
            let scaled = v / tier.divisor
            let tenths = (scaled * 10).rounded()
            if tenths < 100 {
                let n = Int(tenths)
                let text = n % 10 == 0 ? "\(n / 10)" : "\(n / 10).\(n % 10)"
                return text + tier.suffix
            }
            let whole = scaled.rounded()
            if whole < 1000 || i == tiers.count - 1 {
                return String(format: "%.0f", whole) + tier.suffix
            }
        }
        return "\u{2013}"  // unreachable: the last tier always returns
    }

    /// Chart axis ticks: k/M/B suffixes, up to three decimals, trailing zeros
    /// trimmed, so quarter steps stay distinct (0.25, 2.5k, 1.25M).
    static func axis(_ v: Double) -> String {
        guard v.isFinite else { return "\u{2013}" }
        if v < 0 { return "-" + axis(-v) }
        let tier = tiers.last { v >= $0.divisor } ?? tiers[0]
        var text = String(format: "%.3f", v / tier.divisor)
        while text.hasSuffix("0") { text.removeLast() }
        if text.hasSuffix(".") { text.removeLast() }
        return text + tier.suffix
    }

    static func full(_ v: Double, metric: Metric) -> String {
        compact(v) + " " + metric.unit
    }

    static func menuBar(_ v: Double?, metric: Metric, unreachable: Bool) -> String {
        if unreachable { return "off" }
        guard let v else { return "\u{2013}" }
        return compact(v) + metric.menuBarSuffix
    }
}
