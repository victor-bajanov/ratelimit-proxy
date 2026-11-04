import Foundation
import Observation
import ServiceManagement

/// Launch at login via SMAppService.mainApp, which registers this bundle at
/// its current path.
@MainActor @Observable
final class LoginItem {
    static let shared = LoginItem()

    private(set) var status: LoginItemStatus = LoginItem.read()
    private(set) var error: String?

    /// On the very first launch, turn launch-at-login on; later launches
    /// leave the user's choice alone.
    func registerOnFirstLaunch(defaults: UserDefaults = .standard) {
        let initialised = defaults.bool(forKey: SettingsKey.loginItemInitialised)
        if LoginItemStatus.shouldRegisterOnLaunch(initialised: initialised, status: status) {
            set(true)
        }
        defaults.set(true, forKey: SettingsKey.loginItemInitialised)
    }

    func set(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() }
            else { try SMAppService.mainApp.unregister() }
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
        status = Self.read()
    }

    private static func read() -> LoginItemStatus {
        switch SMAppService.mainApp.status {
        case .enabled: .enabled
        case .requiresApproval: .requiresApproval
        case .notRegistered: .notRegistered
        case .notFound: .notFound
        @unknown default: .notFound
        }
    }
}
