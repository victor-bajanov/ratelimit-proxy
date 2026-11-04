import AppKit
import SwiftUI

/// The window opened from the menu-bar item.
struct PanelView: View {
    @State private var poller = Poller.shared
    @AppStorage(SettingsKey.chartRange) private var chartRangeRaw = ChartRange.default.rawValue
    @State private var showSettings = false

    static let clock = Date.VerbatimFormatStyle(
        format: "\(hour: .twoDigits(clock: .twentyFourHour, hourCycle: .zeroBased)):\(minute: .twoDigits):\(second: .twoDigits)",
        timeZone: .current, calendar: .current)

    private var range: ChartRange { ChartRange(stored: chartRangeRaw) }

    var body: some View {
        let now = Date()
        VStack(alignment: .leading, spacing: 10) {
            header
            Picker("Range", selection: Binding(
                get: { range },
                set: { r in chartRangeRaw = r.rawValue })) {
                ForEach(ChartRange.allCases) { Text($0.label).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()

            Picker("Menu bar", selection: Binding(
                get: { poller.settings.sparklineMetric },
                set: { m in change { $0.sparklineMetric = m } })) {
                ForEach(Metric.allCases) { Text($0.menuTitle).tag($0) }
            }
            .pickerStyle(.menu)

            ForEach(MetricGroup.allCases) { group in
                GroupChart(group: group, store: poller.store, range: range,
                           pollInterval: poller.settings.pollInterval, now: now)
            }

            if showSettings {
                Divider()
                SettingsSection(poller: poller, change: change)
            }

            Divider()
            HStack {
                Button { showSettings.toggle() } label: {
                    Image(systemName: "gearshape")
                }
                .buttonStyle(.borderless)
                .help("Settings")
                Spacer()
                Button("Quit") { NSApplication.shared.terminate(nil) }
                    .keyboardShortcut("q")
            }
        }
        .padding(12)
        .frame(width: 380)
    }

    @ViewBuilder private var header: some View {
        switch poller.store.status {
        case .live(let updated):
            Text("Live · updated \(updated.formatted(Self.clock))")
                .font(.caption).foregroundStyle(.secondary)
        case .unreachable(let since):
            VStack(alignment: .leading, spacing: 2) {
                Label("Proxy unreachable since \(since.formatted(Self.clock))",
                      systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red)
                if let err = poller.lastError {
                    Text(err).font(.caption).foregroundStyle(.secondary)
                }
                Text(poller.settings.endpoint.absoluteString)
                    .font(.caption.monospaced()).foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        case .waiting:
            Text("Waiting for first poll…").font(.caption).foregroundStyle(.secondary)
        }
    }

    /// Edits the poller's settings, carrying the panel's chart range so a
    /// save never overwrites it with a stale value.
    private func change(_ edit: (inout AppSettings) -> Void) {
        var s = poller.settings
        s.chartRange = range
        edit(&s)
        poller.apply(s)
    }
}
