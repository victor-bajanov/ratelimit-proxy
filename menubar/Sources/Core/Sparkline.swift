import Foundation

/// A point in sparkline space: x from the left, y up from the bottom.
struct SparkPoint: Equatable, Sendable { var x: Double; var y: Double }

/// Pure geometry for the menu-bar sparkline; the App layer only strokes it.
enum Sparkline {
    static let width = 40.0, height = 16.0, inset = 1.0
    static let window: TimeInterval = 300

    static func geometry(_ points: [ChartPoint], from: Date, to: Date,
                         width: Double = 40, height: Double = 16,
                         inset: Double = 1) -> [[SparkPoint]] {
        let span = to.timeIntervalSince(from)
        guard span > 0 else { return [] }
        let visible = points.filter { $0.time >= from && $0.time <= to }
        guard !visible.isEmpty else { return [] }

        let maxV = max(visible.map(\.value).max() ?? 0, 0)
        func y(_ v: Double) -> Double {
            maxV == 0 ? inset : inset + max(v, 0) / maxV * (height - 2 * inset)
        }

        var groups: [[SparkPoint]] = []
        var currentSegment: Int?
        for p in visible {
            let sp = SparkPoint(x: p.time.timeIntervalSince(from) / span * width, y: y(p.value))
            if p.segment == currentSegment {
                groups[groups.count - 1].append(sp)
            } else {
                groups.append([sp])
                currentSegment = p.segment
            }
        }
        return groups
    }
}
