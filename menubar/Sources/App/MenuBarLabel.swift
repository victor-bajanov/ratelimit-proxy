import AppKit
import SwiftUI

/// The whole menu-bar label: one template image (sparkline + value).
struct MenuBarLabel: View {
    @State private var poller = Poller.shared

    var body: some View {
        Image(nsImage: LabelRenderer.image(
            store: poller.store,
            metric: poller.settings.sparklineMetric,
            pollInterval: poller.settings.pollInterval,
            now: Date()))
    }
}

enum LabelRenderer {
    static let height = 16.0
    static let spacing = 3.0
    static let lineWidth = 1.5
    static let offlineAlpha = 0.35
    static let widestText = "888k/s"

    /// Draws the label for the store's current state as a template image.
    static func image(store: SampleStore, metric: Metric, pollInterval: Int, now: Date) -> NSImage {
        let status = store.status
        let unreachable: Bool
        let waiting: Bool
        switch status {
        case .waiting: unreachable = false; waiting = true
        case .live: unreachable = false; waiting = false
        case .unreachable: unreachable = true; waiting = false
        }
        let from = now.addingTimeInterval(-Sparkline.window)
        let points = store.points(for: [metric], since: from,
                                  breakAfter: PollInterval.breakThreshold(pollInterval))
        let segments = Sparkline.geometry(points, from: from, to: now,
                                          width: Sparkline.width, height: height,
                                          inset: Sparkline.inset)
        let text = ValueFormat.menuBar(store.latestValue(metric), metric: metric,
                                       unreachable: unreachable)

        let font = NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .medium)
        let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.black]
        // Reserve the width of the widest usual value so the status item does not
        // shift as values change; text is left-aligned next to the sparkline.
        let textWidth = max((text as NSString).size(withAttributes: attrs).width,
                            (widestText as NSString).size(withAttributes: attrs).width)
        let sparkWidth = Sparkline.width
        let size = NSSize(width: ceil(sparkWidth + spacing + textWidth), height: height)

        let image = NSImage(size: size, flipped: false) { _ in
            let font = NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .medium)
            let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.black]
            if waiting {
                let path = NSBezierPath()
                path.lineWidth = 1
                path.setLineDash([2, 2], count: 2, phase: 0)
                path.move(to: NSPoint(x: 0, y: Sparkline.inset + 0.5))
                path.line(to: NSPoint(x: sparkWidth, y: Sparkline.inset + 0.5))
                NSColor.black.setStroke()
                path.stroke()
            } else {
                let color = NSColor.black.withAlphaComponent(unreachable ? offlineAlpha : 1)
                color.setStroke()
                color.setFill()
                for seg in segments {
                    if seg.count == 1, let p = seg.first {
                        let r = lineWidth / 2
                        NSBezierPath(ovalIn: NSRect(x: p.x - r, y: p.y - r,
                                                    width: lineWidth, height: lineWidth)).fill()
                        continue
                    }
                    let path = NSBezierPath()
                    path.lineWidth = lineWidth
                    path.lineJoinStyle = .round
                    path.lineCapStyle = .round
                    for (i, p) in seg.enumerated() {
                        let pt = NSPoint(x: p.x, y: p.y)
                        if i == 0 { path.move(to: pt) } else { path.line(to: pt) }
                    }
                    path.stroke()
                }
            }
            let tSize = (text as NSString).size(withAttributes: attrs)
            (text as NSString).draw(at: NSPoint(x: sparkWidth + spacing,
                                                y: (height - tSize.height) / 2),
                                    withAttributes: attrs)
            return true
        }
        image.isTemplate = true
        return image
    }
}
