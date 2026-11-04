import AppKit

/// `--render-label <dir>`: writes label-live/offline/waiting.png at 2x and exits.
enum RenderLabel {
    static func runIfRequested(_ args: [String]) {
        guard let i = args.firstIndex(of: "--render-label") else { return }
        guard i + 1 < args.count else {
            FileHandle.standardError.write(Data("usage: --render-label <dir>\n".utf8))
            exit(2)
        }
        let dir = URL(fileURLWithPath: args[i + 1], isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let now = Date()
            for (name, store) in syntheticStores(now: now) {
                let image = LabelRenderer.image(store: store, metric: .tpsCredits, now: now)
                try png(image, scale: 2).write(to: dir.appendingPathComponent("label-\(name).png"))
            }
        } catch {
            FileHandle.standardError.write(Data("render-label: \(error)\n".utf8))
            exit(1)
        }
        exit(0)
    }

    /// live: a 5 min ramp sampled every 5 s with one gap; offline: the same plus
    /// a trailing gap; waiting: empty.
    static func syntheticStores(now: Date) -> [(String, SampleStore)] {
        var live = SampleStore()
        for i in stride(from: 300, through: 0, by: -5) {
            let t = now.addingTimeInterval(-Double(i))
            if i == 150 {
                live.recordGap(at: t)
            } else {
                live.record(RatesSnapshot(values: [.tpsCredits: Double(300 - i) * 2.5]), at: t)
            }
        }
        var offline = live
        offline.recordGap(at: now)
        return [("live", live), ("offline", offline), ("waiting", SampleStore())]
    }

    static func png(_ image: NSImage, scale: Int) throws -> Data {
        let w = Int(image.size.width) * scale, h = Int(image.size.height) * scale
        guard let rep = NSBitmapImageRep(
            bitmapDataPlanes: nil, pixelsWide: w, pixelsHigh: h, bitsPerSample: 8,
            samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)
        else { throw CocoaError(.fileWriteUnknown) }
        rep.size = image.size
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        image.draw(in: NSRect(origin: .zero, size: image.size))
        NSGraphicsContext.restoreGraphicsState()
        guard let data = rep.representation(using: .png, properties: [:])
        else { throw CocoaError(.fileWriteUnknown) }
        return data
    }
}
