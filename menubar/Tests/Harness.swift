import Foundation

nonisolated(unsafe) var checkCount = 0
nonisolated(unsafe) var failureCount = 0

func fail(_ msg: String, _ detail: String, file: String, line: Int) {
    failureCount += 1
    print("FAIL \(file):\(line) \(msg) \(detail)")
}

func expect(_ cond: @autoclosure () -> Bool, _ msg: String = "",
            file: String = #fileID, line: Int = #line) {
    checkCount += 1
    if !cond() { fail(msg, "expected true got false", file: file, line: line) }
}

func expectEqual<T: Equatable>(_ a: T, _ b: T, _ msg: String = "",
                               file: String = #fileID, line: Int = #line) {
    checkCount += 1
    if a != b { fail(msg, "expected \(b) got \(a)", file: file, line: line) }
}

func expectClose(_ a: Double?, _ b: Double, tol: Double = 1e-9, _ msg: String = "",
                 file: String = #fileID, line: Int = #line) {
    checkCount += 1
    guard let a else {
        fail(msg, "expected \(b) got nil", file: file, line: line)
        return
    }
    if !(abs(a - b) <= tol) { fail(msg, "expected \(b) got \(a)", file: file, line: line) }
}

func expectThrows<E: Error & Equatable>(_ e: E, _ body: () throws -> Void, _ msg: String = "",
                                        file: String = #fileID, line: Int = #line) {
    checkCount += 1
    do {
        try body()
        fail(msg, "expected \(e) got no error", file: file, line: line)
    } catch let got as E {
        if got != e { fail(msg, "expected \(e) got \(got)", file: file, line: line) }
    } catch {
        fail(msg, "expected \(e) got \(error)", file: file, line: line)
    }
}
