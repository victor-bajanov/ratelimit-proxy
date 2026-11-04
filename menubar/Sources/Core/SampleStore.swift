import Foundation

/// One poll result: a snapshot, or a gap marker when the poll failed.
struct Sample: Equatable, Sendable {
    let time: Date
    let snapshot: RatesSnapshot?
    /// Poll interval in force when this sample was taken; sets its line-break threshold.
    var pollInterval: Int = PollInterval.defaultSeconds
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
    /// Time of the first failed poll in the current outage; nil while live.
    /// Kept separately so retention pruning cannot move the outage start.
    private(set) var unreachableSince: Date?

    init(retention: TimeInterval = SampleStore.defaultRetention,
         capacity: Int = SampleStore.defaultCapacity) {
        self.retention = retention
        self.capacity = max(0, capacity)
    }

    var status: ConnectionStatus {
        guard let last = samples.last else { return .waiting }
        guard last.isGap else { return .live(updated: last.time) }
        return .unreachable(since: unreachableSince ?? last.time)
    }

    func latestValue(_ metric: Metric) -> Double? {
        samples.last?.snapshot?[metric]
    }

    mutating func record(_ snapshot: RatesSnapshot, at time: Date,
                         pollInterval: Int = PollInterval.defaultSeconds) {
        unreachableSince = nil
        append(Sample(time: time, snapshot: snapshot, pollInterval: pollInterval))
    }

    mutating func recordGap(at time: Date, pollInterval: Int = PollInterval.defaultSeconds) {
        // A gap earlier than the recorded start (clock moved back) moves it back.
        unreachableSince = min(unreachableSince ?? time, time)
        append(Sample(time: time, snapshot: nil, pollInterval: pollInterval))
    }

    mutating func removeAll() {
        samples.removeAll()
        unreachableSince = nil
    }

    private mutating func append(_ sample: Sample) {
        // Wall clock moved back: drop only what now lies in the future.
        if let last = samples.last, sample.time < last.time {
            samples.removeAll { $0.time > sample.time }
        }
        samples.append(sample)
        let cutoff = sample.time.addingTimeInterval(-retention)
        if let keepFrom = samples.firstIndex(where: { $0.time >= cutoff }), keepFrom > 0 {
            samples.removeFirst(keepFrom)
        }
        if samples.count > capacity { samples.removeFirst(samples.count - capacity) }
    }

    /// Points per metric (in `metrics` order) for samples at or after `since`.
    /// A gap or a sample lacking the metric breaks the line; so does a step longer
    /// than PollInterval.breakThreshold of the larger of the two samples' intervals.
    func points(for metrics: [Metric], since: Date) -> [ChartPoint] {
        let visible = samples.drop { $0.time < since }
        var out: [ChartPoint] = []
        for metric in metrics {
            var segment = 0
            var previous: Sample?
            var broken = false
            for sample in visible {
                guard let value = sample.snapshot?[metric] else {
                    if previous != nil { broken = true }
                    continue
                }
                if let previous {
                    let threshold = PollInterval.breakThreshold(
                        max(previous.pollInterval, sample.pollInterval))
                    if broken || sample.time.timeIntervalSince(previous.time) > threshold {
                        segment += 1
                    }
                }
                broken = false
                previous = sample
                out.append(ChartPoint(metric: metric, time: sample.time, value: value, segment: segment))
            }
        }
        return out
    }
}
