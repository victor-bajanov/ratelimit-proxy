import Foundation
import Observation

/// Polls the rates endpoint from launch and keeps the in-memory history.
@MainActor @Observable
final class Poller {
    static let shared = Poller()

    var store = SampleStore()
    var lastError: String?
    private(set) var settings: AppSettings

    @ObservationIgnored private var task: Task<Void, Never>?
    @ObservationIgnored private let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 3
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: config)
    }()

    init() {
        settings = AppSettings.load(from: .standard)
    }

    func start() {
        task?.cancel()
        task = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let started = Date()
                await self.pollOnce()
                let delay = PollInterval.nextDelay(interval: self.settings.pollInterval,
                                                   elapsed: Date().timeIntervalSince(started))
                try? await Task.sleep(for: .seconds(delay))
            }
        }
    }

    func apply(_ new: AppSettings) {
        let old = settings
        settings = new
        new.save(to: .standard)
        if new.endpoint != old.endpoint { store.removeAll() }
        if new.endpoint != old.endpoint || new.pollInterval != old.pollInterval { start() }
    }

    private func pollOnce() async {
        let endpoint = settings.endpoint
        var request = URLRequest(url: endpoint, cachePolicy: .reloadIgnoringLocalCacheData,
                                 timeoutInterval: 3)
        request.httpMethod = "GET"
        do {
            let (data, response) = try await session.data(for: request)
            guard !Task.isCancelled, endpoint == settings.endpoint else { return }
            if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                fail("HTTP \(http.statusCode)")
                return
            }
            do {
                store.record(try RatesSnapshot.decode(from: data), at: Date())
                lastError = nil
            } catch {
                fail("Bad response")
            }
        } catch {
            guard !Task.isCancelled, endpoint == settings.endpoint else { return }
            fail(Self.describe(error))
        }
    }

    private func fail(_ message: String) {
        store.recordGap(at: Date())
        lastError = message
    }

    private static func describe(_ error: Error) -> String {
        guard let e = error as? URLError else { return "Request failed" }
        switch e.code {
        case .cannotConnectToHost, .cannotFindHost, .networkConnectionLost, .notConnectedToInternet:
            return "Could not connect"
        case .timedOut: return "Timed out"
        case .appTransportSecurityRequiresSecureConnection: return "Plain http blocked (use https)"
        default: return "Request failed (\(e.code.rawValue))"
        }
    }
}
