import Foundation

enum RatesDecodeError: Error, Equatable { case invalidJSON, notAnObject, noMetrics }

/// The metrics read from one /__proxy/rates response. Absent metrics are missing keys.
struct RatesSnapshot: Equatable, Sendable {
    var values: [Metric: Double]

    init(values: [Metric: Double]) { self.values = values }

    subscript(metric: Metric) -> Double? { values[metric] }

    /// Tolerant decode: extra keys ignored; a missing key, null, string or JSON
    /// bool leaves that metric absent. Ints and floats both accepted.
    static func decode(from data: Data) throws -> RatesSnapshot {
        let root: Any
        do {
            root = try JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
        } catch {
            throw RatesDecodeError.invalidJSON
        }
        guard let object = root as? [String: Any] else { throw RatesDecodeError.notAnObject }

        var values: [Metric: Double] = [:]
        for metric in Metric.allCases {
            if let v = number(at: metric.path, in: object) { values[metric] = v }
        }
        guard !values.isEmpty else { throw RatesDecodeError.noMetrics }
        return RatesSnapshot(values: values)
    }

    private static func number(at path: [String], in object: [String: Any]) -> Double? {
        var node: Any = object
        for key in path {
            guard let dict = node as? [String: Any], let next = dict[key] else { return nil }
            node = next
        }
        guard let n = node as? NSNumber,
              CFGetTypeID(n) != CFBooleanGetTypeID() else { return nil }
        return n.doubleValue
    }
}
