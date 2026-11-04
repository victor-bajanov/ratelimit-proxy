import Foundation

func loginItemTests() {
    // The switch reads on while macOS holds the registration, approved or not.
    expect(LoginItemStatus.enabled.isOn)
    expect(LoginItemStatus.requiresApproval.isOn)
    expect(!LoginItemStatus.notRegistered.isOn)
    expect(!LoginItemStatus.notFound.isOn)

    expectEqual(LoginItemStatus.enabled.note, nil)
    expectEqual(LoginItemStatus.notRegistered.note, nil)
    expectEqual(LoginItemStatus.requiresApproval.note,
                "Allow ProxyRates in System Settings › General › Login Items")
    expectEqual(LoginItemStatus.notFound.note, "macOS couldn't find the app to register")

    // First launch turns it on once; after that the user's choice stands.
    expect(LoginItemStatus.shouldRegisterOnLaunch(initialised: false, status: .notRegistered))
    expect(!LoginItemStatus.shouldRegisterOnLaunch(initialised: true, status: .notRegistered))
    expect(!LoginItemStatus.shouldRegisterOnLaunch(initialised: false, status: .enabled))
    expect(!LoginItemStatus.shouldRegisterOnLaunch(initialised: false, status: .requiresApproval))
    expect(!LoginItemStatus.shouldRegisterOnLaunch(initialised: false, status: .notFound))

    expectEqual(SettingsKey.loginItemInitialised, "loginItemInitialised")
}
