import Foundation

private func snap(_ v: Double) -> RatesSnapshot {
    RatesSnapshot(values: [.tpsCredits: v, .tpsInput: v * 10])
}

func sampleStoreTests() {
    expectEqual(SampleStore.defaultRetention, 3600)
    expectEqual(SampleStore.defaultCapacity, 4000)

    var s = SampleStore()
    expectEqual(s.status, .waiting)
    expectEqual(s.latestValue(.tpsCredits), nil)

    s.record(snap(1), at: t(0))
    s.record(snap(2), at: t(5))
    expectEqual(s.samples.map(\.time), [t(0), t(5)])
    expectEqual(s.latestValue(.tpsCredits), 2)
    expectEqual(s.latestValue(.inFlight), nil, "metric absent from last snapshot")
    expectEqual(s.status, .live(updated: t(5)))

    s.recordGap(at: t(10))
    expectEqual(s.status, .unreachable(since: t(10)))
    expectEqual(s.latestValue(.tpsCredits), nil)
    expect(s.samples.last!.isGap)
    s.recordGap(at: t(15))
    expectEqual(s.status, .unreachable(since: t(10)))
    s.record(snap(3), at: t(20))
    expectEqual(s.status, .live(updated: t(20)))
    expectEqual(s.latestValue(.tpsCredits), 3)

    // Leading gap only
    var g = SampleStore()
    g.recordGap(at: t(1))
    expectEqual(g.status, .unreachable(since: t(1)))

    // Time pruning: boundary kept
    var p = SampleStore()
    p.record(snap(1), at: t(0))
    p.record(snap(1), at: t(1))
    p.record(snap(1), at: t(3600))
    expectEqual(p.samples.map(\.time), [t(0), t(1), t(3600)])
    p.record(snap(1), at: t(3601))
    expectEqual(p.samples.map(\.time), [t(1), t(3600), t(3601)])

    var pg = SampleStore()
    pg.recordGap(at: t(0))
    pg.recordGap(at: t(1))
    pg.recordGap(at: t(3600))
    expectEqual(pg.samples.count, 3)
    pg.recordGap(at: t(3601))
    expectEqual(pg.samples.map(\.time), [t(1), t(3600), t(3601)])

    // Capacity
    var c = SampleStore(capacity: 5)
    for i in 0..<7 { c.record(snap(Double(i)), at: t(Double(i))) }
    expectEqual(c.samples.map(\.time), (2..<7).map { t(Double($0)) })

    // Negative capacity is clamped to 0
    var neg = SampleStore(capacity: -1)
    neg.record(snap(1), at: t(0))
    neg.recordGap(at: t(1))
    expect(neg.samples.isEmpty, "negative capacity keeps nothing")
    expectEqual(neg.status, .waiting)

    // One hour at 1 s
    var h = SampleStore()
    var maxCount = 0
    for i in 0..<4000 {
        h.record(snap(1), at: t(Double(i)))
        maxCount = max(maxCount, h.samples.count)
    }
    expectEqual(h.samples.count, 3601)
    expectEqual(h.samples.first?.time, t(399))
    expect(maxCount <= 4000, "count never above capacity")

    // Clock backwards
    var b = SampleStore()
    b.record(snap(1), at: t(100))
    b.record(snap(1), at: t(200))
    b.record(snap(9), at: t(50))
    expectEqual(b.samples, [Sample(time: t(50), snapshot: snap(9))])
    var bg = SampleStore()
    bg.record(snap(1), at: t(100))
    bg.recordGap(at: t(50))
    expectEqual(bg.samples.map(\.time), [t(50)])

    b.removeAll()
    expect(b.samples.isEmpty)
    expectEqual(b.status, .waiting)

    pointsTests()
}

private func pointsTests() {
    var s = SampleStore()
    s.record(snap(1), at: t(0))
    s.record(snap(2), at: t(5))
    s.record(snap(3), at: t(10))
    s.recordGap(at: t(15))
    s.record(snap(4), at: t(20))
    s.record(snap(5), at: t(25))
    let pts = s.points(for: [.tpsCredits], since: t(0), breakAfter: 100)
    expectEqual(pts.map(\.segment), [0, 0, 0, 1, 1])
    expectEqual(pts.map(\.time), [t(0), t(5), t(10), t(20), t(25)])
    expectEqual(pts.map(\.value), [1, 2, 3, 4, 5])
    expect(pts.allSatisfy { $0.metric == .tpsCredits })

    // Leading gap, then two consecutive gaps bump once
    var l = SampleStore()
    l.recordGap(at: t(0))
    l.record(snap(1), at: t(5))
    l.recordGap(at: t(10))
    l.recordGap(at: t(15))
    l.record(snap(2), at: t(20))
    expectEqual(l.points(for: [.tpsCredits], since: t(0), breakAfter: 100).map(\.segment), [0, 1])

    // Snapshot lacking credits breaks credits only
    var m = SampleStore()
    m.record(snap(1), at: t(0))
    m.record(snap(1), at: t(5))
    m.record(RatesSnapshot(values: [.tpsInput: 7]), at: t(10))
    m.record(snap(1), at: t(15))
    expectEqual(m.points(for: [.tpsCredits], since: t(0), breakAfter: 100).map(\.segment), [0, 0, 1])
    expectEqual(m.points(for: [.tpsInput], since: t(0), breakAfter: 100).map(\.segment), [0, 0, 0, 0])

    // Time step break
    var w = SampleStore()
    w.record(snap(1), at: t(0))
    w.record(snap(1), at: t(5))
    w.record(snap(1), at: t(60))
    expectEqual(w.points(for: [.tpsCredits], since: t(0), breakAfter: 15).map(\.segment), [0, 0, 1])
    // exactly breakAfter does not break
    expectEqual(w.points(for: [.tpsCredits], since: t(0), breakAfter: 55).map(\.segment), [0, 0, 0])

    // since filter, metric order, independent numbering, seriesKey
    var f = SampleStore()
    f.record(snap(1), at: t(0))
    f.record(RatesSnapshot(values: [.tpsCredits: 2]), at: t(5))
    f.record(snap(3), at: t(10))
    f.record(snap(4), at: t(15))
    let fp = f.points(for: [.tpsInput, .tpsCredits], since: t(5), breakAfter: 100)
    expectEqual(fp.map(\.metric), [.tpsInput, .tpsInput, .tpsCredits, .tpsCredits, .tpsCredits])
    expectEqual(fp.map(\.time), [t(10), t(15), t(5), t(10), t(15)])
    expectEqual(fp.map(\.segment), [0, 0, 0, 0, 0], "lacking metric before first point: no bump")
    let all = f.points(for: [.tpsInput, .tpsCredits], since: t(0), breakAfter: 100)
    expectEqual(all.filter { $0.metric == .tpsInput }.map(\.segment), [0, 1, 1])
    expectEqual(all.filter { $0.metric == .tpsCredits }.map(\.segment), [0, 0, 0, 0])
    expectEqual(all[1].seriesKey, "tps.input#1")
    expectEqual(all[1].id, "tps.input#1@\(t(10).timeIntervalSinceReferenceDate)")
    expect(f.points(for: [], since: t(0), breakAfter: 10).isEmpty)
}
