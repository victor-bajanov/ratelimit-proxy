import Foundation

private func decode(_ s: String) throws -> RatesSnapshot {
    try RatesSnapshot.decode(from: Data(s.utf8))
}

private func decoded(_ s: String, file: String = #fileID, line: Int = #line) -> RatesSnapshot {
    do { return try decode(s) } catch {
        checkCount += 1
        fail("decode", "expected snapshot got \(error)", file: file, line: line)
        return RatesSnapshot(values: [:])
    }
}

func decodeTests() {
    let real = decoded(realPayload)
    expectEqual(real.values.count, 8)
    expectEqual(real[.inFlight], 9)
    expectEqual(real[.rpsStarted], 0.883)
    expectEqual(real[.rpsFinished], 0.95)
    expectEqual(real[.tpsInput], 78.7)
    expectEqual(real[.tpsOutput], 179.35)
    expectEqual(real[.tpsCacheWrite], 1250.2)
    expectEqual(real[.tpsCacheRead], 26377.35)
    expectEqual(real[.tpsCredits], 668.8)

    let pretty = decoded("""
    {
      "window_s": 60,
      "in_flight": 2,
      "rps": {
        "started": 0.31666666666666665,
        "finished": 0.3
      },
      "tps": {
        "input": 12.5,
        "output": 404.53333333333336,
        "cache_write": 0,
        "cache_read": 9876.016666666666,
        "credits": 123.45678901234567
      }
    }
    """)
    expectEqual(pretty.values.count, 8, "pretty-printed")
    expectEqual(pretty[.tpsOutput], 404.53333333333336)
    expectEqual(pretty[.tpsCacheWrite], 0)

    let extra = decoded(#"{"foo":1,"bar":{"x":[1,2]},"in_flight":3,"rps":{"started":1,"finished":2,"nested":{"a":1}},"tps":{"extra":3,"input":1,"output":2,"cache_write":3,"cache_read":4,"credits":5}}"#)
    expectEqual(extra.values.count, 8, "extra keys ignored")
    expectEqual(extra[.tpsCredits], 5)

    let noCredits = decoded(#"{"in_flight":9,"rps":{"started":1,"finished":2},"tps":{"input":1,"output":2,"cache_write":3,"cache_read":4}}"#)
    expectEqual(noCredits.values.count, 7)
    expectEqual(noCredits[.tpsCredits], nil)

    let noRps = decoded(#"{"in_flight":9,"tps":{"input":1,"output":2,"cache_write":3,"cache_read":4,"credits":5}}"#)
    expectEqual(noRps[.rpsStarted], nil)
    expectEqual(noRps[.rpsFinished], nil)
    expectEqual(noRps.values.count, 6)

    for bad in [#""9""#, "null", "true", "false"] {
        let s = decoded(#"{"in_flight":"# + bad + #","tps":{"credits":5}}"#)
        expectEqual(s[.inFlight], nil, "in_flight \(bad) absent")
        expectEqual(s[.tpsCredits], 5)
    }

    let rpsScalar = decoded(#"{"rps":5,"tps":{"credits":5}}"#)
    expectEqual(rpsScalar[.rpsStarted], nil)
    expectEqual(rpsScalar[.rpsFinished], nil)
    expectEqual(rpsScalar.values.count, 1)

    expectThrows(RatesDecodeError.noMetrics) { _ = try decode("{}") }
    expectThrows(RatesDecodeError.noMetrics) { _ = try decode(#"{"window_s":60}"#) }
    expectThrows(RatesDecodeError.notAnObject) { _ = try decode("[]") }
    expectThrows(RatesDecodeError.invalidJSON) { _ = try decode("<html>") }
    expectThrows(RatesDecodeError.invalidJSON) { _ = try RatesSnapshot.decode(from: Data()) }

    let direct = RatesSnapshot(values: [.inFlight: 1])
    expectEqual(direct[.inFlight], 1)
    expectEqual(direct, RatesSnapshot(values: [.inFlight: 1]))
}
