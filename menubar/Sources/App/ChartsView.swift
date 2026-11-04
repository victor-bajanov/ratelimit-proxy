import Charts
import SwiftUI

/// One chart for a metric group over the selected range.
struct GroupChart: View {
    let group: MetricGroup
    let store: SampleStore
    let range: ChartRange
    let pollInterval: Int
    let now: Date

    static func color(_ m: Metric) -> Color {
        switch m {
        case .rpsStarted, .tpsInput: .blue
        case .rpsFinished, .tpsOutput: .green
        case .inFlight, .tpsCacheWrite: .orange
        case .tpsCacheRead: .purple
        case .tpsCredits: .pink
        }
    }

    var body: some View {
        let from = now.addingTimeInterval(-range.seconds)
        let points = store.points(for: group.metrics, since: from,
                                  breakAfter: PollInterval.breakThreshold(pollInterval))
        let counts = Dictionary(grouping: points, by: \.seriesKey).mapValues(\.count)
        let latest = group.metrics.compactMap { m in
            store.latestValue(m).map { ValueFormat.full($0, metric: m) }
        }
        let multi = group.metrics.count > 1

        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text(group.title).font(.headline)
                Spacer()
                Text(latest.joined(separator: " · "))
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Chart {
                ForEach(points) { p in
                    LineMark(x: .value("Time", p.time), y: .value("Value", p.value),
                             series: .value("Series", p.seriesKey))
                        .foregroundStyle(by: .value("Metric", p.metric.name))
                        .interpolationMethod(.linear)
                    if counts[p.seriesKey] == 1 {
                        PointMark(x: .value("Time", p.time), y: .value("Value", p.value))
                            .foregroundStyle(by: .value("Metric", p.metric.name))
                            .symbolSize(12)
                    }
                }
            }
            .chartXScale(domain: from...now)
            .chartYScale(domain: .automatic(includesZero: true), type: .linear)
            .chartForegroundStyleScale(domain: group.metrics.map(\.name),
                                       range: group.metrics.map(Self.color))
            .chartLegend(position: .bottom)
            .chartLegend(multi ? .visible : .hidden)  // last, so it wins
            .overlay {
                if points.isEmpty {
                    Text("No data").font(.caption).foregroundStyle(.secondary)
                }
            }
            .frame(height: 110)
        }
    }
}
