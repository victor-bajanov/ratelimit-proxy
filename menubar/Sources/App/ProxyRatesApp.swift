import SwiftUI

@main
struct ProxyRatesApp: App {
    init() {
        RenderLabel.runIfRequested(CommandLine.arguments)  // exits when requested
        Poller.shared.start()
    }

    var body: some Scene {
        MenuBarExtra {
            PanelView()
        } label: {
            MenuBarLabel()
        }
        .menuBarExtraStyle(.window)
    }
}
