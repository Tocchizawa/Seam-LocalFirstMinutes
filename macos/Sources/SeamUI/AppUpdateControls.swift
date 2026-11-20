import SwiftUI

package struct AppUpdateControls {
    package let automaticallyChecks: Binding<Bool>
    package let automaticallyInstalls: Binding<Bool>
    package let canCheck: Bool
    package let check: () -> Void

    package init(automaticallyChecks: Binding<Bool>, automaticallyInstalls: Binding<Bool>,
                 canCheck: Bool, check: @escaping () -> Void) {
        self.automaticallyChecks = automaticallyChecks
        self.automaticallyInstalls = automaticallyInstalls
        self.canCheck = canCheck
        self.check = check
    }
}

extension EnvironmentValues {
    @Entry package var appUpdateControls: AppUpdateControls?
}
