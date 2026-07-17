import SwiftUI

// Entry point. `status` argument = one-shot headless status dump so the state
// logic is verifiable over ssh without a GUI session.
@main
struct AcceleratorBarMain {
    static func main() async {
        if CommandLine.arguments.contains("status") {
            print(await StatusModel.textStatus())
            return
        }
        await MainActor.run { AcceleratorBarApp.main() }
    }
}

struct AcceleratorBarApp: App {
    @StateObject private var model = StatusModel()

    var body: some Scene {
        MenuBarExtra {
            MenuView(model: model)
        } label: {
            // Menu-bar icon reflects health at a glance: bolt = running,
            // slashed bolt = stopped, exclamation = degraded.
            Image(systemName: iconName)
        }
        .menuBarExtraStyle(.window)
    }

    private var iconName: String {
        switch model.snap.overall {
        case .running: return "bolt.fill"
        case .stopped: return "bolt.slash"
        case .degraded: return "bolt.trianglebadge.exclamationmark"
        }
    }
}
