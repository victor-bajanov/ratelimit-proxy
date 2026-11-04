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
        let effects = settings.changeEffects(to: new)
        settings = new
        new.save(to: .standard)
        if effects.clearHistory { store.removeAll() }
        if effects.restartPolling { start() }
    }

    private func pollOnce() async {
        let endpoint = settings.endpoint
        var request = URLRequest(url: endpoint, cachePolicy: .reloadIgnoringLocalCacheData,
                                 timeoutInterval: 3)
        request.httpMethod = "GET"
        let outcome: PollOutcome
        do {
            let (data, response) = try await session.data(for: request)
            outcome = PollOutcome.from(statusCode: (response as? HTTPURLResponse)?.statusCode,
                                       data: data)
        } catch {
            outcome = .failure(PollOutcome.describe(error))
        }
        // Drop results from a cancelled loop or a superseded endpoint.
        guard !Task.isCancelled, endpoint == settings.endpoint else { return }
        store.record(outcome, at: Date(), pollInterval: settings.pollInterval)
        lastError = outcome.errorMessage
    }
}
