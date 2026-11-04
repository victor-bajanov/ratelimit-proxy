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
    // Partial clock-back keeps samples not after the incoming time
    var pb = SampleStore()
    pb.record(snap(1), at: t(100))
    pb.record(snap(1), at: t(200))
    pb.record(snap(2), at: t(150))
    expectEqual(pb.samples.map(\.time), [t(100), t(150)], "clock-back record trims")
    var pbg = SampleStore()
    pbg.record(snap(1), at: t(100))
    pbg.record(snap(1), at: t(200))
    pbg.recordGap(at: t(150))
    expectEqual(pbg.samples.map(\.time), [t(100), t(150)], "clock-back gap trims")
    expect(pbg.samples.last!.isGap)

    b.removeAll()
    expect(b.samples.isEmpty)
    expectEqual(b.status, .waiting)

    // Outage start survives retention pruning of the first gap
    var o = SampleStore(retention: 10)
    o.record(snap(1), at: t(0))
    for s in stride(from: 5.0, through: 30, by: 5) { o.recordGap(at: t(s)) }
    expectEqual(o.samples.first?.time, t(20), "first gaps pruned")
    expectEqual(o.status, .unreachable(since: t(5)), "since outlives pruning")
    expectEqual(o.unreachableSince, t(5))
    o.removeAll()
    expectEqual(o.unreachableSince, nil)
    expectEqual(o.status, .waiting)
    o.recordGap(at: t(40))
    expectEqual(o.status, .unreachable(since: t(40)))
    o.record(snap(1), at: t(45))
    expectEqual(o.status, .live(updated: t(45)))
    expectEqual(o.unreachableSince, nil)
    // Clock-back during an outage moves since back to the new gap time
    var ob = SampleStore()
    ob.record(snap(1), at: t(100))
    ob.recordGap(at: t(200))
    ob.recordGap(at: t(150))
    expectEqual(ob.status, .unreachable(since: t(150)))

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
    let pts = s.points(for: [.tpsCredits], since: t(0))
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
    expectEqual(l.points(for: [.tpsCredits], since: t(0)).map(\.segment), [0, 1])

    // Snapshot lacking credits breaks credits only
    var m = SampleStore()
    m.record(snap(1), at: t(0))
    m.record(snap(1), at: t(5))
    m.record(RatesSnapshot(values: [.tpsInput: 7]), at: t(10))
    m.record(snap(1), at: t(15))
    expectEqual(m.points(for: [.tpsCredits], since: t(0)).map(\.segment), [0, 0, 1])
    expectEqual(m.points(for: [.tpsInput], since: t(0)).map(\.segment), [0, 0, 0, 0])

    // Time step break: default interval 5 -> threshold 15
    var w = SampleStore()
    w.record(snap(1), at: t(0))
    w.record(snap(1), at: t(5))
    w.record(snap(1), at: t(60))
    expectEqual(w.points(for: [.tpsCredits], since: t(0)).map(\.segment), [0, 0, 1])
    var w15 = SampleStore()
    w15.record(snap(1), at: t(0))
    w15.record(snap(1), at: t(15))
    expectEqual(w15.points(for: [.tpsCredits], since: t(0)).map(\.segment), [0, 0], "exactly threshold")
    // Interval change: the larger of the two neighbours' intervals sets the threshold
    var wi = SampleStore()
    for s in [0.0, 30, 60] { wi.record(snap(1), at: t(s), pollInterval: 30) }
    for s in [61.0, 62] { wi.record(snap(1), at: t(s), pollInterval: 1) }
    expectEqual(wi.points(for: [.tpsCredits], since: t(0)).map(\.segment), [0, 0, 0, 0, 0])
    expectEqual(wi.samples.map(\.pollInterval), [30, 30, 30, 1, 1])
    // 25 s step from a 30 s sample to a 1 s sample: max(30, 1) -> threshold 90, no break
    var wm = SampleStore()
    wm.record(snap(1), at: t(60), pollInterval: 30)
    wm.record(snap(1), at: t(85), pollInterval: 1)
    expectEqual(wm.points(for: [.tpsCredits], since: t(0)).map(\.segment), [0, 0], "max interval used")
    var w1 = SampleStore()
    w1.record(snap(1), at: t(0), pollInterval: 1)
    w1.record(snap(1), at: t(20), pollInterval: 1)
    expectEqual(w1.points(for: [.tpsCredits], since: t(0)).map(\.segment), [0, 1], "interval 1 -> 10 s")
    var wg = SampleStore()
    wg.recordGap(at: t(0), pollInterval: 2)
    expectEqual(wg.samples.first?.pollInterval, 2)
    expectEqual(Sample(time: t(0), snapshot: nil).pollInterval, PollInterval.defaultSeconds)

    // since filter, metric order, independent numbering, seriesKey
    var f = SampleStore()
    f.record(snap(1), at: t(0))
    f.record(RatesSnapshot(values: [.tpsCredits: 2]), at: t(5))
    f.record(snap(3), at: t(10))
    f.record(snap(4), at: t(15))
    let fp = f.points(for: [.tpsInput, .tpsCredits], since: t(5))
    expectEqual(fp.map(\.metric), [.tpsInput, .tpsInput, .tpsCredits, .tpsCredits, .tpsCredits])
    expectEqual(fp.map(\.time), [t(10), t(15), t(5), t(10), t(15)])
    expectEqual(fp.map(\.segment), [0, 0, 0, 0, 0], "lacking metric before first point: no bump")
    let all = f.points(for: [.tpsInput, .tpsCredits], since: t(0))
    expectEqual(all.filter { $0.metric == .tpsInput }.map(\.segment), [0, 1, 1])
    expectEqual(all.filter { $0.metric == .tpsCredits }.map(\.segment), [0, 0, 0, 0])
    expectEqual(all[1].seriesKey, "tps.input#1")
    expectEqual(all[1].id, "tps.input#1@\(t(10).timeIntervalSinceReferenceDate)")
    expect(f.points(for: [], since: t(0)).isEmpty)
}
