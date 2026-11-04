import SwiftUI

/// Inline settings: poll interval, endpoint URL, reset.
struct SettingsSection: View {
    let poller: Poller
    /// Applies a change on top of the current settings (keeps the chart range in sync).
    let change: (_ edit: (inout AppSettings) -> Void) -> Void
    @State private var draft: String

    init(poller: Poller, change: @escaping (_ edit: (inout AppSettings) -> Void) -> Void) {
        self.poller = poller
        self.change = change
        _draft = State(initialValue: poller.settings.endpoint.absoluteString)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Picker("Poll every", selection: Binding(
                get: { poller.settings.pollInterval },
                set: { v in change { $0.pollInterval = v } })) {
                ForEach(PollInterval.choices, id: \.self) { Text("\($0) s").tag($0) }
            }
            .pickerStyle(.menu)

            let valid = EndpointURL.validate(draft)
            HStack {
                TextField("Endpoint", text: $draft)
                    .textFieldStyle(.roundedBorder)
                    .font(.caption.monospaced())
                    .onSubmit { apply(valid) }
                Button("Apply") { apply(valid) }
                    .disabled(valid == nil || valid == poller.settings.endpoint)
            }
            if valid == nil {
                Text("Enter an http(s) URL").font(.caption).foregroundStyle(.red)
            }
            Button("Reset to defaults") {
                poller.apply(.default)
                draft = AppSettings.default.endpoint.absoluteString
            }
        }
    }

    private func apply(_ url: URL?) {
        guard let url else { return }
        change { $0.endpoint = url }
        draft = url.absoluteString
    }
}
