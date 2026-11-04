import Charts
import SwiftUI

/// One chart for a metric group over the selected range.
struct GroupChart: View {
    let group: MetricGroup
    let store: SampleStore
    let range: ChartRange
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
        let points = store.points(for: group.metrics, since: from)
        let counts = Dictionary(grouping: points, by: \.seriesKey).mapValues(\.count)
        let latest: [(metric: Metric, text: String)] = group.metrics.compactMap { m in
            store.latestValue(m).map { (m, ValueFormat.full($0, metric: m)) }
        }
        let multi = group.metrics.count > 1
        let axis = DualAxis(points: points, group: group)

        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text(group.title).font(.headline)
                Spacer()
                latestText(latest)
                    .font(.caption.monospacedDigit())
                    .lineLimit(1)
            }
            let chart = Chart {
                ForEach(points) { p in
                    LineMark(x: .value("Time", p.time), y: .value("Value", axis.plotted(p)),
                             series: .value("Series", p.seriesKey))
                        .foregroundStyle(by: .value("Metric", p.metric.name))
                        .interpolationMethod(.linear)
                    if counts[p.seriesKey] == 1 {
                        PointMark(x: .value("Time", p.time), y: .value("Value", axis.plotted(p)))
                            .foregroundStyle(by: .value("Metric", p.metric.name))
                            .symbolSize(12)
                    }
                }
            }
            .chartXScale(domain: from...now)
            .chartForegroundStyleScale(domain: group.metrics.map(\.name),
                                       range: group.metrics.map(Self.color))

            Group {
                if axis.isActive {
                    chart
                        .chartYScale(domain: 0...axis.primaryMax, type: .linear)
                        .chartYAxis { dualYAxis(axis) }
                        .chartLegend(.hidden)
                } else {
                    chart
                        .chartYScale(domain: .automatic(includesZero: true), type: .linear)
                        .chartYAxis {
                            AxisMarks { value in
                                AxisGridLine()
                                AxisTick()
                                AxisValueLabel {
                                    if let v = value.as(Double.self) { Text(ValueFormat.axis(v)) }
                                }
                            }
                        }
                        .chartLegend(position: .bottom)
                        .chartLegend(multi ? .visible : .hidden)  // last, so it wins
                }
            }
            .overlay {
                if points.isEmpty {
                    Text("No data").font(.caption).foregroundStyle(.secondary)
                }
            }
            .frame(height: group == .inFlight ? 60 : 110)

            if axis.isActive {
                // Primary series under the leading axis, secondary under the trailing one.
                HStack {
                    legend(group.secondaryMetrics, suffix: " (left axis)")
                    Spacer()
                    legend(group.primaryMetrics)
                }
            }
        }
    }

    @AxisContentBuilder
    private func dualYAxis(_ axis: DualAxis) -> some AxisContent {
        AxisMarks(position: .trailing, values: axis.primaryTicks) { value in
            AxisGridLine()
            AxisValueLabel {
                if let v = value.as(Double.self) { Text(ValueFormat.axis(v)) }
            }
        }
        AxisMarks(position: .leading, values: axis.primaryTicks) { value in
            AxisValueLabel {
                if let v = value.as(Double.self) {
                    Text(ValueFormat.axis(axis.secondaryValue(atPosition: v)))
                        .foregroundStyle(Self.color(group.secondaryMetrics[0]))
                }
            }
        }
    }

    /// Latest values, each in its series colour, dot-separated.
    private func latestText(_ items: [(metric: Metric, text: String)]) -> Text {
        items.enumerated().reduce(Text("")) { acc, item in
            let sep = item.offset == 0 ? Text("") : Text(" · ").foregroundStyle(.secondary)
            return acc + sep + Text(item.element.text).foregroundStyle(Self.color(item.element.metric))
        }
    }

    private func legend(_ metrics: [Metric], suffix: String = "") -> some View {
        HStack(spacing: 10) {
            ForEach(metrics) { m in
                HStack(spacing: 4) {
                    Circle().fill(Self.color(m)).frame(width: 7, height: 7)
                    Text(m.name + suffix)
                }
            }
        }
        .font(.caption2)
        .foregroundStyle(.secondary)
    }
}
