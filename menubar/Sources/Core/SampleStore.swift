import Foundation

/// One poll result: a snapshot, or a gap marker when the poll failed.
struct Sample: Equatable, Sendable {
    let time: Date
    let snapshot: RatesSnapshot?
    var isGap: Bool { snapshot == nil }
}

enum ConnectionStatus: Equatable, Sendable {
    case waiting
    case live(updated: Date)
    case unreachable(since: Date)
}

/// One plotted value. `segment` splits a metric's line at gaps and long pauses.
struct ChartPoint: Equatable, Sendable, Identifiable {
    let metric: Metric
    let time: Date
    let value: Double
    let segment: Int
    var seriesKey: String { "\(metric.rawValue)#\(segment)" }
    var id: String { "\(seriesKey)@\(time.timeIntervalSinceReferenceDate)" }
}

/// Time-ordered poll history, bounded by age (retention) and count (capacity).
struct SampleStore: Sendable {
    static let defaultRetention: TimeInterval = 3600
    static let defaultCapacity = 4000

    let retention: TimeInterval
    let capacity: Int
    private(set) var samples: [Sample] = []

    init(retention: TimeInterval = 3600, capacity: Int = 4000) {
        self.retention = retention
        self.capacity = max(0, capacity)
    }

    var status: ConnectionStatus {
        guard let last = samples.last else { return .waiting }
        guard last.isGap else { return .live(updated: last.time) }
        let firstGap = samples.reversed().prefix(while: \.isGap).last ?? last
        return .unreachable(since: firstGap.time)
    }

    func latestValue(_ metric: Metric) -> Double? {
        samples.last?.snapshot?[metric]
    }

    mutating func record(_ snapshot: RatesSnapshot, at time: Date) {
        append(Sample(time: time, snapshot: snapshot))
    }

    mutating func recordGap(at time: Date) {
        append(Sample(time: time, snapshot: nil))
    }

    mutating func removeAll() { samples.removeAll() }

    private mutating func append(_ sample: Sample) {
        if let last = samples.last, sample.time < last.time { samples.removeAll() }
        samples.append(sample)
        let cutoff = sample.time.addingTimeInterval(-retention)
        if let keepFrom = samples.firstIndex(where: { $0.time >= cutoff }), keepFrom > 0 {
            samples.removeFirst(keepFrom)
        }
        if samples.count > capacity { samples.removeFirst(samples.count - capacity) }
    }

    func points(for metrics: [Metric], since: Date, breakAfter: TimeInterval) -> [ChartPoint] {
        let visible = samples.drop { $0.time < since }
        var out: [ChartPoint] = []
        for metric in metrics {
            var segment = 0
            var lastTime: Date?
            var broken = false
            for sample in visible {
                guard let value = sample.snapshot?[metric] else {
                    if lastTime != nil { broken = true }
                    continue
                }
                if let lastTime, broken || sample.time.timeIntervalSince(lastTime) > breakAfter {
                    segment += 1
                }
                broken = false
                lastTime = sample.time
                out.append(ChartPoint(metric: metric, time: sample.time, value: value, segment: segment))
            }
        }
        return out
    }
}
