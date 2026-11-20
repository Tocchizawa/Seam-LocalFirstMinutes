import SeamCore
import SeamWorkspace
import SwiftUI

#Preview("実操作（サンプルデータ）") {
    if #available(macOS 26.0, *) {
        Workspace(api: SeamAPI(port: 18900))
            .frame(width: 1040, height: 660)
    } else {
        Text("macOS 26以降が必要です")
    }
}
