import Foundation

private func pt(_ s: Double, _ v: Double, seg: Int = 0) -> ChartPoint {
    ChartPoint(metric: .tpsCredits, time: t(s), value: v, segment: seg)
}

private func geo(_ pts: [ChartPoint]) -> [[SparkPoint]] {
    Sparkline.geometry(pts, from: t(0), to: t(300))
}

func sparklineTests() {
    expectEqual(Sparkline.width, 40)
    expectEqual(Sparkline.height, 16)
    expectEqual(Sparkline.inset, 1)
    expectEqual(Sparkline.window, 300)

    let zeros = geo([pt(0, 0), pt(150, 0), pt(300, 0)])
    expectEqual(zeros.count, 1)
    expectEqual(zeros.first?.map(\.y), [1, 1, 1])
    expectEqual(zeros.first?.map(\.x), [0, 20, 40])

    let ramp = geo([pt(0, 0), pt(150, 5), pt(300, 10)])
    expectEqual(ramp.first?.map(\.y), [1, 8, 15])

    let split = geo([pt(0, 2, seg: 0), pt(30, 5, seg: 0), pt(60, 10, seg: 1), pt(90, 0, seg: 1)])
    expectEqual(split.count, 2)
    expectClose(split.first?.first?.y, 3.8)
    expectEqual(split.first?.last?.y, 8)
    expectEqual(split.last?.map(\.y), [15, 1])
    expectEqual(split.map { $0.map(\.x) }, [[0, 4], [8, 12]])

    let single = geo([pt(0, 1, seg: 0), pt(30, 2, seg: 0), pt(60, 4, seg: 1)])
    expectEqual(single.count, 2)
    expectEqual(single.last?.count, 1)
    expectEqual(single.last?.first, SparkPoint(x: 8, y: 15))

    let neg = geo([pt(-1, 99), pt(0, -5), pt(150, 10), pt(301, 99)])
    expectEqual(neg.count, 1)
    expectEqual(neg.first, [SparkPoint(x: 0, y: 1), SparkPoint(x: 20, y: 15)])

    // Segment emptied by the window filter is dropped
    let dropped = geo([pt(-10, 3, seg: 0), pt(10, 3, seg: 1)])
    expectEqual(dropped.count, 1)

    // Custom size
    let custom = Sparkline.geometry([pt(0, 0), pt(300, 4)], from: t(0), to: t(300),
                                    width: 100, height: 10, inset: 0)
    expectEqual(custom, [[SparkPoint(x: 0, y: 0), SparkPoint(x: 100, y: 10)]])

    expectEqual(Sparkline.geometry([pt(0, 1)], from: t(300), to: t(300)), [])
    expectEqual(Sparkline.geometry([pt(0, 1)], from: t(300), to: t(0)), [])
    expectEqual(geo([]), [])
}
