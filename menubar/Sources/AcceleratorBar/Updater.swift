import Sparkle
import SwiftUI

// Wraps Sparkle's updater so the menu can drive a manual "Check for Updates"
// and so scheduled background checks run (configured via SUEnableAutomaticChecks
// / SUScheduledCheckInterval in Info.plist). The app is Developer-ID signed and
// notarized in CI, and the appcast feed (SUFeedURL) is EdDSA-verified against
// SUPublicEDKey, so Sparkle-delivered updates install without Gatekeeper friction.
@MainActor
final class UpdaterModel: ObservableObject {
    static let shared = UpdaterModel()

    private let controller = SPUStandardUpdaterController(
        startingUpdater: true, updaterDelegate: nil, userDriverDelegate: nil)

    @Published var canCheck = true

    private init() {
        canCheck = controller.updater.canCheckForUpdates
    }

    func checkForUpdates() {
        controller.updater.checkForUpdates()
    }
}
