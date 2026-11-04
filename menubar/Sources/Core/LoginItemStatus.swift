import Foundation

/// SMAppService.Status, mirrored so the decisions can be tested without
/// ServiceManagement.
enum LoginItemStatus: Equatable, Sendable {
    case enabled, requiresApproval, notRegistered, notFound

    /// Registered with macOS, whether or not the user has approved it yet.
    var isOn: Bool { self == .enabled || self == .requiresApproval }

    var note: String? {
        switch self {
        case .enabled, .notRegistered: nil
        case .requiresApproval: "Allow ProxyRates in System Settings › General › Login Items"
        case .notFound: "macOS couldn't find the app to register"
        }
    }

    /// Launch at login defaults to on: register once, on the first launch,
    /// and after that leave it to the user's switch.
    static func shouldRegisterOnLaunch(initialised: Bool, status: LoginItemStatus) -> Bool {
        !initialised && status == .notRegistered
    }
}
