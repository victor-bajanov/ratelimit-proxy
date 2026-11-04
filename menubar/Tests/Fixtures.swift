import Foundation

let realPayload = #"{"window_s":60,"in_flight":9,"requests":{"started":53,"finished":57},"rps":{"started":0.883,"finished":0.95},"tokens":{"input":4723,"output":10761,"cache_write":75012,"cache_read":1582641,"credits":40130},"tps":{"input":78.7,"output":179.35,"cache_write":1250.2,"cache_read":26377.35,"credits":668.8}}"#

func t(_ s: Double) -> Date { Date(timeIntervalSince1970: 1_800_000_000 + s) }
